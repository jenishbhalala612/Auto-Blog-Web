"""
AutoBlog Engine — Publisher Module

GOAL: When a draft in vault/Drafts/ has been approved (its frontmatter `draft: false`,
toggled manually in Obsidian after review), this module automatically:
1. Scans vault/Drafts/<lang>/<date>/*.md for approved drafts (`draft: false`)
   that are not yet published and not rejected.
2. Converts the draft into the Astro site's content collection format, saving to
   website/src/content/blog/<slug>.md. Strips internal-only audit metadata
   (source_trend, source_url, heroImageSource) for the public copy while
   preserving it in the Obsidian vault audit archive.
3. Downloads remote hero images (Unsplash/Pexels) to website/public/images/posts/
   or copies local images, updating frontmatter to the local asset path.
4. Verifies the Astro static build succeeds.
5. Stages and commits changes via Git ("Add post: <title> (<lang>)") and pushes
   to the configured GitHub branch (triggering Cloudflare Pages auto-deploy).
6. Verifies the new post is live at its public URL (HTTP 200) with retries.
7. Marks the post as published in SQLite database (autoblog.db).
8. Moves the original file from vault/Drafts/ to vault/Published/ preserving dates.
9. Sends Telegram notification confirmation.
10. Handles errors gracefully: rolls back temporary content, stops retries after
    3 failed attempts, and sends Telegram alert for manual inspection.
"""

import os
import re
import sys
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows (avoids cp1252 emoji crash)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
import yaml
from rich.console import Console

import config
import db
from modules.notifier import Notifier

console = Console(legacy_windows=False)

# Allowed frontmatter fields defined in website/src/content/config.ts
ALLOWED_ASTRO_SCHEMA_FIELDS = {
    "title",
    "description",
    "pubDate",
    "updatedDate",
    "heroImage",
    "heroImageAlt",
    "tags",
    "keywords",
    "author",
    "lang",
    "draft",
}

# Internal audit fields stripped from public Astro copy (retained in vault/Published/)
INTERNAL_AUDIT_FIELDS = {
    "source_trend",
    "source_url",
    "heroImageSource",
    "rejected",
    "rejection_reason",
    "publish_attempts",
}


