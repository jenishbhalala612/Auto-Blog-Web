"""
AutoBlog Engine — Keyword Researcher

Finds SEO keywords for trending topics using:
1. SearXNG web search  (self-hosted, JSON API + autocomplete + suggestions)
2. Google autocomplete (free, no API key)
3. LLM-powered keyword expansion via Ollama

Results saved to vault/Keywords/ and SQLite.
"""

from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console

import config
import db
from modules.searxng_client import SearXNGClient
from modules.status_tracker import update_global_progress

console = Console()


class KeywordResearcher:
    """Research SEO keywords for given topics."""

    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True)
        self.searxng = SearXNGClient()

    def research(self, topics: list[dict], language: str = "en") -> list[dict]:
        """Research keywords for a list of topics with quality filtering and per-topic cap (5-6 max)."""
        all_keywords = []
        total = len(topics)

        for i, topic in enumerate(topics):
            # Update global progress bar
            pct = int((i / total) * 100) if total > 0 else 0
            update_global_progress(pct)

            title = topic.get("title", "") if isinstance(topic, dict) else str(topic)
            console.print(f"  🔑 Researching: [cyan]{title[:50]}...[/]")

            topic_candidates = []

            # Method 1: Google Autocomplete (highest search intent)
            kw_auto = self._google_autocomplete(title, language)
            topic_candidates.extend(kw_auto)

            # Method 2: SearXNG full search
            kw_searx = self._searxng_search_keywords(title, language)
            topic_candidates.extend(kw_searx)

            # Method 3: LLM keyword expansion (grounded & contextual)
            kw_llm = self._llm_keyword_expansion(title, language)
            topic_candidates.extend(kw_llm)

            # Filter, deduplicate, and select top 5-6 keywords for THIS topic
            selected_for_topic = []
            seen_for_topic = set()

            for kw in topic_candidates:
                kw_clean = kw["keyword"].strip().lower()
                if (
                    kw_clean not in seen_for_topic
                    and self._is_valid_keyword(kw["keyword"], title)
                ):
                    seen_for_topic.add(kw_clean)
                    selected_for_topic.append(kw)
                    if len(selected_for_topic) >= 6:
                        break

            all_keywords.extend(selected_for_topic)
            console.print(f"    ✓ Selected [green]{len(selected_for_topic)}[/] high-quality keywords")

        # Global deduplication
        seen = set()
        unique_keywords = []
        for kw in all_keywords:
            if kw["keyword"].lower() not in seen:
                seen.add(kw["keyword"].lower())
                unique_keywords.append(kw)

        # Save results
        self._save_results(unique_keywords, language)

        return unique_keywords

    def _is_valid_keyword(self, keyword: str, topic: str) -> bool:
        """Sanity check: reject garbled/scraped junk, website navigation artifacts, and incoherent phrases."""
        kw = keyword.strip()
        kw_lower = kw.lower()

        # Length check
        if len(kw) < 4 or len(kw) > 70:
            return False

        # Reject website navigation/scraping artifacts (like 'fiction records contact', 'privacy policy')
        junk_terms = [
            "contact us", "contact", "phone number", "customer care", "customer service",
            "support number", "records contact", "address", "privacy policy", "terms of service",
            "login", "sign in", "sign up", "download free mp3", "full movie", "torrent",
            "wikipedia", "facebook", "instagram", "twitter", "reddit thread", "404 not found",
        ]
        if any(jt in kw_lower for jt in junk_terms):
            return False

        # Reject mechanically prepended nonsense on obviously non-commercial topics
        # e.g., 'free petroleum', 'top trump iran'
        words = kw_lower.split()
        if len(words) >= 2:
            first_word = words[0]
            if first_word in ("free", "best", "top", "cheap") and any(
                w in kw_lower for w in ("petroleum", "oil", "war", "investigation", "crime", "calendar")
            ):
                return False

        # Ensure keyword contains alphanumeric characters and not just symbols
        if not re.search(r"[a-zA-Z0-9\u0900-\u097F]", kw):
            return False

        return True


    def _google_autocomplete(self, query: str, language: str = "en") -> list[dict]:
        """Get keyword suggestions from Google autocomplete (free, no key)."""
        keywords = []
        try:
            # Google autocomplete API (unofficial but stable)
            hl = "hi" if language == "hi" else "en"
            url = "https://suggestqueries.google.com/complete/search"
            params = {
                "client": "firefox",
                "q": query,
                "hl": hl,
            }
            resp = self.client.get(url, params=params)
            data = resp.json()

            if isinstance(data, list) and len(data) > 1:
                suggestions = data[1]
                for s in suggestions[:8]:
                    keywords.append({
                        "keyword": s,
                        "source": "Google Autocomplete",
                        "language": language,
                        "topic": query,
                    })
        except Exception as e:
            console.print(f"    [yellow]⚠ Google autocomplete failed: {e}[/]")
            db.log_error("keyword_researcher", "google_autocomplete", str(e))
        return keywords

    def _searxng_search_keywords(self, topic: str, language: str = "en") -> list[dict]:
        """Use SearXNG JSON API for full keyword research: autocomplete + suggestions + title mining."""
        if not self.searxng.is_available():
            console.print(f"    [dim]SearXNG not available, skipping[/]")
            return []
        try:
            keywords = self.searxng.discover_keywords(topic, language=language, max_results=12)
            if keywords:
                console.print(f"    [green]✓[/] SearXNG returned {len(keywords)} keyword suggestions")
            return keywords
        except Exception as e:
            console.print(f"    [yellow]SearXNG keyword search failed: {e}[/]")
            db.log_error("keyword_researcher", "searxng_search", str(e))
            return []

    def _llm_keyword_expansion(self, topic: str, language: str = "en") -> list[dict]:
        """Use Ollama to generate related long-tail keywords."""
        keywords = []
        try:
            import ollama

            lang_instruction = ""
            if language == "hi":
                lang_instruction = "Generate keywords in both Hindi (Devanagari) and Hinglish (Hindi in Roman script). "

            prompt = f"""You are an expert SEO strategist. Generate 4 to 5 high-intent, natural search queries for a tech article about:
"{topic}"

{lang_instruction}Strict Rules:
- Every keyword must make NATURAL sense for this specific topic (do NOT blindly force "free", "best", or "how to" if it doesn't fit).
- Length: 2 to 5 words per keyword.
- Focus on what real developers or tech enthusiasts actually type into Google.
- Do NOT use generic website navigation or contact terms.
- Return ONLY the keywords, one per line. No numbering, no bullets, no explanation.

Keywords:"""

            response = ollama.chat(
                model=config.OLLAMA_MODEL_FAST,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.3},
                keep_alive=0,
            )

            text = response["message"]["content"].strip()
            for line in text.split("\n"):
                line = line.strip().strip("-").strip("•").strip()
                if line and len(line) > 5 and len(line) < 80:
                    keywords.append({
                        "keyword": line,
                        "source": "LLM (Ollama)",
                        "language": language,
                        "topic": topic,
                    })

        except Exception as e:
            console.print(f"    [yellow]⚠ LLM keyword expansion failed: {e}[/]")
            db.log_error("keyword_researcher", "llm_expansion", str(e))
        return keywords

    def _save_results(self, keywords: list[dict], language: str):
        """Save keywords to vault and database."""
        today = datetime.now().strftime("%Y-%m-%d")
        vault_file = config.VAULT_KEYWORDS / f"{today}-keywords-{language}.md"

        lines = [
            f"# 🔑 SEO Keywords — {today} ({language.upper()})\n",
            f"*Generated at {datetime.now().strftime('%H:%M:%S')}*\n",
            f"**Total keywords:** {len(keywords)}\n",
            "---\n",
            "| Keyword | Source | Topic |",
            "|---------|--------|-------|",
        ]

        for kw in keywords:
            lines.append(f"| {kw['keyword']} | {kw['source']} | {kw.get('topic', '')[:30]} |")

        vault_file.write_text("\n".join(lines), encoding="utf-8")

        # Save to database
        for kw in keywords:
            db.log_keyword(
                keyword=kw["keyword"],
                language=kw.get("language", language),
                topic=kw.get("topic", ""),
            )
