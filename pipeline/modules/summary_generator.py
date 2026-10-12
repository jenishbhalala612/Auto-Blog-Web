"""
AutoBlog Engine — Summary Generator

Generates daily and weekly reports from the SQLite database.
Reports are:
1. Sent to Telegram
2. Saved to vault/Logs/
3. Stored in the database

Daily summary: what happened today
Weekly summary: trends, growth, and recommendations
"""

from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

import config
import db

console = Console()


class SummaryGenerator:
    """Generate daily and weekly summaries of pipeline activity."""

    def generate_daily(self) -> str:
        """Generate today's summary."""
        stats = db.get_today_stats()
        errors = db.get_recent_errors(5)
        now = datetime.now()

        summary_lines = [
            f"📊 *Daily Summary — {now.strftime('%A, %B %d, %Y')}*",
            "",
            "━━━ Pipeline Activity ━━━",
            f"🔬 Topics researched: {stats['research_count']}",
            f"🔑 Keywords found: {stats['keywords_found']}",
            f"📝 Drafts created: {stats['drafts_created']}",
            f"📤 Posts published: {stats['posts_published']}",
            "",
            "━━━ System Health ━━━",
            f"✅ Health checks passed: {stats['health_ok']}",
            f"❌ Health checks failed: {stats['health_fail']}",
            f"⚠️ Errors today: {stats['errors_today']}",
            f"🔧 Auto-fixed: {stats['auto_fixed']}",
            "",
            f"📈 Total published posts: {stats['total_posts']}",
        ]

        # Add error details if any
        if errors:
            summary_lines.append("")
            summary_lines.append("━━━ Recent Errors ━━━")
            for err in errors[:3]:
                module = err.get("module", "unknown")
                msg = err.get("message", "")[:80]
                fixed = "✅ Auto-fixed" if err.get("auto_fixed") else "❌ Needs attention"
                summary_lines.append(f"• [{module}] {msg} — {fixed}")

        # Add recommendations
        summary_lines.append("")
        summary_lines.append("━━━ Recommendations ━━━")
        if stats['drafts_created'] > 0 and stats['posts_published'] == 0:
            summary_lines.append("📋 You have drafts waiting for review in Obsidian!")
        if stats['errors_today'] > 3:
            summary_lines.append("⚠️ High error count — check the logs")
        if stats['research_count'] == 0:
            summary_lines.append("🔍 No research ran today — check the scheduler")

        summary_lines.append(f"\n_Generated at {now.strftime('%H:%M')}_")

        summary = "\n".join(summary_lines)

        # Save to vault
        self._save_to_vault(summary, "daily")

        # Save to database
        db.log_summary("daily", summary)

        return summary

    def generate_weekly(self) -> str:
        """Generate a weekly summary with trends and growth."""
        now = datetime.now()
        week_start = now - timedelta(days=7)

        with db.get_connection() as conn:
            # Posts published this week
            posts_this_week = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE published_at >= ? AND status = 'published'",
                (week_start.isoformat(),)
            ).fetchone()[0]

            # Total posts all time
            total_posts = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE status = 'published'"
            ).fetchone()[0]

            # Research topics this week
            research_count = conn.execute(
                "SELECT COUNT(*) FROM research WHERE created_at >= ?",
                (week_start.isoformat(),)
            ).fetchone()[0]

            # Keywords this week
            keywords_count = conn.execute(
                "SELECT COUNT(*) FROM keywords WHERE created_at >= ?",
                (week_start.isoformat(),)
            ).fetchone()[0]

            # Errors this week
            errors_count = conn.execute(
                "SELECT COUNT(*) FROM errors WHERE created_at >= ?",
                (week_start.isoformat(),)
            ).fetchone()[0]

            # Auto-fixed this week
            auto_fixed = conn.execute(
                "SELECT COUNT(*) FROM errors WHERE created_at >= ? AND auto_fixed = TRUE",
                (week_start.isoformat(),)
            ).fetchone()[0]

            # Top topics (most researched)
            top_topics = conn.execute(
                "SELECT topic, COUNT(*) as cnt FROM research WHERE created_at >= ? GROUP BY topic ORDER BY cnt DESC LIMIT 5",
                (week_start.isoformat(),)
            ).fetchall()

        summary_lines = [
            f"📊 *Weekly Report — {week_start.strftime('%b %d')} to {now.strftime('%b %d, %Y')}*",
            "",
            "━━━ This Week ━━━",
            f"📤 Posts published: {posts_this_week}",
            f"🔬 Topics researched: {research_count}",
            f"🔑 Keywords found: {keywords_count}",
            f"⚠️ Errors: {errors_count} (Auto-fixed: {auto_fixed})",
            "",
            f"📈 *Total published (all time): {total_posts}*",
        ]

        if top_topics:
            summary_lines.append("")
            summary_lines.append("━━━ Hot Topics This Week ━━━")
            for i, topic in enumerate(top_topics, 1):
                summary_lines.append(f"{i}. {topic['topic'][:50]}")

        summary_lines.append("")
        summary_lines.append("━━━ Next Week Goals ━━━")
        target = max(posts_this_week + 1, 3)
        summary_lines.append(f"🎯 Target: {target} posts")
        summary_lines.append("📋 Review and approve pending drafts")
        summary_lines.append("🔍 Monitor indexing in Google Search Console")

        summary_lines.append(f"\n_Generated at {now.strftime('%H:%M')}_")

        summary = "\n".join(summary_lines)

        # Save to vault
        self._save_to_vault(summary, "weekly")

        # Save to database
        db.log_summary("weekly", summary)

        return summary

    def _save_to_vault(self, summary: str, summary_type: str):
        """Save summary as a markdown file in the vault."""
        now = datetime.now()
        if summary_type == "daily":
            folder = config.VAULT_LOGS / "daily"
            filename = f"{now.strftime('%Y-%m-%d')}-summary.md"
        else:
            folder = config.VAULT_LOGS / "weekly"
            filename = f"{now.strftime('%Y-W%W')}-summary.md"

        folder.mkdir(parents=True, exist_ok=True)

        # Convert Telegram markdown to vault-friendly format
        # Use placeholder to prevent chain-replacement
        clean_summary = summary.replace("*", "\x00BOLD\x00").replace("_", "*").replace("\x00BOLD\x00", "**")
        content = f"# {summary_type.capitalize()} Summary — {now.strftime('%Y-%m-%d')}\n\n{clean_summary}\n"

        (folder / filename).write_text(content, encoding="utf-8")
