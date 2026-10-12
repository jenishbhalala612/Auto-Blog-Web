"""
AutoBlog Engine — Scheduler

Uses APScheduler to run pipeline jobs at configured times.
All schedules are defined in .env and loaded via config.py.
"""

from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console

import config
import db

console = Console()


def start_scheduler():
    """Start the APScheduler with all configured jobs."""
    scheduler = BlockingScheduler()

    # Parse time strings like "02:00" into hour and minute
    def parse_time(time_str: str) -> tuple:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])

    # ── Research job ────────────────────────────────────
    rh, rm = parse_time(config.SCHEDULE_RESEARCH)
    scheduler.add_job(
        _job_research,
        CronTrigger(hour=rh, minute=rm),
        id="research",
        name="Trend & Topic Research",
        misfire_grace_time=3600,
    )

    # ── Keyword research ────────────────────────────────
    kh, km = parse_time(config.SCHEDULE_KEYWORDS)
    scheduler.add_job(
        _job_keywords,
        CronTrigger(hour=kh, minute=km),
        id="keywords",
        name="SEO Keyword Research",
        misfire_grace_time=3600,
    )

    # ── Content writing ─────────────────────────────────
    wh, wm = parse_time(config.SCHEDULE_WRITING)
    scheduler.add_job(
        _job_writing,
        CronTrigger(hour=wh, minute=wm),
        id="writing",
        name="Content Writing",
        misfire_grace_time=3600,
    )

    # ── Image handling ──────────────────────────────────
    ih, im = parse_time(config.SCHEDULE_IMAGES)
    scheduler.add_job(
        _job_images,
        CronTrigger(hour=ih, minute=im),
        id="images",
        name="Image Processing",
        misfire_grace_time=3600,
    )

    # ── Publish check (every 30 minutes) ────────────────
    scheduler.add_job(
        _job_publish,
        CronTrigger(minute="*/30"),
        id="publish",
        name="Publish Approved Posts",
        misfire_grace_time=600,
    )

    # ── Health check (every 15 minutes) ─────────────────
    scheduler.add_job(
        _job_health,
        CronTrigger(minute="*/15"),
        id="health",
        name="Health Monitor",
        misfire_grace_time=300,
    )

    # ── Daily summary ───────────────────────────────────
    sh, sm = parse_time(config.SCHEDULE_DAILY_SUMMARY)
    scheduler.add_job(
        _job_daily_summary,
        CronTrigger(hour=sh, minute=sm),
        id="daily_summary",
        name="Daily Summary",
        misfire_grace_time=3600,
    )

    # ── Weekly summary (Sunday) ─────────────────────────
    weekly_minute = (sm + 30) % 60
    weekly_hour = sh if sm + 30 < 60 else (sh + 1) % 24
    scheduler.add_job(
        _job_weekly_summary,
        CronTrigger(day_of_week="sun", hour=weekly_hour, minute=weekly_minute),
        id="weekly_summary",
        name="Weekly Summary",
        misfire_grace_time=3600,
    )

    console.print("\n[bold green]📅 Scheduler started![/] Jobs configured:\n")
    for job in scheduler.get_jobs():
        console.print(f"  • [cyan]{job.name}[/] — next run: {getattr(job, 'next_run_time', 'Unknown')}")

    console.print("\n[dim]Press Ctrl+C to stop.[/]\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/]")
        scheduler.shutdown()


# ── Job wrappers (catch errors so scheduler keeps running) ──

def _safe_run(job_name: str, func):
    """Wrapper that catches errors and logs them."""
    import time
    import traceback
    from modules.notifier import Notifier

    start = time.time()
    try:
        console.print(f"\n[bold cyan]▶ Running: {job_name}[/] ({datetime.now().strftime('%H:%M:%S')})")
        func()
        duration = time.time() - start
        db.log_pipeline_run(job_name, "success", "", "", duration)
        console.print(f"[green]  ✅ {job_name} completed in {duration:.1f}s[/]")
    except Exception as e:
        duration = time.time() - start
        error_msg = str(e)
        stack = traceback.format_exc()
        console.print(f"[red]  ❌ {job_name} failed: {error_msg}[/]")
        db.log_error("scheduler", type(e).__name__, f"{job_name}: {error_msg}", stack)
        db.log_pipeline_run(job_name, "error", error_msg, stack, duration)
        try:
            notifier = Notifier()
            notifier.send_alert(f"❌ Scheduled job '{job_name}' failed!\n\n{error_msg}")
        except Exception:
            pass  # Don't let notification failure crash the scheduler


def _job_research():
    from modules.trend_scanner import TrendScanner
    def run():
        scanner = TrendScanner()
        scanner.scan()
    _safe_run("research", run)


def _job_keywords():
    from modules.keyword_researcher import KeywordResearcher
    def run():
        topics = db.get_pending_topics()
        researcher = KeywordResearcher()
        researcher.research(topics)
    _safe_run("keywords", run)


def _job_writing():
    from modules.content_writer import ContentWriter
    from modules.keyword_researcher import KeywordResearcher
    def run():
        topics = db.get_pending_topics()
        if not topics:
            console.print("  [yellow]No pending topics to write about.[/]")
            return
        researcher = KeywordResearcher()
        keywords = researcher.research(topics[:config.MAX_DAILY_DRAFTS])
        writer = ContentWriter()
        writer.write_drafts(topics[:config.MAX_DAILY_DRAFTS], keywords)
    _safe_run("writing", run)


def _job_images():
    from modules.image_handler import ImageHandler
    from modules.publisher import Publisher
    def run():
        handler = ImageHandler()
        handler.process_pending_drafts()
        if config.AUTO_PUBLISH:
            console.print("  ✨ Auto-publish: checking for ready posts to push to website...")
            publisher = Publisher()
            publisher.publish_approved()
    _safe_run("images", run)


def _job_publish():
    from modules.publisher import Publisher
    def run():
        publisher = Publisher()
        publisher.publish_approved()
    _safe_run("publish", run)


def _job_health():
    from modules.health_monitor import HealthMonitor
    from modules.self_healer import SelfHealer
    from modules.notifier import Notifier
    def run():
        monitor = HealthMonitor()
        health = monitor.check_all()
        if not health["all_ok"]:
            notifier = Notifier()
            healer = SelfHealer(notifier)
            healer.heal(health)
    _safe_run("health_check", run)


def _job_daily_summary():
    from modules.summary_generator import SummaryGenerator
    from modules.notifier import Notifier
    def run():
        generator = SummaryGenerator()
        summary = generator.generate_daily()
        notifier = Notifier()
        notifier.send_summary(summary)
    _safe_run("daily_summary", run)


def _job_weekly_summary():
    from modules.summary_generator import SummaryGenerator
    from modules.notifier import Notifier
    def run():
        generator = SummaryGenerator()
        summary = generator.generate_weekly()
        notifier = Notifier()
        notifier.send_summary(summary)
    _safe_run("weekly_summary", run)
