"""
AutoBlog Engine — SQLite Database Layer

Records every action the pipeline takes:
- Research results
- Keywords found
- Drafts created
- Posts published
- Errors encountered
- Health checks

This is the system's memory. The summary generator reads from here.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

import config


def get_db_path() -> Path:
    """Get the database path, ensuring parent directory exists."""
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return config.DATABASE_PATH


@contextmanager
def get_connection():
    """Context manager for database connections."""
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database():
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
            -- Trend research results
            CREATE TABLE IF NOT EXISTS research (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                source TEXT NOT NULL,
                url TEXT,
                relevance_score REAL DEFAULT 0.0,
                language TEXT DEFAULT 'en',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used BOOLEAN DEFAULT FALSE
            );

            -- SEO keywords
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                search_volume TEXT,
                difficulty TEXT,
                language TEXT DEFAULT 'en',
                topic TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used BOOLEAN DEFAULT FALSE
            );

            -- Blog posts (drafts and published)
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                language TEXT DEFAULT 'en',
                status TEXT DEFAULT 'draft',
                keywords TEXT,
                tags TEXT,
                file_path TEXT,
                word_count INTEGER DEFAULT 0,
                publish_attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                published_at TIMESTAMP,
                updated_at TIMESTAMP
            );

            -- Pipeline run log
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                details TEXT,
                duration_seconds REAL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            -- Error log
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                stack_trace TEXT,
                auto_fixed BOOLEAN DEFAULT FALSE,
                fix_action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Health checks
            CREATE TABLE IF NOT EXISTS health_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_type TEXT NOT NULL,
                status TEXT NOT NULL,
                response_time_ms REAL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Daily summaries
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_type TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_via TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Used images tracking (prevents duplicate images across posts)
            CREATE TABLE IF NOT EXISTS used_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_url TEXT UNIQUE,
                post_slug TEXT,
                source TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Ensure schema migrations for existing databases
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(posts)")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        if "publish_attempts" not in existing_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN publish_attempts INTEGER DEFAULT 0")
        if "published_at" not in existing_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN published_at TIMESTAMP")


# ── Convenience functions ───────────────────────────────

def log_research(topic: str, source: str, url: str = "", language: str = "en", relevance: float = 0.0):
    """Log a research finding."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO research (topic, source, url, language, relevance_score) VALUES (?, ?, ?, ?, ?)",
            (topic, source, url, language, relevance),
        )


def log_keyword(keyword: str, language: str = "en", topic: str = "", volume: str = "", difficulty: str = ""):
    """Log a discovered keyword."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO keywords (keyword, language, topic, search_volume, difficulty) VALUES (?, ?, ?, ?, ?)",
            (keyword, language, topic, volume, difficulty),
        )


def log_post(title: str, slug: str, language: str, status: str, file_path: str, word_count: int = 0, keywords: str = "", tags: str = ""):
    """Log a new post."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO posts (title, slug, language, status, file_path, word_count, keywords, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, slug, language, status, file_path, word_count, keywords, tags),
        )


def get_post_by_slug(slug: str) -> dict | None:
    """Fetch a post record by slug."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM posts WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None


def is_post_published(slug: str) -> bool:
    """Check if a post has already been marked published in the database."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, published_at FROM posts WHERE slug = ?",
            (slug,),
        ).fetchone()
        if not row:
            return False
        return (row["status"] == "published") or (row["published_at"] is not None)


def increment_publish_attempts(slug: str) -> int:
    """Increment the publish failure/attempt counter for a post and return the new count."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE posts SET publish_attempts = COALESCE(publish_attempts, 0) + 1, updated_at = ? WHERE slug = ?",
            (now, slug),
        )
        row = conn.execute("SELECT publish_attempts FROM posts WHERE slug = ?", (slug,)).fetchone()
        return row["publish_attempts"] if row and row["publish_attempts"] is not None else 1


def get_publish_attempts(slug: str) -> int:
    """Get the current publish failure attempt count for a post."""
    with get_connection() as conn:
        row = conn.execute("SELECT publish_attempts FROM posts WHERE slug = ?", (slug,)).fetchone()
        return row["publish_attempts"] if row and row["publish_attempts"] is not None else 0


def reset_publish_attempts(slug: str):
    """Reset publish attempt counter back to 0."""
    with get_connection() as conn:
        conn.execute("UPDATE posts SET publish_attempts = 0 WHERE slug = ?", (slug,))


def mark_post_published(slug: str, file_path: str = ""):
    """Mark a post as published with timestamp and clear attempt count."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if file_path:
            conn.execute(
                "UPDATE posts SET status = 'published', published_at = ?, updated_at = ?, file_path = ?, publish_attempts = 0 WHERE slug = ?",
                (now, now, file_path, slug),
            )
        else:
            conn.execute(
                "UPDATE posts SET status = 'published', published_at = ?, updated_at = ?, publish_attempts = 0 WHERE slug = ?",
                (now, now, slug),
            )


