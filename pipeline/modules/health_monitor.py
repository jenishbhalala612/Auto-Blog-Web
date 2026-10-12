"""
AutoBlog Engine — Health Monitor

Checks the health of all system components:
1. Website uptime (HTTP check)
2. Ollama service
3. SearXNG service
4. Disk space
5. Recent pipeline errors
6. Pending drafts count
"""

import time
from pathlib import Path

import httpx
from rich.console import Console

import config
import db

console = Console()


class HealthMonitor:
    """Monitor the health of all AutoBlog services."""

    def __init__(self):
        self.client = httpx.Client(timeout=10, follow_redirects=True)

    def check_all(self) -> dict:
        """Run all health checks and return a report."""
        report = {
            "all_ok": True,
            "issues": [],
            "checks": {},
        }

        # 1. Website uptime
        self._check_website(report)

        # 2. Ollama
        self._check_ollama(report)

        # 3. SearXNG
        self._check_searxng(report)

        # 4. Disk space
        self._check_disk_space(report)

        # 5. Recent errors
        self._check_recent_errors(report)

        # 6. Pending work
        self._check_pending_work(report)

        # 7. Draft metadata integrity (detect empty keywords, blank heroImage)
        self._check_draft_metadata(report)

        # Update overall status
        report["all_ok"] = len(report["issues"]) == 0

        return report

    def _check_website(self, report: dict):
        """Check if the live website is responding."""
        check_name = "website"
        try:
            if config.SITE_URL.startswith("https://your-site"):
                report["checks"][check_name] = "skipped (no URL configured)"
                return

            start = time.time()
            resp = self.client.get(config.SITE_URL)
            response_time = (time.time() - start) * 1000

            if resp.status_code == 200:
                report["checks"][check_name] = f"ok ({response_time:.0f}ms)"
                db.log_health_check("website", "ok", response_time)
            else:
                report["checks"][check_name] = f"error (HTTP {resp.status_code})"
                report["issues"].append(f"Website returned HTTP {resp.status_code}")
                db.log_health_check("website", "error", response_time, f"HTTP {resp.status_code}")

        except Exception as e:
            report["checks"][check_name] = f"down ({e})"
            report["issues"].append(f"Website is unreachable: {e}")
            db.log_health_check("website", "down", 0, str(e))

    def _check_ollama(self, report: dict):
        """Check if Ollama is running."""
        check_name = "ollama"
        try:
            start = time.time()
            resp = self.client.get(f"{config.OLLAMA_HOST}/api/tags")
            response_time = (time.time() - start) * 1000

            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                report["checks"][check_name] = f"ok ({len(models)} models, {response_time:.0f}ms)"
                db.log_health_check("ollama", "ok", response_time, f"Models: {', '.join(model_names)}")
            else:
                report["checks"][check_name] = f"error (HTTP {resp.status_code})"
                report["issues"].append(f"Ollama returned HTTP {resp.status_code}")
                db.log_health_check("ollama", "error", response_time)

        except Exception as e:
            report["checks"][check_name] = f"down ({e})"
            report["issues"].append(f"Ollama is not running. Start it with: ollama serve")
            db.log_health_check("ollama", "down", 0, str(e))

    def _check_searxng(self, report: dict):
        """Check if SearXNG is running."""
        check_name = "searxng"
        try:
            start = time.time()
            resp = self.client.get(f"{config.SEARXNG_URL}/healthz")
            response_time = (time.time() - start) * 1000

            if resp.status_code == 200:
                report["checks"][check_name] = f"ok ({response_time:.0f}ms)"
                db.log_health_check("searxng", "ok", response_time)
            else:
                report["checks"][check_name] = f"warning (HTTP {resp.status_code})"
                db.log_health_check("searxng", "warning", response_time)

        except Exception:
            report["checks"][check_name] = "offline (optional)"
            # SearXNG is optional — don't add to issues

    def _check_disk_space(self, report: dict):
        """Check available disk space."""
        check_name = "disk_space"
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(config.VAULT_PATH))
            free_gb = free / (1024**3)
            total_gb = total / (1024**3)
            used_percent = (used / total) * 100

            if free_gb < 1:
                report["checks"][check_name] = f"critical ({free_gb:.1f} GB free)"
                report["issues"].append(f"Disk space critically low: {free_gb:.1f} GB free")
            elif free_gb < 5:
                report["checks"][check_name] = f"warning ({free_gb:.1f} GB free)"
                report["issues"].append(f"Disk space low: {free_gb:.1f} GB free")
            else:
                report["checks"][check_name] = f"ok ({free_gb:.1f} GB free, {used_percent:.0f}% used)"

            db.log_health_check("disk_space", "ok" if free_gb >= 5 else "warning", 0, f"{free_gb:.1f}GB free")

        except Exception as e:
            report["checks"][check_name] = f"error ({e})"

    def _check_recent_errors(self, report: dict):
        """Check for recent pipeline errors."""
        check_name = "recent_errors"
        errors = db.get_recent_errors(10)

        # Count errors in the last hour
        from datetime import datetime, timedelta
        one_hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
        recent = [e for e in errors if e.get("created_at", "") > one_hour_ago]

        if len(recent) > 5:
            report["checks"][check_name] = f"critical ({len(recent)} errors in last hour)"
            report["issues"].append(f"High error rate: {len(recent)} errors in the last hour")
        elif len(recent) > 0:
            report["checks"][check_name] = f"warning ({len(recent)} errors in last hour)"
        else:
            report["checks"][check_name] = "ok (no recent errors)"

    def _check_pending_work(self, report: dict):
        """Check pending drafts and research topics."""
        check_name = "pending_work"
        pending_topics = db.get_pending_topics()
        
        # Count drafts in vault
        draft_count = 0
        for lang in config.LANGUAGES:
            draft_dir = config.VAULT_DRAFTS / lang
            if draft_dir.exists():
                draft_count += len(list(draft_dir.rglob("*.md")))

        approved_count = 0
        for lang in config.LANGUAGES:
            approved_dir = config.VAULT_APPROVED / lang
            if approved_dir.exists():
                approved_count += len(list(approved_dir.rglob("*.md")))

        report["checks"][check_name] = (
            f"{len(pending_topics)} topics pending, "
            f"{draft_count} drafts awaiting review, "
            f"{approved_count} approved awaiting publish"
        )

    def _check_draft_metadata(self, report: dict):
        """Check drafts in vault for incomplete or blank metadata (empty keywords, blank heroImage)."""
        check_name = "draft_metadata"
        incomplete_drafts = []

        import re
        for lang in config.LANGUAGES:
            draft_dir = config.VAULT_DRAFTS / lang
            if not draft_dir.exists():
                continue
            for md_file in draft_dir.rglob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    # Ignore rejected drafts
                    if "rejected: true" in content or "status: rejected" in content:
                        continue
                    issues = []
                    if 'keywords: []' in content or 'keywords: ""' in content:
                        issues.append("empty keywords")
                    if 'heroImage: ""' in content or "heroImage: ''" in content:
                        issues.append("empty heroImage")
                    if issues:
                        incomplete_drafts.append(f"{md_file.name} ({', '.join(issues)})")
                except Exception:
                    pass

        if incomplete_drafts:
            report["checks"][check_name] = f"warning ({len(incomplete_drafts)} drafts incomplete)"
            for inc in incomplete_drafts[:3]:
                report["issues"].append(f"Incomplete draft metadata: {inc}")
        else:
            report["checks"][check_name] = "ok (all drafts have metadata)"
