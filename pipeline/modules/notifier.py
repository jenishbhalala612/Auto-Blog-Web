"""
AutoBlog Engine — Telegram Notifier

Sends notifications to your Telegram:
- 🚨 Alerts when something breaks
- 📊 Daily/weekly summaries
- ✅ Publish confirmations
- 📝 Draft ready notifications

Uses the python-telegram-bot library (async-native).
Falls back to httpx if the library isn't available.
"""

from datetime import datetime

import httpx
from rich.console import Console

import config

console = Console()


class Notifier:
    """Send notifications via Telegram bot."""

    def __init__(self):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.client = httpx.Client(timeout=15)

        if not self.enabled:
            console.print("  [dim]Telegram not configured — notifications disabled[/]")

    def send_alert(self, message: str):
        """Send an urgent alert (errors, failures)."""
        if not self.enabled:
            console.print(f"  [red]ALERT (no Telegram):[/] {message[:100]}")
            return

        text = f"🚨 *AutoBlog Alert*\n\n{message}\n\n_{datetime.now().strftime('%Y-%m-%d %H:%M')}_"
        self._send(text)

    def send_summary(self, summary: str):
        """Send a daily/weekly summary."""
        if not self.enabled:
            console.print(f"  [cyan]SUMMARY (no Telegram):[/]\n{summary[:200]}")
            return

        self._send(summary)

    def send_publish_notification(self, title: str, url: str, language: str):
        """Notify when a post is published."""
        if not self.enabled:
            return

        lang_emoji = "🇮🇳" if language == "hi" else "🇬🇧"
        text = (
            f"✅ *Post Published!* {lang_emoji}\n\n"
            f"📝 *{title}*\n"
            f"🔗 {url}\n\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_"
        )
        self._send(text)

    def send_draft_notification(self, count: int):
        """Notify when new drafts are ready for review."""
        if not self.enabled:
            return

        text = (
            f"📝 *{count} New Drafts Ready!*\n\n"
            f"Open Obsidian to review and approve them.\n"
            f"Move approved posts to the `✅ Approved/` folder.\n\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_"
        )
        self._send(text)

    def send_pipeline_complete(
        self,
        topics: list | None = None,
        keywords: list | None = None,
        drafts: list | None = None,
        published: list | None = None,
        health: dict | None = None,
        duration: float = 0.0,
    ):
        """Send a comprehensive, detailed notification when a pipeline completes."""
        if not self.enabled:
            return

        topics = topics or []
        keywords = keywords or []
        drafts = drafts or []
        published = published or []
        health = health or {"all_ok": True}

        mins = int(duration // 60)
        secs = int(duration % 60)
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{duration:.1f}s"

        health_status = "✅ Healthy" if health.get("all_ok") else "⚠️ Issues Detected"

        lines = [
            "🚀 *AutoBlog Engine — Pipeline Run Completed!*",
            "",
            f"⏱ *Duration:* {time_str}",
            f"🩺 *System Health:* {health_status}",
            "",
            "📊 *Run Metrics:*",
            f"• 🔍 Topics Processed: *{len(topics)}*",
            f"• 🔑 Keywords Found: *{len(keywords)}*",
            f"• 📝 Drafts in Vault: *{len(drafts)}*",
            f"• 📤 Posts Published: *{len(published)}*",
        ]

        if drafts:
            lines.append("")
            lines.append("📝 *Draft Posts in Obsidian:*")
            for d in drafts[:6]:
                title = d.get("title", d.get("slug", "Untitled"))
                lang = d.get("language", "en")
                flag = "🇮🇳" if lang == "hi" else "🇬🇧"
                lines.append(f"  • {flag} {title}")
            if len(drafts) > 6:
                lines.append(f"  _...and {len(drafts) - 6} more_")

        if published:
            lines.append("")
            lines.append("📤 *Published Posts:*")
            for p in published[:5]:
                title = p.get("title", "Post")
                lines.append(f"  • {title}")

        lines.extend([
            "",
            "💡 *Next Step:* Review & edit drafts in Obsidian `Drafts/`, then move approved posts to `Approved/` to auto-publish.",
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        ])

        text = "\n".join(lines)
        self._send(text)

    def _send(self, text: str):
        """Send a message via Telegram Bot API with automatic markdown fallback."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            resp = self.client.post(url, json=payload)

            # Fall back without Markdown formatting if Telegram rejects markdown syntax
            if resp.status_code != 200:
                payload.pop("parse_mode", None)
                fallback_resp = self.client.post(url, json=payload)
                if fallback_resp.status_code != 200:
                    console.print(f"  [yellow]⚠ Telegram send failed: {fallback_resp.text[:100]}[/]")

        except Exception as e:
            console.print(f"  [yellow]⚠ Telegram error: {e}[/]")