def update_post_status(slug: str, status: str):
    """Update a post's status (draft → approved → published)."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if status == "approved":
            conn.execute("UPDATE posts SET status = ?, approved_at = ?, updated_at = ? WHERE slug = ?", (status, now, now, slug))
        elif status == "published":
            conn.execute("UPDATE posts SET status = ?, published_at = ?, updated_at = ?, publish_attempts = 0 WHERE slug = ?", (status, now, now, slug))
        else:
            conn.execute("UPDATE posts SET status = ?, updated_at = ? WHERE slug = ?", (status, now, slug))


def log_pipeline_run(run_type: str, status: str, message: str = "", details: str = "", duration: float = 0.0):
    """Log a pipeline run."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (run_type, status, message, details, duration_seconds, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_type, status, message, details, duration, datetime.now().isoformat()),
        )


def log_error(module: str, error_type: str, message: str, stack_trace: str = "", auto_fixed: bool = False, fix_action: str = ""):
    """Log an error."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO errors (module, error_type, message, stack_trace, auto_fixed, fix_action) VALUES (?, ?, ?, ?, ?, ?)",
            (module, error_type, message, stack_trace, auto_fixed, fix_action),
        )


def log_health_check(check_type: str, status: str, response_time_ms: float = 0.0, details: str = ""):
    """Log a health check."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO health_checks (check_type, status, response_time_ms, details) VALUES (?, ?, ?, ?)",
            (check_type, status, response_time_ms, details),
        )


def log_summary(summary_type: str, content: str, sent_via: str = ""):
    """Log a generated summary."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO summaries (summary_type, content, sent_via) VALUES (?, ?, ?)",
            (summary_type, content, sent_via),
        )


def get_today_stats() -> dict:
    """Get today's statistics for the summary."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        stats = {}
        stats["research_count"] = conn.execute(
            "SELECT COUNT(*) FROM research WHERE DATE(created_at) = ?", (today,)
        ).fetchone()[0]
        stats["keywords_found"] = conn.execute(
            "SELECT COUNT(*) FROM keywords WHERE DATE(created_at) = ?", (today,)
        ).fetchone()[0]
        stats["drafts_created"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE DATE(created_at) = ? AND status = 'draft'", (today,)
        ).fetchone()[0]
        stats["posts_published"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE DATE(published_at) = ?", (today,)
        ).fetchone()[0]
        stats["errors_today"] = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE DATE(created_at) = ?", (today,)
        ).fetchone()[0]
        stats["auto_fixed"] = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE DATE(created_at) = ? AND auto_fixed = TRUE", (today,)
        ).fetchone()[0]
        stats["health_ok"] = conn.execute(
            "SELECT COUNT(*) FROM health_checks WHERE DATE(created_at) = ? AND status = 'ok'", (today,)
        ).fetchone()[0]
        stats["health_fail"] = conn.execute(
            "SELECT COUNT(*) FROM health_checks WHERE DATE(created_at) = ? AND status != 'ok'", (today,)
        ).fetchone()[0]
        stats["total_posts"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'published'"
        ).fetchone()[0]
        return stats


def get_recent_errors(limit: int = 5) -> list:
    """Get recent errors."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM errors ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_pending_topics() -> list:
    """Get research topics not yet used for drafts."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM research WHERE used = FALSE ORDER BY relevance_score DESC LIMIT 10"
        ).fetchall()
        return [dict(row) for row in rows]


def mark_topic_used(topic_id: int):
    """Mark a research topic as used so it won't be picked up again."""
    with get_connection() as conn:
        conn.execute("UPDATE research SET used = TRUE WHERE id = ?", (topic_id,))


def mark_image_used(image_url: str, post_slug: str, source: str = "Unsplash"):
    """Record an image URL as used for a specific post."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO used_images (image_url, post_slug, source) VALUES (?, ?, ?)",
            (image_url, post_slug, source),
        )


def get_used_image_urls(days: int = 30) -> set[str]:
    """Get all image URLs used in the last N days (default: 30 days)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT image_url FROM used_images WHERE datetime(used_at) >= datetime('now', ?)",
            (f"-{days} days",),
        ).fetchall()
        return {r["image_url"] for r in rows if r["image_url"]}
