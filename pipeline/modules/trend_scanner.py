"""
AutoBlog Engine — Trend Scanner

Discovers high-value trending topics from multiple free sources:
1. SearXNG web search (self-hosted, JSON API)
2. Google Trends (tech-filtered, breakout consumer & innovation trends)
3. Hacker News front page (top-voted community discussions)
4. Reddit trending (r/technology, r/artificial)
5. Leading Tech & Gadget RSS feeds (TechCrunch, The Verge, Ars Technica, VentureBeat, Wired)

Filters out low-value noise (celebrity gossip, routine sports scores, local crime)
and prioritizes high-value stories (AI breakthroughs, gadgets, buyer guides, startup funding).
Results are saved to the Obsidian vault (Research/) and SQLite.
"""

import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
import feedparser
from rich.console import Console

import config
import db
from modules.searxng_client import SearXNGClient

console = Console()


class TrendScanner:
    """Scans multiple sources for high-value stories across tech, AI, gadgets, and innovation."""

    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True)
        self.searxng = SearXNGClient()
        self.sources = {
            "google_trends": "https://trends.google.com/trending/rss?geo=US",
            "hackernews": "https://hnrss.org/frontpage?points=100",
            "reddit_tech": "https://www.reddit.com/r/technology/hot.json?limit=15",
            "reddit_ai": "https://www.reddit.com/r/artificial/hot.json?limit=15",
        }
        # Curated feeds for a diverse mixture of high-value stories
        self.rss_feeds = [
            "https://techcrunch.com/feed/",                                    # Startups, funding, major acquisitions
            "https://www.theverge.com/rss/index.xml",                          # Consumer gadgets, devices, lifestyle tech
            "https://feeds.arstechnica.com/arstechnica/technology-lab",         # Deep tech, science, computing
            "https://venturebeat.com/category/ai/feed/",                       # AI breakthroughs, tools, enterprise tech
            "https://www.wired.com/feed/category/gear/latest/rss",             # Hands-on hardware, reviews, smart gear
        ]

    def scan(self) -> list[dict]:
        """Scan all sources and return deduplicated, high-value trending topics."""
        all_topics = []

        # 1. SearXNG web search
        topics = self._scan_searxng()
        all_topics.extend(topics)
        if topics:
            console.print(f"  🔍 SearXNG Web Search: [green]{len(topics)}[/] topics")
        else:
            console.print(f"  🔍 SearXNG: [dim]offline or not configured[/]")

        # 2. Google Trends (Tech & Innovation filtered)
        topics = self._scan_google_trends()
        all_topics.extend(topics)
        console.print(f"  📊 Google Trends (Filtered): [green]{len(topics)}[/] topics")

        # 3. Hacker News
        topics = self._scan_hackernews()
        all_topics.extend(topics)
        console.print(f"  🔶 Hacker News: [green]{len(topics)}[/] topics")

        # 4. Reddit
        topics = self._scan_reddit()
        all_topics.extend(topics)
        console.print(f"  🤖 Reddit: [green]{len(topics)}[/] topics")

        # 5. RSS Feeds
        topics = self._scan_rss_feeds()
        all_topics.extend(topics)
        console.print(f"  📰 RSS Feeds: [green]{len(topics)}[/] topics")

        # Deduplicate by similar titles
        unique_topics = self._deduplicate(all_topics)

        # Score and prioritize by high editorial value
        scored = self._score_topics(unique_topics)

        # Save to vault and database
        self._save_results(scored)

        return scored

    def _scan_searxng(self) -> list[dict]:
        """Search trending topics via the self-hosted SearXNG JSON API."""
        if not self.searxng.is_available():
            return []
        try:
            raw = self.searxng.discover_trending()
            topics = []
            for item in raw:
                title = item.get("title", "").strip()
                if title and self._is_relevant(title):
                    topics.append({
                        "title": title,
                        "url": item.get("url", ""),
                        "source": item.get("source", "SearXNG"),
                        "published": item.get("published", ""),
                        "content_snippet": item.get("content", ""),
                    })
            return topics
        except Exception as e:
            console.print(f"  [yellow]SearXNG scan error: {e}[/]")
            db.log_error("trend_scanner", "searxng", str(e))
            return []

    def _scan_google_trends(self) -> list[dict]:
        """Parse Google Trends RSS feed with high-value filtering."""
        topics = []
        try:
            resp = self.client.get(self.sources["google_trends"])
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                # Apply filter to eliminate celebrity gossip / routine sports
                if self._is_relevant(title):
                    topics.append({
                        "title": title,
                        "url": entry.get("link", ""),
                        "source": "Google Trends",
                        "published": entry.get("published", ""),
                    })
        except Exception as e:
            console.print(f"  [yellow]⚠ Google Trends failed: {e}[/]")
            db.log_error("trend_scanner", "google_trends", str(e))
        return topics

    def _scan_hackernews(self) -> list[dict]:
        """Parse Hacker News RSS (top stories with 100+ points)."""
        topics = []
        try:
            resp = self.client.get(self.sources["hackernews"])
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if self._is_relevant(title):
                    topics.append({
                        "title": title,
                        "url": entry.get("link", ""),
                        "source": "Hacker News",
                        "published": entry.get("published", ""),
                    })
        except Exception as e:
            console.print(f"  [yellow]⚠ Hacker News failed: {e}[/]")
            db.log_error("trend_scanner", "hackernews", str(e))
        return topics

    def _scan_reddit(self) -> list[dict]:
        """Fetch trending posts from tech/AI subreddits."""
        topics = []
        headers = {"User-Agent": "AutoBlog/1.0"}

        for name, url in [("reddit_tech", self.sources["reddit_tech"]),
                          ("reddit_ai", self.sources["reddit_ai"])]:
            try:
                resp = self.client.get(url, headers=headers)
                data = resp.json()
                posts = data.get("data", {}).get("children", [])
                for post in posts:
                    pdata = post.get("data", {})
                    title = pdata.get("title", "").strip()
                    if pdata.get("score", 0) > 40 and self._is_relevant(title):
                        topics.append({
                            "title": title,
                            "url": f"https://reddit.com{pdata.get('permalink', '')}",
                            "source": f"Reddit ({name})",
                            "published": "",
                        })
                time.sleep(1)
            except Exception as e:
                console.print(f"  [yellow]⚠ {name} failed: {e}[/]")
                db.log_error("trend_scanner", name, str(e))
        return topics

    def _scan_rss_feeds(self) -> list[dict]:
        """Parse curated tech & gadget RSS feeds."""
        topics = []
        for feed_url in self.rss_feeds:
            try:
                resp = self.client.get(feed_url)
                feed = feedparser.parse(resp.text)
                feed_name = feed_url.split('/')[2].replace("www.", "").replace("feeds.", "")
                for entry in feed.entries[:8]:
                    title = entry.get("title", "").strip()
                    if self._is_relevant(title):
                        topics.append({
                            "title": title,
                            "url": entry.get("link", ""),
                            "source": f"RSS ({feed_name})",
                            "published": entry.get("published", ""),
                        })
            except Exception as e:
                console.print(f"  [yellow]⚠ RSS {feed_url} failed: {e}[/]")
        return topics

    def _is_relevant(self, title: str) -> bool:
        """
        Check if a topic strictly belongs to our niche (AI, software, gadgets, tech innovation)
        and passes the sensitive topic blocklist.
        """
        title_lower = title.lower()

        # ── 1. Sensitive Topic Blocklist (Real named officials, legal allegations, politics, pop-culture, commodities) ──
        sensitive_blocklist = [
            # Real named public officials & political/legal accusations
            r"\b(subpoena|subpoenaed|phone records|indicted|fbi|doj|grand jury|congressional|prosecutor|classified docs|testimony)\b",
            r"\b(susie wiles|kash patel|trump|biden|kamala|desantis|pelosi|mcconnell|cabinet|white house chief|senator|governor|attorney general)\b",
            r"\b(election fraud|voter fraud|partisan|impeach|impeachment|campaign finance|pac)\b",
            # Celebrities, Pop-Culture, Gossip & Entertainment
            r"\b(actor|actress|hollywood|dating|divorce|husband|wife|affair|girlfriend|boyfriend|ex-wife|ex-husband|red carpet|svu|bachelor|celebrity net worth|kardashian|giddish|brolin|comic con|pokemon|box office|movie trailer)\b",
            # Sports
            r"\b(basketball|football|baseball|soccer|cricket|nba|nfl|mlb|nhl|ipl|quarterback|touchdown|roster|game time|espn|coaching staff|playoffs schedule|hoops|ohio state)\b",
            # Crime, violence, tragedies
            r"\b(arrested|mugshot|murder|homicide|pleads guilty|guilty plea|sentenced to prison|fatal crash|shooting|stabbing|autopsy)\b",
            # Commodities & non-tech industries
            r"\b(petroleum|crude oil|diesel|gasoline|refinery|barrel of oil|commodities trading|mattel)\b",
            # YMYL Health / Medical advice
            r"\b(treatment for|cure for|cancer cure|symptoms of|dosage|medical diagnosis|weight loss pill|dietary supplement)\b",
            # Financial speculation
            r"\b(penny stock|get rich|100x gem|crypto moonshot|pump and dump|forex trading)\b",
        ]
        for pattern in sensitive_blocklist:
            if re.search(pattern, title_lower):
                return False

        # ── 2. Positive Niche Filter: Must be about AI, software, hardware, dev tools, or tech innovation ──
        high_value_keywords = [
            # AI & Intelligent Systems
            r"\bai\b", r"\bartificial intelligence\b", r"\bgpt\b", r"\bclaude\b", r"\bgemini\b",
            r"\bllm\b", r"\bllms\b", r"\bopenai\b", r"\banthropic\b", r"\bagent\b", r"\bagents\b",
            r"\brobot\b", r"\brobotics\b", r"\bneural\b", r"\bdeep learning\b", r"\bmachine learning\b",
            r"\bchatbot\b", r"\bautomation\b", r"\bprompt\b", r"\bhuggingface\b",
            # Hardware, Gadgets & Consumer Tech
            r"\blaptop\b", r"\blaptops\b", r"\bsmartphone\b", r"\bsmartphones\b", r"\biphone\b",
            r"\bandroid\b", r"\btablet\b", r"\btablets\b", r"\bipad\b", r"\bapple\b", r"\bgoogle\b",
            r"\bsamsung\b", r"\bmicrosoft\b", r"\bnvidia\b", r"\bchip\b", r"\bchips\b",
            r"\bprocessor\b", r"\bprocessors\b", r"\bintel\b", r"\bamd\b", r"\bqualcomm\b",
            r"\bmacbook\b", r"\bkindle\b", r"\be-reader\b", r"\bwearable\b", r"\bsmartwatch\b",
            r"\bgadget\b", r"\bgadgets\b", r"\bhardware\b", r"\brobotaxi\b", r"\bgpu\b",
            r"\bgpus\b", r"\bsemiconductor\b",
            # Apps, Software & Practical Digital Tools
            r"\bsoftware\b", r"\bbrowser\b", r"\bchrome\b", r"\bfirefox\b", r"\bwindows 11\b",
            r"\bmacos\b", r"\blinux\b", r"\bopen source\b", r"\bopen-source\b", r"\bdeveloper\b",
            r"\bdevelopers\b", r"\bprogramming\b", r"\bpython\b", r"\bcoding\b", r"\bapi\b",
            r"\bapis\b", r"\bgithub\b", r"\bcybersecurity\b", r"\bsecurity scan\b", r"\bcloud\b",
            r"\bsaas\b", r"\bquantum computing\b",
        ]

        # For single/two-word titles, enforce stricter tech core words
        words = title_lower.split()
        if len(words) <= 2:
            strict_tech_regex = (
                r"\b(ai|gpt|claude|gemini|llm|openai|anthropic|robot|nvidia|apple|"
                r"chip|software|startup|gadget|programming|python|linux|kindle)\b"
            )
            return bool(re.search(strict_tech_regex, title_lower))

        return any(re.search(pattern, title_lower) for pattern in high_value_keywords)


    def _deduplicate(self, topics: list[dict]) -> list[dict]:
        """Remove near-duplicate topics based on title similarity."""
        seen_titles = set()
        unique = []
        for topic in topics:
            normalized = re.sub(r'[^a-z0-9\s]', '', topic["title"].lower())
            words = set(normalized.split())
            is_duplicate = False
            for seen in seen_titles:
                seen_words = set(seen.split())
                if len(words & seen_words) / max(len(words), 1) > 0.6:
                    is_duplicate = True
                    break
            if not is_duplicate:
                seen_titles.add(normalized)
                unique.append(topic)
        return unique

    def _score_topics(self, topics: list[dict]) -> list[dict]:
        """
        Rank topics by true editorial value, audience interest, and actionable depth.
        """
        for topic in topics:
            score = 0.50
            title_lower = topic["title"].lower()

            # 1. Major Product Launch / Breakthrough Signals (+0.25)
            launch_signals = [
                "unveils", "launches", "releases", "introduces", "announces",
                "debuts", "raises", "funding", "new feature", "breakthrough",
                "milestone", "over $", "splits",
            ]
            if any(sig in title_lower for sig in launch_signals):
                score += 0.25

            # 2. High-Impact AI & Agents (+0.25)
            ai_signals = [
                "gpt", "claude", "openai", "anthropic", "ai agent", "gemini",
                "autonomous", "robotaxi", "neural", "llm", "intelligent ui",
            ]
            if any(sig in title_lower for sig in ai_signals):
                score += 0.25

            # 3. Actionable / Explainer / Buyer Guide value (+0.20)
            practical_signals = [
                "how to", "guide", "explained", "why", "what is", "best",
                "free", "budget", "vs", "review", "comparison", "tips", "need one",
            ]
            if any(sig in title_lower for sig in practical_signals):
                score += 0.20

            # 4. Major Tech Platforms & Hardware Brands (+0.15)
            major_brands = [
                "google", "apple", "microsoft", "meta", "nvidia",
                "amazon", "tesla", "samsung", "openai", "anthropic",
            ]
            if any(brand in title_lower for brand in major_brands):
                score += 0.15

            # 5. Penalize vague speculation or clickbait noise (-0.20)
            if any(w in title_lower for w in ["rumors", "speculation", "leak", "controversy"]):
                score -= 0.20

            # Normalize to 0.2 - 1.0 range
            topic["relevance_score"] = min(max(round(score, 2), 0.20), 1.00)

        # Sort descending by relevance score (highest value stories first!)
        topics.sort(key=lambda x: x["relevance_score"], reverse=True)
        return topics

    def _save_results(self, topics: list[dict]):
        """Save results to Obsidian vault and SQLite database."""
        today = datetime.now().strftime("%Y-%m-%d")
        vault_file = config.VAULT_RESEARCH / f"{today}-trends.md"

        lines = [
            f"# 🔬 Trending Topics — {today}\n",
            f"*Scanned at {datetime.now().strftime('%H:%M:%S')}*\n",
            f"**Total high-value topics found:** {len(topics)}\n",
            "---\n",
        ]

        for i, topic in enumerate(topics, 1):
            stars = "⭐" * max(1, int(topic["relevance_score"] * 5))
            lines.append(f"## {i}. {topic['title']}\n")
            lines.append(f"- **Source:** {topic['source']}")
            lines.append(f"- **Relevance Score:** {topic['relevance_score']} ({stars})")
            if topic.get("url"):
                lines.append(f"- **URL:** [{topic['url'][:60]}...]({topic['url']})")
            lines.append(f"- **Status:** 🔲 Not used\n")

        vault_file.write_text("\n".join(lines), encoding="utf-8")

        for topic in topics:
            db.log_research(
                topic=topic["title"],
                source=topic["source"],
                url=topic.get("url", ""),
                language="en",
                relevance=topic.get("relevance_score", 0.5),
            )
