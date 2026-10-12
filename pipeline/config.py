"""
AutoBlog Engine — Central Configuration

Loads settings from .env file and provides typed access
to all configuration values across the pipeline.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# ── Paths ───────────────────────────────────────────────
VAULT_PATH = Path(os.getenv("VAULT_PATH", str(PROJECT_ROOT / "vault")))
WEBSITE_PATH = Path(os.getenv("WEBSITE_PATH", str(PROJECT_ROOT / "website")))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "pipeline" / "autoblog.db")))
WEBSITE_CONTENT_PATH = WEBSITE_PATH / "src" / "content" / "blog"

# Vault subdirectories
VAULT_DRAFTS = VAULT_PATH / "Drafts"
VAULT_APPROVED = VAULT_PATH / "Approved"
VAULT_PUBLISHED = VAULT_PATH / "Published"
VAULT_RESEARCH = VAULT_PATH / "Research"
VAULT_KEYWORDS = VAULT_PATH / "Keywords"
VAULT_LOGS = VAULT_PATH / "Logs"
VAULT_IMAGES = VAULT_PATH / "Images"

# ── Site ────────────────────────────────────────────────
SITE_NAME = os.getenv("SITE_NAME", "AI & Tech Simplified")
SITE_URL = os.getenv("SITE_URL", "https://auto-blog-web-three.vercel.app")
SITE_DESCRIPTION = os.getenv("SITE_DESCRIPTION", "")

# ── Ollama (Local LLM) ─────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST", "qwen2.5:7b")

# ── Telegram ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── APIs ────────────────────────────────────────────────
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
UNSPLASH_API_KEY = os.getenv("UNSPLASH_API_KEY", "")
if "client_id=" in UNSPLASH_API_KEY:
    import urllib.parse
    _parsed = urllib.parse.urlparse(UNSPLASH_API_KEY)
    _qs = urllib.parse.parse_qs(_parsed.query)
    if "client_id" in _qs:
        UNSPLASH_API_KEY = _qs["client_id"][0]
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

# ── Schedule ────────────────────────────────────────────
SCHEDULE_RESEARCH = os.getenv("SCHEDULE_RESEARCH", "02:00")
SCHEDULE_KEYWORDS = os.getenv("SCHEDULE_KEYWORDS", "03:00")
SCHEDULE_WRITING = os.getenv("SCHEDULE_WRITING", "04:00")
SCHEDULE_IMAGES = os.getenv("SCHEDULE_IMAGES", "04:30")
SCHEDULE_DAILY_SUMMARY = os.getenv("SCHEDULE_DAILY_SUMMARY", "20:00")

# ── Content ─────────────────────────────────────────────
MAX_DAILY_DRAFTS = int(os.getenv("MAX_DAILY_DRAFTS", "8"))
LANGUAGES = [lang.strip() for lang in os.getenv("LANGUAGES", "en,hi").split(",") if lang.strip()]
PRIMARY_LANGUAGE = os.getenv("PRIMARY_LANGUAGE", "en")

# ── Git ─────────────────────────────────────────────────
GIT_REPO_URL = os.getenv("GIT_REPO_URL", "")
GIT_BRANCH = os.getenv("GIT_BRANCH", "main")
GIT_AUTO_PUSH = os.getenv("GIT_AUTO_PUSH", "true").lower() == "true"

# ── Publishing ──────────────────────────────────────────
# When AUTO_PUBLISH is true, blog posts are automatically pushed to the website
# (Astro build verified, git committed, and pushed to GitHub -> Vercel auto-deploy)
# as soon as all blogs and images are ready.
# When false, posts remain in vault/Drafts/ with draft: true for manual review in Obsidian.
AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "true").lower() in ("true", "1", "yes")

# ── Monitoring ──────────────────────────────────────────
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "900"))
MAX_FAILURES_BEFORE_ALERT = int(os.getenv("MAX_FAILURES_BEFORE_ALERT", "3"))
AUTO_HEAL_ENABLED = os.getenv("AUTO_HEAL_ENABLED", "true").lower() == "true"

# ── Image Generation ───────────────────────────────────
LOCAL_IMAGE_GEN = os.getenv("LOCAL_IMAGE_GEN", "false").lower() == "true"
COMFYUI_URL = os.getenv("COMFYUI_URL", "http://localhost:8188")


def ensure_directories():
    """Create all required directories if they don't exist."""
    dirs = [
        VAULT_DRAFTS / "en",
        VAULT_DRAFTS / "hi",
        VAULT_APPROVED / "en",
        VAULT_APPROVED / "hi",
        VAULT_PUBLISHED / "en",
        VAULT_PUBLISHED / "hi",
        VAULT_RESEARCH,
        VAULT_KEYWORDS,
        VAULT_LOGS / "daily",
        VAULT_LOGS / "weekly",
        VAULT_IMAGES,
        WEBSITE_CONTENT_PATH,
        DATABASE_PATH.parent,
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
