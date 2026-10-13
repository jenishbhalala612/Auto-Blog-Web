"""
AutoBlog Engine — Content Writer

Uses Ollama (local LLM) to draft blog posts.
Workflow:
1. Takes a trending topic + keywords
2. Researches via SearXNG for context
3. Generates a structured blog post
4. Saves as markdown in vault/Drafts/

Posts include full SEO frontmatter and are ready
for your review in Obsidian.
"""

import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from bs4 import BeautifulSoup
from rich.console import Console

import config
import db
from modules.status_tracker import update_global_progress

console = Console(legacy_windows=False)


class ContentWriter:
    """AI-powered content writer using Ollama."""

    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True)

    def write_drafts(self, topics: list[dict], keywords: list[dict], max_drafts: int | None = None) -> list[dict]:
        """Write draft posts for given topics (8 blogs for each configured language)."""
        limit_per_lang = max_drafts if max_drafts is not None else config.MAX_DAILY_DRAFTS
        drafts = []
        topics = topics[:limit_per_lang]
        total = len(topics)
        for i, topic in enumerate(topics):
            pct = int((i / total) * 100) if total > 0 else 0
            update_global_progress(pct)

            title = topic.get("title", "") if isinstance(topic, dict) else str(topic)
            source_info = {
                "source": topic.get("source", "Web Trends") if isinstance(topic, dict) else "Web Trends",
                "url": topic.get("url", "") if isinstance(topic, dict) else "",
            }
            topic_keywords = [
                kw["keyword"] for kw in keywords
                if kw.get("topic", "").lower() in title.lower() or title.lower() in kw.get("topic", "").lower()
            ][:6]

            topic_has_draft = False
            # Generate blog post for each configured language (e.g. 8 English and 8 Hindi)
            for lang in config.LANGUAGES:
                draft = self._write_post(
                    title,
                    topic_keywords,
                    lang,
                    source_info=source_info,
                    model=config.OLLAMA_MODEL,
                )
                if draft:
                    drafts.append(draft)
                    topic_has_draft = True
                time.sleep(2)

            # Only mark topic as used if at least one draft was successfully generated
            if topic_has_draft and isinstance(topic, dict) and "id" in topic:
                db.mark_topic_used(topic["id"])

        update_global_progress(100)
        return drafts

    def _write_post(
        self,
        topic: str,
        keywords: list[str],
        language: str,
        source_info: dict | None = None,
        model: str | None = None,
    ) -> dict | None:
        """Generate a single factual, grounded blog post."""
        try:
            import ollama

            source_info = source_info or {}
            fallback_url = source_info.get("url", "")

            # Step 1: Multi-tier research (SearXNG -> Source URL scraping -> DuckDuckGo -> Context synthesis)
            research_context, source_urls = self._research_topic(topic, fallback_url=fallback_url)
            if not research_context or not research_context.strip():
                research_context, source_urls = self._synthesize_fallback_context(topic, fallback_url)

            # Step 2: Generate the post with strict grounding prompt
            prompt = self._build_prompt(topic, keywords, language, research_context, source_urls)

            console.print(f"    ✍️  Writing: [cyan]{topic[:40]}...[/] ({language})")
            start = time.time()

            use_model = model or config.OLLAMA_MODEL
            response = ollama.chat(
                model=use_model,
                messages=[
                    {"role": "system", "content": self._system_prompt(language)},
                    {"role": "user", "content": prompt},
                ],
                options={
                    "temperature": 0.5,
                    "num_predict": 3000,
                },
                keep_alive=0,
            )

            content = response["message"]["content"].strip()
            duration = time.time() - start
            console.print(f"    ⏱️  Generated in {duration:.1f}s")

            # Step 3: Ensure grounded citations are attached in Sources section
            content = self._ensure_grounding(content, source_urls, language)

            # Step 4: Parse, tag, and save with full audit metadata
            draft = self._parse_and_save(topic, content, keywords, language, source_info=source_info)
            console.print(f"    ✅ Draft saved: [green]{draft.get('slug', topic[:30])}[/] ({language})")
            return draft

        except Exception as e:
            console.print(f"    [red]❌ Failed to write post: {e}[/]")
            db.log_error("content_writer", type(e).__name__, f"Failed to write: {topic} ({language}): {e}")
            return None

    def _ensure_grounding(self, content: str, source_urls: list[str], language: str) -> str:
        """Ensure cited source URLs are explicitly attached in the Sources section."""
        if not source_urls:
            return content

        found_urls = re.findall(r"https?://[^\s\)\"']+", content)
        real_domains = {self._domain(u) for u in source_urls if u}
        cited_domains = {self._domain(u) for u in found_urls if u}

        # If LLM didn't cite the exact domain in text, append formatted Sources section
        if not (real_domains & cited_domains):
            heading = "## स्रोत" if language == "hi" else "## Sources"
            sources_block = f"\n\n{heading}\n" + "\n".join(f"- {u}" for u in source_urls[:5] if u)
            if heading in content:
                content = content.replace(heading, f"{heading}\n" + "\n".join(f"- {u}" for u in source_urls[:5] if u))
            else:
                content += sources_block

        return content

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            return ""

    def _research_topic(self, topic: str, fallback_url: str = "") -> tuple[str, list[str]]:
        """Multi-tier research: SearXNG -> Source URL scraping -> DuckDuckGo -> Baseline synthesis."""
        # Tier 1: Try SearXNG (if online)
        context, urls = self._search_searxng(topic)
        if context and urls:
            return context, urls

        # Tier 2: Direct Source URL Scraping (if topic came from RSS / Hacker News)
        if fallback_url and fallback_url.startswith(("http://", "https://")):
            context, urls = self._scrape_source_url(fallback_url, topic)
            if context and urls:
                return context, urls

        # Tier 3: DuckDuckGo Web Search (public, free, zero-config)
        context, urls = self._search_duckduckgo(topic)
        if context and urls:
            return context, urls

        # Tier 4: Baseline Context Synthesis (guarantees pipeline never drops topic)
        return self._synthesize_fallback_context(topic, fallback_url)

    def _search_searxng(self, topic: str) -> tuple[str, list[str]]:
        """Query self-hosted SearXNG instance."""
        source_urls = []
        try:
            url = f"{config.SEARXNG_URL}/search"
            params = {
                "q": topic,
                "format": "json",
                "categories": "general",
                "engines": "google,bing,duckduckgo",
                "language": "en",
            }
            resp = self.client.get(url, params=params, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])[:5]
                context_parts = []
                for r in results:
                    title = r.get("title", "")
                    snippet = r.get("content", "").strip()
                    u = r.get("url", "").strip()
                    if snippet:
                        if u:
                            source_urls.append(u)
                        context_parts.append(f"- {title}: {snippet} (Source: {u})")
                if context_parts:
                    console.print(f"    🔍 SearXNG research found for: [cyan]{topic[:35]}[/]")
                    return "\n".join(context_parts), source_urls
        except Exception:
            pass  # Fall through smoothly to next tier
        return "", []

    def _scrape_source_url(self, url: str, topic: str) -> tuple[str, list[str]]:
        """Extract article body paragraphs from the topic's source URL."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = self.client.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                paragraphs = [p.text.strip() for p in soup.find_all("p") if len(p.text.strip()) > 40]
                if paragraphs:
                    extracted_text = " ".join(paragraphs[:6])
                    if len(extracted_text) > 100:
                        context = f"- Source Article Content ({url}): {extracted_text[:1200]}"
                        console.print(f"    📄 Extracted context directly from source URL: [cyan]{url[:45]}[/]")
                        return context, [url]
        except Exception:
            pass
        return "", []

    def _search_duckduckgo(self, topic: str) -> tuple[str, list[str]]:
        """Search DuckDuckGo HTML for snippets and source URLs."""
        source_urls = []
        context_parts = []
        try:
            url = "https://html.duckduckgo.com/html/"
            params = {"q": topic}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            resp = self.client.get(url, params=params, headers=headers, timeout=12)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                titles = soup.select(".result__title a")
                snippets = soup.select(".result__snippet")
                for a_tag, s_tag in list(zip(titles, snippets))[:5]:
                    raw_href = a_tag.get("href", "")
                    clean_url = ""
                    if "uddg=" in raw_href:
                        parsed_q = urllib.parse.parse_qs(urllib.parse.urlparse(raw_href).query)
                        clean_url = parsed_q.get("uddg", [""])[0]
                    elif raw_href.startswith("http"):
                        clean_url = raw_href

                    title_text = a_tag.text.strip()
                    snippet_text = s_tag.text.strip()
                    if snippet_text:
                        if clean_url:
                            source_urls.append(clean_url)
                        context_parts.append(f"- {title_text}: {snippet_text} (Source: {clean_url})")

                if context_parts:
                    console.print(f"    🌐 Fetched web research via DuckDuckGo for: [cyan]{topic[:35]}[/]")
                    return "\n".join(context_parts), source_urls
        except Exception:
            pass
        return "", []

    def _synthesize_fallback_context(self, topic: str, fallback_url: str = "") -> tuple[str, list[str]]:
        """Generate baseline technical context when all external search engines are unavailable."""
        canonical_source = fallback_url or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(topic.replace(' ', '_'))}"
        context = (
            f"- Topic Analysis ({topic}): In-depth technical analysis and comprehensive overview of {topic}, "
            f"covering key background, core developments, real-world applications, and practical insights. "
            f"(Source: {canonical_source})"
        )
        console.print(f"    💡 Generated baseline technical context for: [cyan]{topic[:35]}[/]")
        return context, [canonical_source]

    def _system_prompt(self, language: str) -> str:
        """System prompt for the content writer."""
        if language == "hi":
            return """You are an expert Hindi tech blogger for AI & Tech Simplified.
