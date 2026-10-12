"""
AutoBlog Engine — SearXNG Client

A dedicated client for the self-hosted SearXNG instance.
Provides:
  - Web search (JSON API)        → for trend research
  - News search                  → for trending topics
  - Autocomplete                 → for keyword suggestions
  - Trending keyword discovery   → combined multi-query search

SearXNG must be running at the URL configured in SEARXNG_URL (.env).
Start it with:  docker-compose up -d searxng
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Literal

import httpx
from rich.console import Console

import config

console = Console()

# ── Types ────────────────────────────────────────────────────────────────────

Category = Literal["general", "news", "it", "social media", "videos"]


@dataclass
class SearchResult:
    """A single result returned by SearXNG."""
    title: str
    url: str
    content: str = ""
    engine: str = ""
    score: float = 0.0
    category: str = "general"
    published: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SearchResult":
        return cls(
            title=data.get("title", ""),
            url=data.get("url", ""),
            content=data.get("content", ""),
            engine=", ".join(data.get("engines", [])),
            score=data.get("score", 0.0),
            category=data.get("category", "general"),
            published=data.get("publishedDate", ""),
        )


@dataclass
class SearchResponse:
    """Full response from a SearXNG search."""
    query: str
    results: list[SearchResult] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    number_of_results: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and len(self.results) > 0


# ── Client ────────────────────────────────────────────────────────────────────

class SearXNGClient:
    """
    HTTP client for the self-hosted SearXNG JSON API.

    Usage:
        client = SearXNGClient()
        if not client.is_available():
            print("SearXNG offline -- skipping web search")
        else:
            results = client.search("best AI tools 2026")
            for r in results.results:
                print(r.title, r.url)
    """

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or config.SEARXNG_URL).rstrip("/")
        self.client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "AutoBlog-Pipeline/1.0 (self-hosted SearXNG client)",
                "Accept": "application/json",
            },
        )
        self._available: bool | None = None  # cached

    # ── Availability ─────────────────────────────────────────────────────────

    def is_available(self, force_check: bool = False) -> bool:
        """Check if SearXNG is reachable (cached after first call)."""
        if self._available is not None and not force_check:
            return self._available
        try:
            resp = self.client.get(f"{self.base_url}/healthz", timeout=5)
            self._available = resp.status_code == 200
        except Exception:
            # Some SearXNG versions don't have /healthz -- try the root
            try:
                resp = self.client.get(self.base_url, timeout=5)
                self._available = resp.status_code in (200, 302)
            except Exception:
                self._available = False
        return self._available

    # ── Core Search ──────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        category: Category = "general",
        language: str = "en",
        page: int = 1,
        max_results: int = 10,
    ) -> SearchResponse:
        """
        Perform a web search via SearXNG JSON API.

        Args:
            query:       The search query string.
            category:    "general" | "news" | "it" | "social media" | "videos"
            language:    ISO 639-1 language code (e.g. "en", "hi")
            page:        Result page number (1-indexed)
            max_results: Maximum results to return (client-side trim)

        Returns:
            SearchResponse with .results list and .suggestions list.
        """
        if not self.is_available():
            return SearchResponse(query=query, error="SearXNG not available")

        params = {
            "q": query,
            "categories": category,
            "language": language,
            "pageno": page,
            "format": "json",
            "engines": self._engines_for_category(category),
        }

        try:
            resp = self.client.get(f"{self.base_url}/search", params=params)
            resp.raise_for_status()
            data = resp.json()

            results = [
                SearchResult.from_dict(r)
                for r in data.get("results", [])[:max_results]
            ]
            suggestions = data.get("suggestions", [])
            n_results = data.get("number_of_results", len(results))

            return SearchResponse(
                query=query,
                results=results,
                suggestions=suggestions,
                number_of_results=n_results,
            )

        except httpx.HTTPStatusError as e:
            error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            console.print(f"    [yellow]SearXNG search error: {error}[/]")
            return SearchResponse(query=query, error=error)
        except Exception as e:
            console.print(f"    [yellow]SearXNG search failed: {e}[/]")
            return SearchResponse(query=query, error=str(e))

    def search_news(self, query: str, language: str = "en", max_results: int = 10) -> SearchResponse:
        """Search for recent news articles about a topic."""
        return self.search(query, category="news", language=language, max_results=max_results)

    def search_tech(self, query: str, max_results: int = 10) -> SearchResponse:
        """Search within tech/IT category (GitHub, StackOverflow, etc.)."""
        return self.search(query, category="it", max_results=max_results)

    # ── Autocomplete ─────────────────────────────────────────────────────────

    def autocomplete(self, query: str) -> list[str]:
        """
        Get autocomplete suggestions from SearXNG.
        Returns a list of query suggestion strings.
        """
        if not self.is_available():
            return []
        try:
            resp = self.client.get(
                f"{self.base_url}/autocompleter",
                params={"q": query},
                timeout=10,
            )
            suggestions = resp.json()
            if isinstance(suggestions, list):
                # SearXNG returns [query, [suggestions]] or just [suggestions]
                if suggestions and isinstance(suggestions[0], list):
                    return suggestions[1][:10]
                return [s for s in suggestions if isinstance(s, str)][:10]
        except Exception:
            pass
        return []

    # ── Trending Topic Discovery ──────────────────────────────────────────────

    def discover_trending(
        self,
        seed_queries: list[str] | None = None,
        language: str = "en",
        max_per_query: int = 8,
    ) -> list[dict]:
        """
        Discover trending topics by searching multiple seed queries.

        Runs both 'general' and 'news' searches for each seed query,
        deduplicates, and returns enriched topic dicts.

        Args:
            seed_queries:  List of seed search terms. Defaults to AI/tech topics.
            language:      Search language.
            max_per_query: Max results per query.

        Returns:
            List of topic dicts with keys: title, url, content, source, published.
        """
        if not self.is_available():
            console.print("    [dim]SearXNG not available -- skipping web search discovery[/]")
            return []

        if seed_queries is None:
            seed_queries = _DEFAULT_SEED_QUERIES

        topics = []
        seen_urls: set[str] = set()

        for query in seed_queries:
            # News search (most relevant for trending)
            news_resp = self.search_news(query, language=language, max_results=max_per_query)
            for r in news_resp.results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    topics.append({
                        "title": r.title,
                        "url": r.url,
                        "content": r.content[:300],
                        "source": f"SearXNG News ({r.engine or query})",
                        "published": r.published,
                        "search_query": query,
                    })

            # General web search
            web_resp = self.search(query, category="general", language=language, max_results=max_per_query // 2)
            for r in web_resp.results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    topics.append({
                        "title": r.title,
                        "url": r.url,
                        "content": r.content[:300],
                        "source": f"SearXNG Web ({r.engine or query})",
                        "published": r.published,
                        "search_query": query,
                    })

            time.sleep(1)  # polite delay between queries

        return topics

    def discover_keywords(
        self,
        topic: str,
        language: str = "en",
        max_results: int = 8,
    ) -> list[dict]:
        """
        Discover keywords by searching for a topic and extracting
        autocomplete suggestions + related search suggestions.

        Args:
            topic:       The topic to research keywords for.
            language:    Language code.
            max_results: Max keywords to return.

        Returns:
            List of keyword dicts: {keyword, source, language, topic}
        """
        keywords: list[dict] = []
        seen: set[str] = set()

        def _add(kw: str, src: str):
            kw = kw.strip()
            if kw and kw.lower() not in seen and len(kw) > 4:
                seen.add(kw.lower())
                keywords.append({
                    "keyword": kw,
                    "source": src,
                    "language": language,
                    "topic": topic,
                })

        # 1. Autocomplete suggestions
        for suggestion in self.autocomplete(topic):
            _add(suggestion, "SearXNG Autocomplete")

        # 2. Search suggestions returned in JSON response
        resp = self.search(topic, category="general", language=language, max_results=5)
        for s in resp.suggestions:
            _add(s, "SearXNG Suggestions")

        # 3. "How to" variant
        how_resp = self.search(f"how to {topic}", category="general", language=language, max_results=3)
        for s in how_resp.suggestions:
            _add(s, "SearXNG Suggestions")

        # 4. Title mining from results (extract meaningful titles)
        for r in resp.results[:5]:
            if r.title and len(r.title) > 10 and len(r.title) < 80:
                _add(r.title, "SearXNG Title Mining")

        return keywords[:max_results]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _engines_for_category(category: Category) -> str:
        """Return a comma-separated list of engines for a category."""
        engine_map = {
            "general": "google,bing,duckduckgo,brave",
            "news": "google news,bing news,hackernews",
            "it": "github,stackoverflow,hackernews",
            "social media": "reddit",
            "videos": "youtube",
        }
        return engine_map.get(category, "google,bing,duckduckgo")

    def close(self):
        """Close the underlying HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Default Seed Queries ──────────────────────────────────────────────────────

_DEFAULT_SEED_QUERIES = [
    # AI & LLM news
    "latest AI tools 2026",
    "new LLM models released",
    "artificial intelligence news today",
    "ChatGPT Claude Gemini updates",

    # Developer & tech
    "best free developer tools 2026",
    "open source software releases",
    "programming tutorial trending",

    # Specific niches
    "AI automation tools for business",
    "machine learning tutorial beginners",
    "tech startup news this week",
]
