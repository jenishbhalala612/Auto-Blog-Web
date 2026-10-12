"""
AutoBlog Engine — Main Entry Point

Usage:
  python main.py                 # Run full pipeline once
  python main.py --schedule      # Start scheduler (runs daily)
  python main.py --research      # Research only
  python main.py --search        # SearXNG web search test (new)
  python main.py --write         # Draft posts only
  python main.py --publish       # Publish approved posts only
  python main.py --health        # Run health check
  python main.py --summary       # Generate and send summary
"""

import sys
import time
import argparse
import traceback
from datetime import datetime

# Force UTF-8 output on Windows (avoids cp1252 emoji crash)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import config
import db
from modules.trend_scanner import TrendScanner
from modules.keyword_researcher import KeywordResearcher
from modules.content_writer import ContentWriter
from modules.image_handler import ImageHandler
from modules.publisher import Publisher
from modules.health_monitor import HealthMonitor
from modules.notifier import Notifier
from modules.summary_generator import SummaryGenerator
from modules.self_healer import SelfHealer
from modules.searxng_client import SearXNGClient
from modules.status_tracker import StatusTracker, get_status

console = Console()


def banner():
    """Display the startup banner."""
    console.print(Panel.fit(
        "[bold bright_magenta]⚡ AutoBlog Engine[/]\n"
        f"[dim]AI & Tech Simplified — Pipeline v1.0[/]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/]",
        border_style="bright_blue",
    ))


def run_full_pipeline(resume=False):
    """Run the complete pipeline: research → keywords → write → images → check approved → publish."""
    start_time = time.time()
    notifier = Notifier()
    healer = SelfHealer(notifier)
    tracker = StatusTracker("full")

    starting_step = 1
    if resume:
        status = get_status()
        if status.get("status") == "done":
            console.print("[bold green]Pipeline already completed successfully. Nothing to resume.[/]")
            return
        elif status.get("status") in ("running", "error"):
            starting_step = status.get("current_step", 1)
            tracker.pipeline_resume(starting_step, status.get("counters", {}), status.get("log_lines", []))
            console.print(f"[bold yellow]Resuming pipeline from Step {starting_step}...[/]")
        else:
            tracker.pipeline_start()
    else:
        tracker.pipeline_start()

    try:
        # Default empty lists for summary if steps are skipped
        topics = []
        keywords = []
        drafts = []
        published = []
        health = {"all_ok": True}

        if starting_step <= 1:
            console.print("\n[bold cyan]━━━ Step 1/6: Trend Research ━━━[/]")
            tracker.step_start("Trend Research", step=1)
            scanner = TrendScanner()
            topics = scanner.scan()
            tracker.step_done("Trend Research", topics_found=len(topics))
            console.print(f"  ✅ Found [green]{len(topics)}[/] trending topics")
        else:
            topics = db.get_pending_topics()
            for t in topics:
                t["title"] = t.get("topic", "")

        if starting_step <= 2:
            console.print("\n[bold cyan]━━━ Step 2/6: Keyword Research ━━━[/]")
            tracker.step_start("Keyword Research", step=2)
            kw_researcher = KeywordResearcher()
            keywords = kw_researcher.research(topics)
            tracker.step_done("Keyword Research", keywords_found=len(keywords))
            console.print(f"  ✅ Found [green]{len(keywords)}[/] keywords")
        else:
            with db.get_connection() as conn:
                rows = conn.execute("SELECT keyword, topic FROM keywords WHERE DATE(created_at) = DATE('now')").fetchall()
                keywords = [dict(row) for row in rows]

        if starting_step <= 3:
            console.print("\n[bold cyan]━━━ Step 3/6: Content Writing ━━━[/]")
            tracker.step_start("Content Writing", step=3)
            writer = ContentWriter()
            drafts = writer.write_drafts(topics[:config.MAX_DAILY_DRAFTS], keywords)
            tracker.step_done("Content Writing", drafts_created=len(drafts))
            console.print(f"  ✅ Created [green]{len(drafts)}[/] drafts")
        else:
            with db.get_connection() as conn:
                rows = conn.execute("SELECT title, slug, file_path, language FROM posts WHERE status = 'draft'").fetchall()
                drafts = [dict(row) for row in rows]

        if starting_step <= 4:
            console.print("\n[bold cyan]━━━ Step 4/6: Image Handling ━━━[/]")
            tracker.step_start("Image Handling", step=4)
            img_handler = ImageHandler()
            for draft in drafts:
                img_handler.add_image(draft)
            tracker.step_done("Image Handling")
            console.print(f"  ✅ Images processed")

        if starting_step <= 5:
            console.print("\n[bold cyan]━━━ Step 5/6: Publishing Approved Posts ━━━[/]")
            tracker.step_start("Publishing", step=5)
            publisher = Publisher()
            published = publisher.publish_approved()
            tracker.step_done("Publishing", posts_published=len(published))
            console.print(f"  ✅ Published [green]{len(published)}[/] posts")

        if starting_step <= 6:
            console.print("\n[bold cyan]━━━ Step 6/6: Health Check ━━━[/]")
            tracker.step_start("Health Check", step=6)
            monitor = HealthMonitor()
            health = monitor.check_all()
            tracker.step_done("Health Check")
            if health["all_ok"]:
                console.print("  ✅ All systems healthy")
            else:
                console.print("  ⚠️  Issues found — attempting auto-fix")
                healer.heal(health)

        duration = time.time() - start_time
        db.log_pipeline_run("full", "success", f"Completed in {duration:.1f}s", "", duration)
        tracker.pipeline_done()

        # Show summary table
        _print_run_summary(topics, keywords, drafts, published, health, duration)

        # Send detailed pipeline completion notification to Telegram
        notifier.send_pipeline_complete(
            topics=topics,
            keywords=keywords,
            drafts=drafts,
            published=published,
            health=health,
            duration=duration,
        )

    except Exception as e:
        duration = time.time() - start_time
        error_msg = str(e)
        stack = traceback.format_exc()
        console.print(f"\n[bold red]❌ Pipeline failed: {error_msg}[/]")
        db.log_error("main", type(e).__name__, error_msg, stack)
        db.log_pipeline_run("full", "error", error_msg, stack, duration)
        tracker.pipeline_error(error_msg)
        notifier.send_alert(f"❌ Pipeline failed!\n\n{error_msg}\n\nDuration: {duration:.1f}s")


