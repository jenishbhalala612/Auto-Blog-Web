"""
AutoBlog Engine — Image Handler

Handles images for blog posts:
1. Fetch free stock photos from Pexels API (if key provided)
2. Fetch high-res stock photos from Unsplash API (default/primary)
3. Local AI image generation via ComfyUI (optional, if enabled)

Images are saved into organized folders:
- Obsidian Vault: vault/Images/YYYY-MM-DD/<blog-title>/
- Website: website/public/images/posts/YYYY-MM-DD/<slug>/

Generates 3 to 4 images per blog post, sets the first as heroImage,
and embeds the additional images into the article content.
"""

import os
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from rich.console import Console

import config
import db

console = Console()


class ImageHandler:
    """Handle blog post images — stock photos and folder organization."""

    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True)
        self.images_dir = config.WEBSITE_PATH / "public" / "images" / "posts"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.vault_images_dir = config.VAULT_IMAGES
        self.vault_images_dir.mkdir(parents=True, exist_ok=True)

    def add_image(self, draft: dict) -> bool:
        """Add hero image and content images to a draft post (at least 3 to 4 images)."""
        title = draft.get("title", "")
        slug = draft.get("slug", "")
        file_path = Path(draft.get("file_path", ""))

        if not file_path.exists():
            return False

        content = file_path.read_text(encoding="utf-8")
        hero_match = re.search(r'heroImage:\s*["\']([^"\']+)["\']', content)
        if hero_match and hero_match.group(1).strip():
            console.print(f"    ⏩ Skipping images for: [dim]{title[:40]}[/] (already has heroImage)")
            if config.AUTO_PUBLISH and "draft: true" in content:
                content = re.sub(r'draft:\s*true', 'draft: false', content)
                file_path.write_text(content, encoding="utf-8")
                db.update_post_status(slug, "approved")
                console.print(f"        ✨ Auto-publish: images already present — marked ready to push (`draft: false`)")
            return True

        today = datetime.now().strftime("%Y-%m-%d")

        # Sanitize title for folder name in Obsidian
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
        lang = draft.get("language", "")
        blog_folder_name = safe_title[:60].strip() if safe_title else slug
        if (lang == "hi" or "-hi" in slug) and not blog_folder_name.endswith("-hi"):
            blog_folder_name = f"{blog_folder_name}-hi"

        # Create destination folders:
        # 1. Vault folder: vault/Images/YYYY-MM-DD/<blog-title>/
        vault_post_dir = self.vault_images_dir / today / blog_folder_name
        vault_post_dir.mkdir(parents=True, exist_ok=True)

        # 2. Website folder: website/public/images/posts/YYYY-MM-DD/<slug>/
        web_post_dir = self.images_dir / today / slug
        web_post_dir.mkdir(parents=True, exist_ok=True)

        web_url_base = f"/images/posts/{today}/{slug}"

        # Extract specific search query from topic + top 2-3 keywords
        query = self._prepare_search_query(title, file_path, draft)
        console.print(f"    🔎 Image search query: [cyan]\"{query}\"[/]")

        # 1. Fetch candidate images from stock APIs (request 10-15 results for variety)
        candidates = []
        if config.PEXELS_API_KEY:
            candidates.extend(self._fetch_pexels(query, count=15))

        if config.UNSPLASH_API_KEY:
            candidates.extend(self._fetch_unsplash(query, count=15))

        # 2. Used images tracking: filter out any image used in the last 30 days
        used_urls = db.get_used_image_urls(days=30)
        fresh_candidates = []
        for c in candidates:
            if c["url"] in used_urls:
                console.print(f"    [yellow]⏭️  Skipping already-used image: {c.get('source', c['url'][:40])}[/]")
            else:
                fresh_candidates.append(c)

        # 3. If fresh candidates are insufficient, try a secondary keyword-specific query
        if len(fresh_candidates) < 4:
            alt_query = self._prepare_alternative_query(title, file_path)
            if alt_query and alt_query != query:
                console.print(f"    🔎 Expanding image search: [cyan]\"{alt_query}\"[/]")
                alt_candidates = []
                if config.PEXELS_API_KEY:
                    alt_candidates.extend(self._fetch_pexels(alt_query, count=15))
                if config.UNSPLASH_API_KEY:
                    alt_candidates.extend(self._fetch_unsplash(alt_query, count=15))

                for c in alt_candidates:
                    if c["url"] in used_urls:
                        console.print(f"    [yellow]⏭️  Skipping already-used alternative image: {c.get('source', c['url'][:40])}[/]")
                    elif c["url"] not in {x["url"] for x in fresh_candidates}:
                        fresh_candidates.append(c)

        # 4. Try local ComfyUI if enabled
        if not fresh_candidates and config.LOCAL_IMAGE_GEN:
            local_candidate = self._generate_local(title, slug, query)
            if local_candidate:
                fresh_candidates = [local_candidate]

        # 5. Fallback: if all results were used, take least-recently used or fallback query
        if not fresh_candidates and candidates:
            fresh_candidates = candidates[:4]
        elif not fresh_candidates:
            fallback_query = f"{title[:30]} technology modern digital"
            if config.UNSPLASH_API_KEY:
                fresh_candidates = self._fetch_unsplash(fallback_query, count=10)
            elif config.PEXELS_API_KEY:
                fresh_candidates = self._fetch_pexels(fallback_query, count=10)

        if not fresh_candidates:
            console.print(f"    [yellow]⚠ No images found for: {title[:40]}[/]")
            return False

        # Ensure in-article diversity: deduplicate by photographer / visual series
        selected_images = []
        seen_photographers = set()
        seen_urls = set()

        # First pass: pick candidates from distinct photographers / sets
        for cand in fresh_candidates:
            url = cand.get("url", "")
            source = cand.get("source", "")
            # Extract photographer name from source string
            photog = source.split("Photographer:")[-1].strip(" )") if "Photographer:" in source else source

            if url not in seen_urls:
                if photog not in seen_photographers or len(seen_photographers) >= len(fresh_candidates):
                    selected_images.append(cand)
                    seen_urls.add(url)
                    seen_photographers.add(photog)
                    if len(selected_images) == 4:
                        break

        # Fallback if strict photographer diversity left fewer than 4 candidates
        if len(selected_images) < 4:
            for cand in fresh_candidates:
                if cand.get("url") not in seen_urls:
                    selected_images.append(cand)
                    seen_urls.add(cand.get("url"))
                    if len(selected_images) == 4:
                        break

        hero_meta = selected_images[0]
        hero_source = hero_meta.get("source", "Unsplash Stock")

        # Download each image and save to both website and Obsidian vault
        saved_web_paths = []
        for idx, img_item in enumerate(selected_images):
            img_url = img_item["url"]
            try:
                img_filename = f"image-{idx + 1}.jpg"
                web_file = web_post_dir / img_filename
                vault_file = vault_post_dir / img_filename

                resp = self.client.get(img_url)
                if resp.status_code == 200:
                    web_file.write_bytes(resp.content)
                    shutil.copy2(web_file, vault_file)
                    saved_web_paths.append(f"{web_url_base}/{img_filename}")
                    # Record in DB tracking table to prevent future duplicates
                    db.mark_image_used(img_url, slug, img_item.get("source", "Stock"))
            except Exception as e:
                console.print(f"    [yellow]⚠ Failed to save image {idx + 1}: {e}[/]")

        if not saved_web_paths:
            return False

        # Update draft post: set heroImage, heroImageSource, and embed extra images
        hero_image = saved_web_paths[0]
        self._update_post_with_images(
            file_path,
            hero_image,
            saved_web_paths[1:],
            title=title,
            hero_image_source=hero_source,
        )

        console.print(
            f"    🖼️  Saved [green]{len(saved_web_paths)}[/] distinct images for: [cyan]{slug[:35]}[/]\n"
            f"        Source: [dim]{hero_source}[/]"
        )
        if config.AUTO_PUBLISH:
            db.update_post_status(slug, "approved")
            console.print(f"        ✨ Auto-publish: images ready — '{slug}' marked approved (`draft: false`)")
        return True

    def process_pending_drafts(self):
        """Process all drafts that don't have images yet."""
        for lang in config.LANGUAGES:
            draft_dir = config.VAULT_DRAFTS / lang
            if not draft_dir.exists():
                continue
            for md_file in draft_dir.rglob("*.md"):
                content = md_file.read_text(encoding="utf-8")
                # Check if heroImage is empty
                if 'heroImage: ""' in content or "heroImage: ''" in content or 'heroImage:' not in content:
                    title_match = re.search(r'^title:\s*"(.+)"', content, re.MULTILINE)
                    if title_match:
                        title = title_match.group(1)
                        slug = md_file.stem
                        draft = {"title": title, "slug": slug, "file_path": str(md_file)}
                        self.add_image(draft)

    def _prepare_search_query(self, title: str, file_path: Path, draft: dict | None = None) -> str:
        """Extract a topic-specific query combining the core subject and top 2-3 keywords."""
        stopwords = {
            "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with",
            "about", "how", "why", "what", "is", "are", "you", "your", "best", "free",
            "guide", "tips", "tutorial", "top", "new", "released", "launches"
        }

        # 1. Clean title words
        title_words = [
            w for w in re.findall(r"[A-Za-z0-9]+", title)
            if w.lower() not in stopwords and len(w) > 2
        ]

        # 2. Extract keywords from draft dict or frontmatter
        kws = []
        if draft and draft.get("keywords"):
            kws = draft["keywords"] if isinstance(draft["keywords"], list) else [draft["keywords"]]
        elif file_path and file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                kw_match = re.search(r"keywords:\s*\[(.*?)\]", content)
                if kw_match:
                    kws = re.findall(r'"([^"]+)"', kw_match.group(1))
            except Exception:
                pass

        # Clean top keywords
        cleaned_kw_words = []
        for kw in kws[:2]:
            for w in re.findall(r"[A-Za-z0-9]+", kw):
                if w.lower() not in stopwords and w.lower() not in [x.lower() for x in title_words]:
                    cleaned_kw_words.append(w)

        # Combine: core title (up to 3 words) + distinct keyword terms (up to 2 words)
        combined_words = title_words[:3] + cleaned_kw_words[:2]
        if len(combined_words) >= 2:
            base_q = " ".join(combined_words)
            if any(term in base_q.lower() for term in ["ai", "security", "code", "model", "software", "research", "scan", "developer"]):
                return f"{base_q} computer"
            return base_q

        # Fallback to title words if available
        if title_words:
            return " ".join(title_words[:4])

        return "artificial intelligence software modern technology"

    def _prepare_alternative_query(self, title: str, file_path: Path) -> str:
        """Extract an alternative secondary query from frontmatter keywords for image variety."""
        if file_path and file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                kw_match = re.search(r"keywords:\s*\[(.*?)\]", content)
                if kw_match:
                    kws = re.findall(r'"([^"]+)"', kw_match.group(1))
                    if len(kws) > 1:
                        words = [w for w in re.findall(r"[A-Za-z0-9]+", kws[1]) if len(w) > 2]
                        if words:
                            return " ".join(words[:4])
            except Exception:
                pass
        return ""

    def _fetch_unsplash(self, query: str, count: int = 15) -> list[dict]:
        """Fetch high-resolution stock photo candidates from Unsplash API."""
        key = config.UNSPLASH_API_KEY
        if not key:
            return []

        try:
            url = "https://api.unsplash.com/search/photos"
            params = {
                "query": query,
                "per_page": min(count, 30),
                "orientation": "landscape",
                "client_id": key,
            }
            resp = self.client.get(url, params=params)
            if resp.status_code != 200:
                console.print(f"    [yellow]⚠ Unsplash returned status {resp.status_code}[/]")
                return []

            data = resp.json()
            results = data.get("results", [])
            items = []
            for r in results:
                raw_url = r.get("urls", {}).get("regular", "")
                photo_id = r.get("id", "")
                photographer = r.get("user", {}).get("name", "Unsplash Contributor")
                if raw_url:
                    items.append({
                        "url": raw_url,
                        "id": photo_id,
                        "source": f"Unsplash: {photo_id} (Photographer: {photographer})",
                    })
            return items
        except Exception as e:
            console.print(f"    [yellow]⚠ Unsplash search failed: {e}[/]")
            db.log_error("image_handler", "unsplash", str(e))
            return []

    def _fetch_pexels(self, query: str, count: int = 15) -> list[dict]:
        """Fetch free stock photo candidates from Pexels API."""
        if not config.PEXELS_API_KEY:
            return []

        try:
            url = "https://api.pexels.com/v1/search"
            headers = {"Authorization": config.PEXELS_API_KEY}
            params = {
                "query": query,
                "per_page": min(count, 30),
                "orientation": "landscape",
                "size": "medium",
            }
            resp = self.client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            photos = data.get("photos", [])
            items = []
            for p in photos:
                raw_url = p.get("src", {}).get("large", "")
                photo_id = str(p.get("id", ""))
                photographer = p.get("photographer", "Pexels Contributor")
                if raw_url:
                    items.append({
                        "url": raw_url,
                        "id": photo_id,
                        "source": f"Pexels: {photo_id} (Photographer: {photographer})",
                    })
            return items
        except Exception as e:
            console.print(f"    [yellow]⚠ Pexels failed: {e}[/]")
            db.log_error("image_handler", "pexels", str(e))
            return []

    def _generate_local(self, title: str, slug: str, query: str = "") -> dict | None:
        """Generate an image locally using ComfyUI with topic-specific prompt and randomized seed."""
        import random
        try:
            url = f"{config.COMFYUI_URL}/prompt"
            seed = random.randint(1, 2147483647)
            prompt = {
                "prompt": (
                    f"Professional editorial blog header image about {title}, "
                    f"featuring {query}, modern tech aesthetic, clean composition, "
                    f"minimalist dark background, vibrant accent colors, 16:9 aspect ratio"
                ),
                "seed": seed,
            }
            resp = self.client.post(url, json=prompt)
            if resp.status_code == 200:
                console.print(f"    🎨 Local image generation requested for: {slug} (Seed: {seed})")
                return {
                    "url": f"/images/posts/generated/{slug}.jpg",
                    "id": f"comfyui-{seed}",
                    "source": f"ComfyUI (Local Generation, Seed: {seed})",
                }
        except Exception:
            pass
        return None

    def _update_post_with_images(
        self,
        file_path: Path,
        hero_image: str,
        extra_images: list[str],
        title: str = "",
        hero_image_source: str = "",
    ):
        """Update frontmatter heroImage, heroImageSource, and embed extra images into article body."""
        content = file_path.read_text(encoding="utf-8")

        # Update heroImage frontmatter
        if 'heroImage: ""' in content:
            content = content.replace('heroImage: ""', f'heroImage: "{hero_image}"')
        elif "heroImage: ''" in content:
            content = content.replace("heroImage: ''", f'heroImage: "{hero_image}"')
        else:
            content = re.sub(r'heroImage:\s*"?[^"\n]*"?', f'heroImage: "{hero_image}"', content)

        # Update or add heroImageSource for audit trail
        if hero_image_source:
            if "heroImageSource:" in content:
                content = re.sub(r'heroImageSource:\s*"?[^"\n]*"?', f'heroImageSource: "{hero_image_source}"', content)
            else:
                content = content.replace(
                    f'heroImage: "{hero_image}"',
                    f'heroImage: "{hero_image}"\nheroImageSource: "{hero_image_source}"'
                )

        # Auto-publish setting: mark draft ready once images are attached
        if config.AUTO_PUBLISH:
            content = re.sub(r'draft:\s*true', 'draft: false', content)

        # Embed extra images (2, 3, 4) into the article body
        if extra_images and not any(img in content for img in extra_images):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                body = parts[2]

                # Split body by H2 subheadings
                sections = re.split(r"(\n##\s+[^\n]+)", body)
                new_body = []
                img_idx = 0

                for i, part in enumerate(sections):
                    new_body.append(part)
                    # When we finish a text section following a subheading, add an image
                    if i > 1 and not part.startswith("\n## ") and img_idx < len(extra_images):
                        img_path = extra_images[img_idx]
                        clean_alt = title.replace('"', "") if title else "Illustration"
                        new_body.append(f"\n\n![{clean_alt}]({img_path})\n\n")
                        img_idx += 1

                # If any images remain, append before conclusion or at bottom
                while img_idx < len(extra_images):
                    img_path = extra_images[img_idx]
                    clean_alt = title.replace('"', "") if title else "Illustration"
                    new_body.append(f"\n\n![{clean_alt}]({img_path})\n\n")
                    img_idx += 1

                content = f"---{frontmatter}---" + "".join(new_body)

        file_path.write_text(content, encoding="utf-8")