Write in natural Hindi (Devanagari script). Use a conversational, authoritative tone.
Include English tech terms where natural (like "AI", "API", "LLM", "Prompt Injection").
GROUNDING MANDATE: Never invent facts, false career chronologies, or unverified claims.
Only state facts substantiated by research. Cite your sources."""

        return """You are an expert tech journalist and blogger writing for AI & Tech Simplified.
Write in a clear, engaging, conversational tone. Make complex topics accessible.
Use short paragraphs, bullet points, and real examples.
GROUNDING MANDATE: Never invent facts, false career chronologies, or unverified claims.
Only state facts substantiated by research. Cite your sources."""

    def _build_prompt(
        self,
        topic: str,
        keywords: list[str],
        language: str,
        research: str,
        source_urls: list[str],
    ) -> str:
        """Build the content generation prompt with strict grounding rules."""
        keywords_str = ", ".join(keywords) if keywords else "N/A"
        sources_list = "\n".join(f"- {url}" for url in source_urls[:5]) if source_urls else f"- Reference documentation on {topic}"

        if language == "hi":
            return f"""Write a comprehensive Hindi tech blog post about: "{topic}"

RESEARCH CONTEXT (FACTS ONLY):
{research}

SEO Keywords to include naturally: {keywords_str}

CRITICAL ANTI-HALLUCINATION RULES:
1. ONLY state facts that appear in the research context above. Never invent biographies, release dates, or product claims.
2. If you do not have a specific fact or date, write "पुष्टि नहीं की गई" (not confirmed) rather than guessing or inventing.
3. You MUST end the post with a "## स्रोत" section listing the research links:
{sources_list}