def run_research_only():
    """Run only the trend + keyword research step."""
    console.print("[bold cyan]Running research only...[/]")
    scanner = TrendScanner()
    topics = scanner.scan()
    kw_researcher = KeywordResearcher()
    keywords = kw_researcher.research(topics)
    console.print(f"✅ Found {len(topics)} topics and {len(keywords)} keywords")


def run_searxng_search(query: str | None = None):
    """Test the SearXNG integration: search for a query and display results."""
    from rich.table import Table

    client = SearXNGClient()
    console.print(f"\n[bold cyan]SearXNG URL:[/] {client.base_url}")

    if not client.is_available():
        console.print(
            "[bold red]❌ SearXNG is not running.[/]\n"
            "[dim]Start it with: [cyan]docker-compose up -d searxng[/][/]"
        )
        return

    console.print("[bold green]✅ SearXNG is online![/]\n")

    # Use a default demo query if none provided
    q = query or "best AI tools 2026"

    # ── General web search ────────────────────────────────────────────────────
    console.print(f"[bold]Searching:[/] [cyan]{q}[/] (general)")
    resp = client.search(q, category="general", max_results=8)
    if resp.ok:
        table = Table(title=f"Web Results: {q}", border_style="bright_blue")
        table.add_column("#", style="dim", width=3)
        table.add_column("Title", style="white")
        table.add_column("Engine", style="cyan", width=20)
        table.add_column("URL", style="dim")
        for i, r in enumerate(resp.results, 1):
            table.add_row(str(i), r.title[:60], r.engine[:20], r.url[:50])
        console.print(table)
    else:
        console.print(f"  [yellow]No results: {resp.error}[/]")

    # ── News search ───────────────────────────────────────────────────────────
    console.print(f"\n[bold]Searching:[/] [cyan]{q}[/] (news)")
    news_resp = client.search_news(q, max_results=5)
    if news_resp.ok:
        for r in news_resp.results:
            console.print(f"  [green]•[/] {r.title[:80]}")
            console.print(f"    [dim]{r.url[:70]}[/]")
    else:
        console.print(f"  [yellow]No news results[/]")

    # ── Autocomplete ──────────────────────────────────────────────────────────
    suggestions = client.autocomplete(q)
    if suggestions:
        console.print(f"\n[bold]Autocomplete suggestions:[/]")
        for s in suggestions:
            console.print(f"  [dim]•[/] {s}")

    console.print("\n[bold green]SearXNG search test complete![/]")