class Publisher:
    """Publish approved drafts from Obsidian vault to live Astro website."""

    def __init__(self):
        self.http_client = httpx.Client(timeout=15, follow_redirects=True)
        self.notifier = Notifier()
        self.repo_root = self._resolve_git_repo_root()

    def _resolve_git_repo_root(self) -> Path:
        """Find the root of the git repository (PROJECT_ROOT or WEBSITE_PATH)."""
        if (config.PROJECT_ROOT / ".git").exists():
            return config.PROJECT_ROOT
        if (config.WEBSITE_PATH / ".git").exists():
            return config.WEBSITE_PATH
        # Default fallback to project root
        return config.PROJECT_ROOT

    # ── 1. SCAN FOR APPROVED DRAFTS ───────────────────────────────────────────

    def scan_approved_drafts(self) -> list[dict]:
        """
        Scan vault/Drafts/<lang>/<date>/*.md and vault/Drafts/<lang>/*.md.
        Select only files where `draft: false` (approved by user in Obsidian)
        AND not already marked "published" or "rejected" in the database.
        """
        approved_drafts = []

        if not config.VAULT_DRAFTS.exists():
            console.print("  [yellow]Vault Drafts directory does not exist.[/]")
            return []

        for lang in config.LANGUAGES:
            lang_dir = config.VAULT_DRAFTS / lang
            if not lang_dir.exists():
                continue

            # Find all markdown files recursively in vault/Drafts/<lang>/
            for md_file in sorted(lang_dir.rglob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    frontmatter, body = self._parse_markdown(content)

                    # 1. Check approval & readiness status
                    hero_img = str(frontmatter.get("heroImage", "")).strip()
                    has_images_ready = bool(hero_img and hero_img not in ('""', "''", "None"))
                    draft_val = frontmatter.get("draft")
                    is_explicitly_approved = (draft_val is False or str(draft_val).lower() == "false")

                    if config.AUTO_PUBLISH:
                        # Auto-publish mode: drafts are published as soon as content and images are ready
                        if not has_images_ready:
                            console.print(f"  [dim]⏳ Waiting for images before pushing: {md_file.name}[/]")
                            continue
                        if not is_explicitly_approved:
                            frontmatter["draft"] = False
                            updated_content = re.sub(r"draft:\s*true", "draft: false", content)
                            if updated_content != content:
                                md_file.write_text(updated_content, encoding="utf-8")
                            console.print(f"  ✨ Auto-publish ready: [cyan]{md_file.name}[/] (content + images ready)")
                    else:
                        # Manual approval mode: only push posts where draft: false was set manually
                        if not is_explicitly_approved:
                            continue

                    # 2. Must not be rejected
                    if frontmatter.get("rejected") is True or str(frontmatter.get("rejected", "")).lower() == "true":
                        console.print(f"  [dim]⏭️  Skipping rejected draft: {md_file.name}[/]")
                        continue

                    # 3. Determine slug
                    slug = self._determine_slug(md_file, frontmatter, lang)

                    # 4. Check database state: skip if already published or rejected in DB
                    if db.is_post_published(slug):
                        console.print(f"  [dim]⏭️  Skipping already published post: {slug}[/]")
                        continue

                    db_post = db.get_post_by_slug(slug)
                    if db_post and db_post.get("status") == "rejected":
                        console.print(f"  [dim]⏭️  Skipping DB-rejected post: {slug}[/]")
                        continue

                    # 5. Check consecutive failure attempts (Requirement 7)
                    attempts = db.get_publish_attempts(slug)
                    if attempts >= 3:
                        console.print(
                            f"  [bold red]⛔ Skipping {slug}: Failed {attempts} consecutive publish attempts. "
                            f"Manual review required.[/]"
                        )
                        continue

                    title = frontmatter.get("title", md_file.stem)
                    approved_drafts.append({
                        "file_path": md_file,
                        "slug": slug,
                        "title": title,
                        "language": lang,
                        "frontmatter": frontmatter,
                        "body": body,
                        "attempts": attempts,
                    })

                except Exception as e:
                    console.print(f"  [red]⚠ Error inspecting draft {md_file.name}: {e}[/]")
                    db.log_error("publisher", "scan_draft_error", f"{md_file.name}: {e}")

        return approved_drafts

    def _determine_slug(self, md_file: Path, frontmatter: dict, lang: str) -> str:
        """Derive the canonical post slug matching database or clean URL."""
        if frontmatter.get("slug"):
            return str(frontmatter["slug"]).strip()

        # Check if database has a matching record by file_path
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT slug FROM posts WHERE file_path = ? OR file_path LIKE ?",
                (str(md_file), f"%{md_file.name}"),
            ).fetchone()
            if row:
                return row["slug"]

        # Strip YYYY-MM-DD- date prefix from stem if present
        stem = md_file.stem
        clean_slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        return clean_slug

    # ── 2. CONVERT TO ASTRO CONTENT FORMAT ────────────────────────────────────

    def _convert_for_astro(self, draft: dict, local_hero_path: str) -> tuple[str, Path]:
        """
        Convert draft to Astro content format:
        - Destination: website/src/content/blog/<slug>.md
        - Strips internal audit fields (source_trend, source_url, heroImageSource)
        - Preserves all Astro schema fields with draft: false
        """
        fm = dict(draft["frontmatter"])
        slug = draft["slug"]
        lang = draft["language"]

        # Strip internal audit fields for public site
        for audit_field in INTERNAL_AUDIT_FIELDS:
            fm.pop(audit_field, None)

        # Set Astro required / standardized fields
        fm["draft"] = False
        fm["lang"] = lang
        if local_hero_path:
            fm["heroImage"] = local_hero_path

        # Format pubDate cleanly (YYYY-MM-DD)
        pub_date = fm.get("pubDate")
        if isinstance(pub_date, datetime):
            fm["pubDate"] = pub_date.strftime("%Y-%m-%d")
        elif not pub_date:
            fm["pubDate"] = datetime.now().strftime("%Y-%m-%d")
        else:
            fm["pubDate"] = str(pub_date)[:10]

        # Only retain fields expected or allowed by Astro content schema
        public_fm = {}
        for k in list(fm.keys()):
            if k in ALLOWED_ASTRO_SCHEMA_FIELDS:
                public_fm[k] = fm[k]

        # Re-assemble markdown file
        dest_path = config.WEBSITE_CONTENT_PATH / f"{slug}.md"
        dest_content = self._serialize_markdown(public_fm, draft["body"])
        return dest_content, dest_path

    # ── 3. HANDLE HERO IMAGE ──────────────────────────────────────────────────

    def _process_hero_image(self, hero_image: str, slug: str, title: str) -> tuple[str, list[Path]]:
        """
        Handle hero image:
        - If remote URL (Unsplash/Pexels), download to website/public/images/posts/<today>/<slug>/
        - If local path (ComfyUI / existing), ensure file is copied into website/public/
        - Returns (web_path_for_frontmatter, list_of_new_image_files_to_git_stage)
        """
        if not hero_image or not hero_image.strip():
            return "", []

        hero_image = hero_image.strip()
        today = datetime.now().strftime("%Y-%m-%d")
        staged_images = []

        # Case A: Remote URL (Unsplash, Pexels, etc.)
        if hero_image.startswith(("http://", "https://")):
            console.print(f"    📥 Downloading remote hero image: [cyan]{hero_image[:60]}...[/]")
            parsed = urllib.parse.urlparse(hero_image)
            ext = Path(parsed.path).suffix.lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"]:
                ext = ".jpg"

            rel_web_dir = f"images/posts/{today}/{slug}"
            web_post_dir = config.WEBSITE_PATH / "public" / rel_web_dir
            web_post_dir.mkdir(parents=True, exist_ok=True)
            img_file = web_post_dir / f"hero{ext}"

            resp = self.http_client.get(hero_image)
            if resp.status_code == 200:
                img_file.write_bytes(resp.content)
                staged_images.append(img_file)
                web_path = f"/{rel_web_dir}/hero{ext}".replace("\\", "/")
                console.print(f"    ✅ Saved hero image locally to: [dim]{web_path}[/]")
                return web_path, staged_images
            else:
                raise RuntimeError(f"HTTP {resp.status_code} downloading hero image: {hero_image}")

        # Case B: Local path (ComfyUI generation or vault image)
        clean_path = hero_image.lstrip("/\\")
        public_dest = config.WEBSITE_PATH / "public" / clean_path

        # If already in website/public, just make sure path is standard
        if public_dest.exists() and public_dest.is_file():
            web_path = f"/{clean_path}".replace("\\", "/")
            staged_images.append(public_dest)
            return web_path, staged_images

        # Check potential local source files in Vault
        candidates = [
            config.VAULT_PATH / clean_path,
            config.VAULT_IMAGES / clean_path,
            config.PROJECT_ROOT / clean_path,
        ]
        img_filename = Path(clean_path).name
        # Also check VAULT_IMAGES by filename search
        vault_matches = list(config.VAULT_IMAGES.rglob(img_filename))
        if vault_matches:
            candidates.insert(0, vault_matches[0])

        for src in candidates:
            if src.exists() and src.is_file():
                public_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, public_dest)
                staged_images.append(public_dest)
                web_path = f"/{clean_path}".replace("\\", "/")
                console.print(f"    ✅ Copied local hero image from vault: [dim]{web_path}[/]")
                return web_path, staged_images

        # If it already follows standard /images/posts/ route structure
        if hero_image.startswith("/images/"):
            return hero_image, []

        return hero_image, []

    # ── 4. ASTRO BUILD VERIFICATION ───────────────────────────────────────────

    def _verify_astro_build(self) -> bool:
        """Run Astro build to guarantee site compiles without errors."""
        console.print("  🔨 Running Astro verification build...")
        try:
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(config.WEBSITE_PATH),
                capture_output=True,
                text=True,
                timeout=180,
                shell=True,
            )
            if result.returncode == 0:
                console.print("  ✅ Astro build passed")
                return True
            else:
                err = result.stderr or result.stdout
                console.print(f"  [bold red]❌ Astro build failed:[/]\n{err[:400]}")
                db.log_error("publisher", "astro_build_failed", err[:1000])
                return False
        except subprocess.TimeoutExpired:
            console.print("  [bold red]❌ Astro build timed out (180s)[/]")
            db.log_error("publisher", "astro_build_timeout", "Astro build exceeded 180s")
            return False
        except Exception as e:
            console.print(f"  [bold red]❌ Astro build error: {e}[/]")
            db.log_error("publisher", "astro_build_error", str(e))
            return False

    # ── 5. GIT COMMIT AND PUSH ────────────────────────────────────────────────

    def _git_stage_commit_push(self, staged_files: list[Path], title: str, lang: str):
        """
        Stage new content and image files in git, commit, and push to GitHub.
        If git push fails, raises an error so post is NOT marked as published.
        """
        repo_root = self.repo_root
        console.print(f"  📦 Staging {len(staged_files)} file(s) in git ({repo_root.name})...")

        for f in staged_files:
            try:
                rel = f.relative_to(repo_root)
            except ValueError:
                rel = f
            subprocess.run(
                ["git", "add", str(rel)],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
                text=True,
            )

        # Check git status for changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            console.print("  [dim]Git working tree clean (no new changes to commit).[/]")
            return

        commit_msg = f"Add post: {title} ({lang})"
        commit_res = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if commit_res.returncode != 0:
            err = commit_res.stderr or commit_res.stdout
            raise RuntimeError(f"Git commit failed: {err}")

        console.print(f"  📝 Git commit created: [green]\"{commit_msg}\"[/]")
        commit_created = True

        # Push to remote branch
        branch = config.GIT_BRANCH or "main"
        if config.GIT_AUTO_PUSH:
            console.print(f"  🚀 Running git push origin {branch}...")
            push_res = subprocess.run(
                ["git", "push", "origin", branch],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=90,
            )
            # Display actual git push command output
            combined_output = (push_res.stdout + ("\n" if push_res.stdout and push_res.stderr else "") + push_res.stderr).strip()
            if combined_output:
                console.print(f"    [dim cyan]Git push output:[/]\n    [dim]{combined_output}[/]")

            if push_res.returncode != 0:
                err = push_res.stderr.strip() or push_res.stdout.strip()
                db.log_error("publisher", "git_push_failed", f"Failed pushing to {branch}: {err}")
                raise RuntimeError(f"Git push failed (exit code {push_res.returncode}): {err}")
            console.print(f"  ✅ Pushed successfully to origin/{branch}")
            db.log_pipeline_run("git_push", "success", commit_msg)
        else:
            console.print("  [dim]ℹ️ Git auto-push disabled (GIT_AUTO_PUSH=false in .env) — commit kept locally[/]")

        return commit_created

    # ── 6. VERIFY POST WENT LIVE ──────────────────────────────────────────────

    def _verify_post_live(self, live_url: str, max_retries: int = 3, initial_delay: int = 5) -> bool:
        """
        Verify post's expected live URL returns HTTP 200.
        If it doesn't within retries, logs a warning but doesn't hard-fail
        (Cloudflare Pages builds can take a minute or two).
        """
        console.print(f"  🌐 Verifying live URL: [cyan]{live_url}[/]")
        if initial_delay > 0:
            time.sleep(initial_delay)

        final_status = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.http_client.get(live_url, timeout=10.0, follow_redirects=True)
                final_status = f"HTTP {resp.status_code}"
                if resp.status_code == 200:
                    console.print(f"  ✅ Live verification confirmed on attempt {attempt}: [green]{live_url}[/] (HTTP 200)")
                    db.log_health_check("post_live_verification", "ok", details=live_url)
                    return True
                else:
                    console.print(f"    [yellow]Retry {attempt}/{max_retries}: HTTP {resp.status_code}[/]")
            except Exception as e:
                final_status = f"{type(e).__name__}: {e}"
                console.print(f"    [yellow]Retry {attempt}/{max_retries}: {e}[/]")

            if attempt < max_retries:
                time.sleep(5)

        # Flag warning for health-check monitor
        console.print(
            f"  ⚠️  Post not yet live at [yellow]{live_url}[/] after {max_retries} retries "
            f"(Final status: {final_status}). Flagged for health-check."
        )
        db.log_health_check("post_live_verification", "warning", details=f"Pending live confirmation: {live_url} ({final_status})")
        return False

    # ── 7. EXECUTE PUBLISH PIPELINE FOR APPROVED POSTS ────────────────────────

    def publish_approved(self) -> list[dict]:
        """
        Scan for approved posts, convert, verify, commit, push, update DB,
        move vault files to Published/, and send Telegram notifications.
        """
        approved = self.scan_approved_drafts()
        if not approved:
            console.print("  [dim]No approved drafts awaiting publication (`draft: false`).[/]")
            return []

        console.print(f"\n[bold green]🚀 Found {len(approved)} approved post(s) to publish![/]\n")
        published_posts = []

        for draft in approved:
            result = self.publish_single_post(draft)
            if result:
                published_posts.append(result)

        return published_posts

    def publish_single_post(self, draft: dict) -> dict | None:
        """Process and publish a single draft with full error handling and rollback."""
        file_path: Path = draft["file_path"]
        slug: str = draft["slug"]
        title: str = draft["title"]
        lang: str = draft["language"]

        console.print(f"\n[bold cyan]━━━ Publishing: {title} ({lang}) ━━━[/]")
        created_files: list[Path] = []
        commit_created = False

        try:
            # 1. Process hero image (download remote URL or copy local asset)
            orig_hero = draft["frontmatter"].get("heroImage", "")
            local_hero_path, image_files = self._process_hero_image(orig_hero, slug, title)
            created_files.extend(image_files)

            # 2. Convert to Astro content format
            astro_content, dest_file = self._convert_for_astro(draft, local_hero_path)
            config.WEBSITE_CONTENT_PATH.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(astro_content, encoding="utf-8")
            created_files.append(dest_file)
            console.print(f"  📄 Converted content saved to: [green]{dest_file.name}[/]")

            # 3. Astro build verification
            build_ok = self._verify_astro_build()
            if not build_ok:
                raise RuntimeError("Astro build verification failed for new content.")

            # 4. Git commit & push
            commit_created = self._git_stage_commit_push(created_files, title, lang)

            # 5. Live URL verification
            base_site = config.SITE_URL.rstrip("/")
            expected_live_url = f"{base_site}/{lang}/{slug}/"
            self._verify_post_live(expected_live_url)

            # 6. Database state update
            db.mark_post_published(slug, file_path=str(dest_file))
            console.print(f"  💾 Database updated: [green]{slug}[/] status='published'")

            # 7. Move file from vault/Drafts/ to vault/Published/
            published_vault_file = self._move_to_published(file_path)
            console.print(f"  📁 Moved draft to audit archive: [dim]{published_vault_file}[/]")

            # 8. Send Telegram notification
            self._send_publish_alert(title, lang, expected_live_url)

            post_record = {
                "title": title,
                "slug": slug,
                "language": lang,
                "live_url": expected_live_url,
                "astro_file": str(dest_file),
                "vault_file": str(published_vault_file),
            }
            console.print(f"[bold green]✅ Successfully published: {title} ({lang})[/]")
            return post_record

        except Exception as e:
            # Error handling: Rollback created files and git commit, increment failure attempts, alert user
            console.print(f"  [bold red]❌ Failed to publish '{title}': {e}[/]")
            self._rollback(created_files, commit_created=commit_created)

            attempts = db.increment_publish_attempts(slug)
            db.log_error("publisher", "publish_failed", f"Failed to publish {slug} (attempt {attempts}): {e}")

            if attempts >= 3:
                alert_text = (
                    f"⚠️ *Publish Failure Alert* ⚠️\n\n"
                    f"Post: *{title}* ({lang})\n"
                    f"Slug: `{slug}`\n"
                    f"Failed *3 consecutive times* during publication.\n\n"
                    f"Error: `{e}`\n\n"
                    f"Automatic retries have been paused. Please check the draft in Obsidian."
                )
                self.notifier.send_alert(alert_text)
                console.print(f"  [bold red]🚨 Alert sent to Telegram: 3 consecutive publish failures for '{title}'[/]")

            return None

    def _move_to_published(self, md_file: Path) -> Path:
        """
        Move draft from vault/Drafts/<rel_path> to vault/Published/<rel_path>.
        Preserves language and date directory structure.
        """
        try:
            rel = md_file.relative_to(config.VAULT_DRAFTS)
        except ValueError:
            rel = Path(md_file.name)

        dest_published = config.VAULT_PUBLISHED / rel
        dest_published.parent.mkdir(parents=True, exist_ok=True)

        if dest_published.exists():
            stem = md_file.stem
            suffix = md_file.suffix
            timestamp = datetime.now().strftime("%H%M%S")
            dest_published = dest_published.parent / f"{stem}-{timestamp}{suffix}"

        shutil.move(str(md_file), str(dest_published))
        return dest_published

    def _send_publish_alert(self, title: str, lang: str, live_url: str):
        """Send formatted publish confirmation via Telegram."""
        if not self.notifier.enabled:
            return

        lang_flag = "🇮🇳" if lang == "hi" else "🇬🇧"
        msg = (
            f"✅ *Published:* {title} ({lang}) {lang_flag}\n"
            f"🔗 {live_url}"
        )
        self.notifier._send(msg)

    def _rollback(self, created_files: list[Path], commit_created: bool = False):
        """
        Remove any newly created content or image files upon failure,
        and undo the local git commit if one was created before the push failed.
        """
        if commit_created:
            try:
                res = subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    console.print("  [yellow]⏪ Undid local git commit (git reset --hard HEAD~1)[/]")
                else:
                    console.print(f"  [red]Failed to undo git commit: {res.stderr}[/]")
            except Exception as e:
                console.print(f"  [red]Error resetting git commit: {e}[/]")

        for f in created_files:
            try:
                if f.exists() and f.is_file():
                    f.unlink()
                    console.print(f"  [yellow]Rolled back created file: {f.name}[/]")
            except Exception as e:
                console.print(f"  [red]Failed to delete rollback file {f}: {e}[/]")

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _parse_markdown(self, content: str) -> tuple[dict, str]:
        """Split and parse YAML frontmatter and markdown body."""
        if not content.startswith("---"):
            return {}, content

        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content

        fm_text = parts[1]
        body = parts[2].lstrip("\r\n")

        try:
            data = yaml.safe_load(fm_text) or {}
        except Exception:
            # Fallback regex parsing if yaml fails
            data = {}
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if v.lower() == "false":
                        data[k] = False
                    elif v.lower() == "true":
                        data[k] = True
                    else:
                        data[k] = v

        return data, body

    def _serialize_markdown(self, frontmatter: dict, body: str) -> str:
        """Format frontmatter and markdown body cleanly for Astro."""
        # Clean YAML output with standard key order
        yaml_lines = ["---"]
        for k, v in frontmatter.items():
            if isinstance(v, (list, tuple)):
                items = [f'"{str(i).replace(chr(34), chr(39))}"' for i in v]
                yaml_lines.append(f"{k}: [{', '.join(items)}]")
            elif isinstance(v, bool):
                yaml_lines.append(f"{k}: {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                yaml_lines.append(f"{k}: {v}")
            else:
                s = str(v).replace('"', "'")
                yaml_lines.append(f'{k}: "{s}"')
        yaml_lines.append("---\n")
        return "\n".join(yaml_lines) + "\n" + body.lstrip("\r\n")