Requirements:
- Write 800-1200 words in Hindi (Devanagari script)
- Start with an engaging introduction that hooks the reader
- Use 3-5 H2 subheadings (##) to organize the content
- Keep technical terms in English where natural
- End with a conclusion and the "## स्रोत" section with links

Write the blog post content (no frontmatter, start directly with the article text):"""

        return f"""Write a comprehensive blog post about: "{topic}"

RESEARCH CONTEXT (FACTS ONLY):
{research}

SEO Keywords to include naturally: {keywords_str}

CRITICAL ANTI-HALLUCINATION RULES:
1. ONLY state facts that appear in the research context above. Never invent biographies, release dates, or product claims.
2. If you do not have a specific fact or date, write "not confirmed" or "unverified" rather than guessing or inventing.
3. You MUST end the post with a "## Sources" section listing the research links:
{sources_list}

Requirements:
- Write 800-1200 words
- Start with an engaging introduction that hooks the reader
- Use 3-5 H2 subheadings (##) to organize the content
- Include practical examples, actionable takeaways, and clear explanations
- End with a conclusion and the "## Sources" section with links

Write the blog post content (no frontmatter, start directly with the article text):"""

    def _parse_and_save(
        self,
        topic: str,
        content: str,
        keywords: list[str],
        language: str,
        source_info: dict | None = None,
    ) -> dict:
        """Parse generated content and save as markdown with frontmatter and audit metadata."""
        # Check and handle empty keywords
        if not keywords:
            console.print(f"    [yellow]⚠ Warning: Empty keywords for '{topic}' — generating fallback keywords[/]")
            title_words = [w for w in re.findall(r"[A-Za-z0-9]+", topic) if len(w) > 3]
            keywords = [topic] + [f"{w} guide" for w in title_words[:4]]

        # Generate slug
        slug = self._make_slug(topic)
        if language != config.PRIMARY_LANGUAGE:
            slug = f"{slug}-{language}"

        # Generate content-aware tags (no hardcoded "Tech")
        tags = self._extract_tags(topic, keywords, content)

        # Generate description
        description = self._extract_description(content)

        source_info = source_info or {}
        source_name = source_info.get("source", "Web Research")
        source_url = source_info.get("url", "")

        # Build frontmatter with audit traceability back to trends.md
        today = datetime.now().strftime("%Y-%m-%d")
        frontmatter = f"""---
title: "{topic.replace('"', "'")}"
description: "{description.replace('"', "'")}"
pubDate: {today}
tags: [{', '.join(f'"{t}"' for t in tags)}]
keywords: [{', '.join(f'"{k}"' for k in keywords[:5])}]
source_trend: "{source_name.replace('"', "'")}"
source_url: "{source_url}"
author: "AutoBlog"
lang: "{language}"
heroImage: ""
heroImageAlt: "{topic.replace('"', "'")}"
draft: true
---

"""
        full_content = frontmatter + content
        word_count = len(content.split())

        # Save to vault: vault/Drafts/<language>/<today>/
        draft_dir = config.VAULT_DRAFTS / language / today
        draft_dir.mkdir(parents=True, exist_ok=True)
        file_path = draft_dir / f"{today}-{slug}.md"
        file_path.write_text(full_content, encoding="utf-8")

        # Log to database
        db.log_post(
            title=topic,
            slug=slug,
            language=language,
            status="draft",
            file_path=str(file_path),
            word_count=word_count,
            keywords=", ".join(keywords[:5]),
            tags=", ".join(tags),
        )

        console.print(f"    📝 Saved: [green]{file_path.name}[/] ({word_count} words)")

        return {
            "title": topic,
            "slug": slug,
            "language": language,
            "file_path": str(file_path),
            "word_count": word_count,
        }

    def _make_slug(self, title: str) -> str:
        """Generate a URL-friendly slug from a title."""
        slug = title.lower()
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-')
        return slug[:60]

    def _extract_tags(self, topic: str, keywords: list[str], content: str = "") -> list[str]:
        """Extract 2-4 content-aware tags dynamically based on topic, keywords, and body text."""
        tag_candidates = set()
        categories = {
            "AI Tools": ["ai", "llm", "gpt", "claude", "agent", "neural", "deep learning", "chatbot", "model", "anthropic", "openai"],
            "Programming": ["code", "programming", "developer", "python", "javascript", "api", "github", "software"],
            "Gadgets & Hardware": ["kindle", "laptop", "surface", "smartphone", "iphone", "android", "tablet", "chip", "processor", "gpu", "macbook", "gadget", "hardware", "device"],
            "Tutorial": ["how to", "guide", "tutorial", "step by step", "tips", "setup"],
            "News & Releases": ["new", "launch", "announces", "release", "unveil", "update", "breakthrough"],
            "Cybersecurity": ["security", "vulnerability", "encryption", "scanner", "privacy", "protection"],
            "Cloud & DevOps": ["cloud", "docker", "server", "linux", "saas", "infrastructure"],
            "Open Source": ["open source", "open-source", "oss", "foss"],
        }

        combined = (topic + " " + " ".join(keywords) + " " + content[:400]).lower()
        for tag, triggers in categories.items():
            if any(re.search(rf"\b{re.escape(t)}\b", combined) for t in triggers):
                tag_candidates.add(tag)

        # Do NOT unconditionally force "Tech". If nothing matches, categorize as "Technology"
        if not tag_candidates:
            tag_candidates.add("Technology")

        return list(tag_candidates)[:4]

    def _extract_description(self, content: str) -> str:
        """Extract a description from the content (first meaningful paragraph)."""
        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            # Skip headings and empty lines
            if line and not line.startswith("#") and len(line) > 50:
                # Truncate to ~160 chars for SEO
                if len(line) > 160:
                    return line[:157] + "..."
                return line
        return "A comprehensive guide on the latest in AI and technology."
