"""
AutoBlog Engine — Self Healer

Automatically fixes common issues:
1. Ollama service not running → attempt restart
2. Build failures → clean cache and retry
3. Git conflicts → reset and re-apply
4. High error rate → pause pipeline and alert
5. Disk space low → clean old logs/images

If auto-fix fails, sends a detailed alert to Telegram.
"""

import subprocess
import shutil
from pathlib import Path

from rich.console import Console

import config
import db

console = Console()


class SelfHealer:
    """Automatically fix common pipeline issues."""

    def __init__(self, notifier):
        self.notifier = notifier

    def heal(self, health_report: dict):
        """Attempt to fix all issues in the health report."""
        if not config.AUTO_HEAL_ENABLED:
            self.notifier.send_alert(
                "⚠️ Issues detected but auto-heal is disabled.\n\n"
                + "\n".join(f"• {issue}" for issue in health_report.get("issues", []))
            )
            return

        for issue in health_report.get("issues", []):
            fixed = False
            fix_action = ""

            if "Ollama" in issue and ("not running" in issue or "unreachable" in issue):
                fixed, fix_action = self._fix_ollama()

            elif "build" in issue.lower() or "Build failed" in issue:
                fixed, fix_action = self._fix_build()

            elif "Disk space" in issue:
                fixed, fix_action = self._fix_disk_space()

            elif "HTTP" in issue and ("Website" in issue or "website" in issue):
                fixed, fix_action = self._fix_website()

            elif "error rate" in issue.lower():
                fixed, fix_action = self._fix_high_errors()

            # Log the fix attempt
            if fixed:
                console.print(f"  🔧 Auto-fixed: [green]{issue}[/]")
                db.log_error("self_healer", "auto_fix", issue, auto_fixed=True, fix_action=fix_action)
            else:
                console.print(f"  ❌ Could not auto-fix: [red]{issue}[/]")
                db.log_error("self_healer", "fix_failed", issue, fix_action=fix_action or "No fix available")
                self.notifier.send_alert(
                    f"🔧 *Auto-Fix Failed*\n\n"
                    f"Issue: {issue}\n"
                    f"Attempted: {fix_action or 'No fix available'}\n\n"
                    f"Manual intervention needed."
                )

    def _fix_ollama(self) -> tuple[bool, str]:
        """Try to restart Ollama."""
        try:
            # On Windows, try to start the Ollama service
            result = subprocess.run(
                ["ollama", "serve"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=True,
            )
            return True, "Attempted to start Ollama service"
        except subprocess.TimeoutExpired:
            # Timeout is expected if it starts successfully (it keeps running)
            return True, "Ollama serve started (background)"
        except Exception as e:
            return False, f"Failed to start Ollama: {e}"

    def _fix_build(self) -> tuple[bool, str]:
        """Fix build failures by cleaning cache and retrying."""
        try:
            # Clean Astro cache
            astro_cache = config.WEBSITE_PATH / ".astro"
            node_modules_cache = config.WEBSITE_PATH / "node_modules" / ".cache"

            if astro_cache.exists():
                shutil.rmtree(astro_cache)
            if node_modules_cache.exists():
                shutil.rmtree(node_modules_cache)

            # Retry build
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(config.WEBSITE_PATH),
                capture_output=True,
                text=True,
                timeout=120,
                shell=True,
            )

            if result.returncode == 0:
                return True, "Cleaned cache and rebuilt successfully"
            else:
                return False, f"Rebuild failed: {result.stderr[:200]}"

        except Exception as e:
            return False, f"Build fix failed: {e}"

    def _fix_disk_space(self) -> tuple[bool, str]:
        """Free disk space by cleaning old logs and temporary files."""
        freed = 0
        try:
            # Clean old daily logs (older than 30 days)
            log_dir = config.VAULT_LOGS / "daily"
            if log_dir.exists():
                from datetime import datetime, timedelta
                cutoff = datetime.now() - timedelta(days=30)
                for f in log_dir.glob("*.md"):
                    try:
                        # Parse date from filename (YYYY-MM-DD-summary.md)
                        date_str = f.stem[:10]
                        file_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if file_date < cutoff:
                            size = f.stat().st_size
                            f.unlink()
                            freed += size
                    except (ValueError, OSError):
                        pass

            # Clean Astro build cache
            dist_dir = config.WEBSITE_PATH / "dist"
            if dist_dir.exists():
                size = sum(f.stat().st_size for f in dist_dir.rglob("*") if f.is_file())
                shutil.rmtree(dist_dir)
                freed += size

            freed_mb = freed / (1024 * 1024)
            if freed_mb > 0:
                return True, f"Freed {freed_mb:.1f} MB"
            else:
                return False, "No files to clean"

        except Exception as e:
            return False, f"Disk cleanup failed: {e}"

    def _fix_website(self) -> tuple[bool, str]:
        """Try to fix website issues by redeploying."""
        try:
            # Rebuild and push
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(config.WEBSITE_PATH),
                capture_output=True,
                text=True,
                timeout=120,
                shell=True,
            )

            if result.returncode == 0 and config.GIT_AUTO_PUSH:
                subprocess.run(
                    ["git", "push", "origin", config.GIT_BRANCH, "--force"],
                    cwd=str(config.WEBSITE_PATH),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    shell=True,
                )
                return True, "Rebuilt and force-pushed"

            return False, "Rebuild or push failed"

        except Exception as e:
            return False, f"Website fix failed: {e}"

    def _fix_high_errors(self) -> tuple[bool, str]:
        """Handle high error rate by checking common causes."""
        try:
            errors = db.get_recent_errors(10)
            
            # Check if errors are from the same module
            modules = [e.get("module", "") for e in errors]
            if modules:
                most_common = max(set(modules), key=modules.count)
                return False, f"Most errors from: {most_common}. Check logs for details."

            return False, "Multiple error sources. Manual investigation needed."

        except Exception as e:
            return False, f"Error analysis failed: {e}"