def run_write_only():
    """Write drafts from pending research topics."""
    console.print("[bold cyan]Writing drafts...[/]")
    topics = db.get_pending_topics()
    if not topics:
        console.print("[yellow]No pending topics to write about.[/]")
        return
    writer = ContentWriter()
    kw_researcher = KeywordResearcher()
    keywords = kw_researcher.research(topics[:config.MAX_DAILY_DRAFTS])
    drafts = writer.write_drafts(topics[:config.MAX_DAILY_DRAFTS], keywords)
    img_handler = ImageHandler()
    for draft in drafts:
        img_handler.add_image(draft)
    console.print(f"✅ Created {len(drafts)} drafts with images")


def run_publish_only():
    """Publish any approved posts."""
    console.print("[bold cyan]Publishing approved posts...[/]")
    publisher = Publisher()
    published = publisher.publish_approved()
    console.print(f"✅ Published {len(published)} posts")


def run_health_check():
    """Run health checks and report."""
    console.print("[bold cyan]Running health check...[/]")
    monitor = HealthMonitor()
    health = monitor.check_all()
    if health["all_ok"]:
        console.print("[bold green]✅ All systems healthy[/]")
    else:
        console.print("[bold red]⚠️  Issues detected:[/]")
        for issue in health.get("issues", []):
            console.print(f"  • {issue}")


def run_summary():
    """Generate and send daily summary."""
    console.print("[bold cyan]Generating summary...[/]")
    generator = SummaryGenerator()
    summary = generator.generate_daily()
    notifier = Notifier()
    notifier.send_summary(summary)
    console.print("✅ Summary sent to Telegram")


def _print_run_summary(topics, keywords, drafts, published, health, duration):
    """Print a nice summary table after a full run."""
    table = Table(title="Pipeline Run Summary", border_style="bright_blue")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Topics found", str(len(topics)))
    table.add_row("Keywords found", str(len(keywords)))
    table.add_row("Drafts created", str(len(drafts)))
    table.add_row("Posts published", str(len(published)))
    table.add_row("Health status", "✅ OK" if health["all_ok"] else "⚠️ Issues")
    table.add_row("Duration", f"{duration:.1f}s")
    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="AutoBlog Engine — Pipeline")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted pipeline run")
    parser.add_argument("--schedule", action="store_true", help="Start the scheduler")
    parser.add_argument("--research", action="store_true", help="Run research only")
    parser.add_argument("--search", metavar="QUERY", nargs="?", const="", help="Test SearXNG (optional query)")
    parser.add_argument("--write", action="store_true", help="Write drafts only")
    parser.add_argument("--publish", action="store_true", help="Publish approved only")
    parser.add_argument("--health", action="store_true", help="Run health check")
    parser.add_argument("--summary", action="store_true", help="Generate summary")
    parser.add_argument("--monitor", action="store_true", help="Start the monitoring dashboard (default port 5000)")
    parser.add_argument("--monitor-port", type=int, default=5000, metavar="PORT", help="Dashboard port (default: 5000)")
    args = parser.parse_args()

    banner()

    # Initialize
    config.ensure_directories()
    db.init_database()
    console.print("[dim]Database initialized. Directories ready.[/]\n")

    if args.monitor:
        from monitor import start_server
        console.print(f"\n[bold cyan]Starting monitoring dashboard on port {args.monitor_port}...[/]")
        start_server(args.monitor_port)
    elif args.schedule:
        from scheduler import start_scheduler
        start_scheduler()
    elif args.research:
        run_research_only()
    elif args.search is not None:
        run_searxng_search(args.search if args.search else None)
    elif args.write:
        run_write_only()
    elif args.publish:
        run_publish_only()
    elif args.health:
        run_health_check()
    elif args.summary:
        run_summary()
    else:
        run_full_pipeline(resume=args.resume)


if __name__ == "__main__":
    main()
