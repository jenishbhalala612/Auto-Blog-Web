"""
AutoBlog Engine — Publisher

Handles the approved → published workflow:
1. Watches vault/Approved/ for posts
2. Copies them to website/src/content/blog/
3. Updates frontmatter (draft: false)
4. Runs Astro build
5. Git commit + push (triggers Cloudflare deploy)
6. Moves the vault file to Published/

If a build or deploy fails, it rolls back and alerts you.
"""

import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

import config
import db

console = Console()


class Publisher:
    """Publish approved posts from the Obsidian vault to the live site."""

    def publish_approved(self) -> list[dict]:
        """Find all approved posts and publish them."""
        published = []

        for lang in config.LANGUAGES:
            approved_dir = config.VAULT_APPROVED / lang
            if not approved_dir.exists():
                continue

            for md_file in approved_dir.rglob("*.md"):
                result = self._publish_post(md_file, lang)
                if result:
                    published.append(result)

        if published:
            # Build and deploy
            build_ok = self._build_site()
            if build_ok:
                if config.GIT_AUTO_PUSH:
                    self._git_push(published)
            else:
                # Rollback on build failure
                self._rollback(published)
                console.print("[red]❌ Build failed — rolled back all changes[/]")
                db.log_error("publisher", "build_failure", "Astro build failed, changes rolled back")

        return published

    def _publish_post(self, md_file: Path, lang: str) -> dict | None:
        """Process a single approved post."""
        try:
            content = md_file.read_text(encoding="utf-8")

            # Update frontmatter: draft: true → draft: false
            content = re.sub(r"draft:\s*true", "draft: false", content)

            # Copy to website content directory
            dest = config.WEBSITE_CONTENT_PATH / md_file.name
            dest.write_text(content, encoding="utf-8")

            # Move vault file to Published/
            published_dir = config.VAULT_PUBLISHED / lang
            published_dir.mkdir(parents=True, exist_ok=True)
            published_path = published_dir / md_file.name

            # If file already exists in Published, add timestamp
            if published_path.exists():
                stem = md_file.stem
                suffix = md_file.suffix
                timestamp = datetime.now().strftime("%H%M%S")
                published_path = published_dir / f"{stem}-{timestamp}{suffix}"

            shutil.move(str(md_file), str(published_path))

            # Extract title from frontmatter for logging
            title_match = re.search(r'^title:\s*"(.+)"', content, re.MULTILINE)
            title = title_match.group(1) if title_match else md_file.stem

            # Extract slug
            slug = md_file.stem

            # Update database
            db.update_post_status(slug, "published")

            console.print(f"  📤 Published: [green]{title}[/] ({lang})")

            return {
                "title": title,
                "slug": slug,
                "language": lang,
                "file_path": str(dest),
                "vault_path": str(published_path),
            }

        except Exception as e:
            console.print(f"  [red]❌ Failed to publish {md_file.name}: {e}[/]")
            db.log_error("publisher", "publish_post", str(e))
            return None

    def _build_site(self) -> bool:
        """Run Astro build."""
        console.print("  🔨 Building Astro site...")
        try:
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(config.WEBSITE_PATH),
                capture_output=True,
                text=True,
                timeout=120,
                shell=True,
            )
            if result.returncode == 0:
                console.print("  ✅ Build successful")
                return True
            else:
                console.print(f"  [red]❌ Build failed:[/]\n{result.stderr[:500]}")
                db.log_error("publisher", "build", result.stderr[:1000])
                return False
        except subprocess.TimeoutExpired:
            console.print("  [red]❌ Build timed out (120s)[/]")
            db.log_error("publisher", "build_timeout", "Astro build exceeded 120s")
            return False
        except Exception as e:
            console.print(f"  [red]❌ Build error: {e}[/]")
            db.log_error("publisher", "build_error", str(e))
            return False

    def _git_push(self, published: list[dict]):
        """Git add, commit, and push changes."""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            titles = [p["title"][:30] for p in published]
            commit_msg = f"📝 Published {len(published)} posts ({now}): {', '.join(titles)}"

            cmds = [
                ["git", "add", "-A"],
                ["git", "commit", "-m", commit_msg],
                ["git", "push", "origin", config.GIT_BRANCH],
            ]

            for cmd in cmds:
                result = subprocess.run(
                    cmd,
                    cwd=str(config.WEBSITE_PATH),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    shell=True,
                )
                if result.returncode != 0 and "nothing to commit" not in result.stdout:
                    console.print(f"  [yellow]⚠ Git command failed: {' '.join(cmd)}[/]")
                    console.print(f"  {result.stderr[:200]}")

            console.print(f"  🚀 Pushed to {config.GIT_BRANCH}")
            db.log_pipeline_run("git_push", "success", commit_msg)

        except Exception as e:
            console.print(f"  [yellow]⚠ Git push failed: {e}[/]")
            db.log_error("publisher", "git_push", str(e))

    def _rollback(self, published: list[dict]):
        """Roll back published posts on build failure."""
        for post in published:
            try:
                # Remove from website content
                dest = Path(post["file_path"])
                if dest.exists():
                    dest.unlink()

                # Move back from Published to Approved
                vault_path = Path(post["vault_path"])
                if vault_path.exists():
                    approved_dir = config.VAULT_APPROVED / post["language"]
                    approved_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(vault_path), str(approved_dir / vault_path.name))

                # Update database
                db.update_post_status(post["slug"], "approved")

            except Exception as e:
                console.print(f"  [red]Rollback failed for {post['slug']}: {e}[/]")
