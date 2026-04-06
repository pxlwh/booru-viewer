"""Download manager and local file cache."""

from __future__ import annotations

import hashlib
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from .config import cache_dir, thumbnails_dir, USER_AGENT

# Track all outgoing connections: {host: [timestamp, ...]}
_connection_log: OrderedDict[str, list[str]] = OrderedDict()


def log_connection(url: str) -> None:
    host = urlparse(url).netloc
    if host not in _connection_log:
        _connection_log[host] = []
    _connection_log[host].append(datetime.now().strftime("%H:%M:%S"))
    # Keep last 50 entries per host
    _connection_log[host] = _connection_log[host][-50:]


def get_connection_log() -> dict[str, list[str]]:
    return dict(_connection_log)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# Shared httpx client for connection pooling (avoids per-request TLS handshakes)
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client(referer: str = "") -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/*,video/*,*/*",
            },
            follow_redirects=True,
            timeout=60.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _shared_client


_IMAGE_MAGIC = {
    b'\x89PNG': True,
    b'\xff\xd8\xff': True,  # JPEG
    b'GIF8': True,
    b'RIFF': True,  # WebP
    b'\x00\x00\x00': True,  # MP4/MOV
    b'\x1aE\xdf\xa3': True,  # WebM/MKV
    b'PK\x03\x04': True,    # ZIP (ugoira)
}


def _is_valid_media(path: Path) -> bool:
    """Check if a file looks like actual media, not an HTML error page."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        if not header or header.startswith(b'<') or header.startswith(b'<!'):
            return False
        # Check for known magic bytes
        for magic in _IMAGE_MAGIC:
            if header.startswith(magic):
                return True
        # If not a known type but not HTML, assume it's ok
        return b'<html' not in header.lower() and b'<!doctype' not in header.lower()
    except Exception:
        return False


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0]
    if "." in path.split("/")[-1]:
        return "." + path.split("/")[-1].rsplit(".", 1)[-1]
    return ".jpg"


def _convert_ugoira_to_gif(zip_path: Path) -> Path:
    """Convert a Pixiv ugoira zip (numbered JPEG/PNG frames) to an animated GIF."""
    import io
    gif_path = zip_path.with_suffix(".gif")
    if gif_path.exists():
        return gif_path
    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(n for n in zf.namelist() if Path(n).suffix.lower() in _IMG_EXTS)
        frames = []
        for name in names:
            try:
                data = zf.read(name)
                frames.append(Image.open(io.BytesIO(data)).convert("RGBA"))
            except Exception:
                continue
    if not frames:
        # Can't convert — just return the zip path as-is
        return zip_path
    frames[0].save(
        gif_path, save_all=True, append_images=frames[1:],
        duration=80, loop=0, disposal=2,
    )
    if gif_path.exists():
        zip_path.unlink()
    return gif_path


def _convert_animated_to_gif(source_path: Path) -> Path:
    """Convert animated PNG or WebP to GIF for Qt playback."""
    gif_path = source_path.with_suffix(".gif")
    if gif_path.exists():
        return gif_path
    try:
        img = Image.open(source_path)
        if not getattr(img, 'is_animated', False):
            return source_path  # not animated, keep as-is
        frames = []
        durations = []
        for i in range(img.n_frames):
            img.seek(i)
            frames.append(img.convert("RGBA").copy())
            durations.append(img.info.get("duration", 80))
        if not frames:
            return source_path
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=durations, loop=0, disposal=2,
        )
        if gif_path.exists():
            source_path.unlink()
            return gif_path
    except Exception:
        pass
    return source_path


async def download_image(
    url: str,
    client: httpx.AsyncClient | None = None,
    dest_dir: Path | None = None,
    progress_callback=None,
) -> Path:
    """Download an image to the cache, returning the local path. Skips if already cached.

    progress_callback: optional callable(bytes_downloaded, total_bytes)
    """
    dest_dir = dest_dir or cache_dir()
    filename = _url_hash(url) + _ext_from_url(url)
    local = dest_dir / filename

    # Check if a ugoira zip was already converted to gif
    if local.suffix.lower() == ".zip":
        gif_path = local.with_suffix(".gif")
        if gif_path.exists():
            return gif_path
        # If the zip is cached but not yet converted, convert it now
        if local.exists() and zipfile.is_zipfile(local):
            return _convert_ugoira_to_gif(local)

    # Check if animated PNG/WebP was already converted to gif
    if local.suffix.lower() in (".png", ".webp"):
        gif_path = local.with_suffix(".gif")
        if gif_path.exists():
            return gif_path

    # Validate cached file isn't corrupt (e.g. HTML error page saved as image)
    if local.exists():
        if _is_valid_media(local):
            # Convert animated PNG/WebP on access if not yet converted
            if local.suffix.lower() in (".png", ".webp"):
                converted = _convert_animated_to_gif(local)
                if converted != local:
                    return converted
            return local
        else:
            local.unlink()  # Remove corrupt cache entry

    # Extract referer from URL domain (needed for Gelbooru CDN etc.)
    parsed = urlparse(url)
    # Map CDN hostnames back to the main site
    referer_host = parsed.netloc
    if referer_host.startswith("img") and "gelbooru" in referer_host:
        referer_host = "gelbooru.com"
    elif referer_host.startswith("cdn") and "donmai" in referer_host:
        referer_host = "danbooru.donmai.us"
    referer = f"{parsed.scheme}://{referer_host}/"

    log_connection(url)

    req_headers = {"Referer": referer}

    own_client = client is None
    if own_client:
        client = _get_shared_client()
    try:
        if progress_callback:
            async with client.stream("GET", url, headers=req_headers) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    raise ValueError(f"Server returned HTML instead of media (possible captcha/block)")
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                chunks = []
                async for chunk in resp.aiter_bytes(8192):
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    progress_callback(downloaded, total)
                data = b"".join(chunks)
                local.write_bytes(data)
        else:
            resp = await client.get(url, headers=req_headers)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                raise ValueError(f"Server returned HTML instead of media (possible captcha/block)")
            local.write_bytes(resp.content)

        # Verify the downloaded file
        if not _is_valid_media(local):
            local.unlink()
            raise ValueError("Downloaded file is not valid media")

        # Convert ugoira zip to animated GIF
        if local.suffix.lower() == ".zip" and zipfile.is_zipfile(local):
            local = _convert_ugoira_to_gif(local)
        # Convert animated PNG/WebP to GIF for Qt playback
        elif local.suffix.lower() in (".png", ".webp"):
            local = _convert_animated_to_gif(local)
    finally:
        pass  # shared client stays open for connection reuse
    return local


async def download_thumbnail(
    url: str,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Download a thumbnail preview image."""
    return await download_image(url, client, thumbnails_dir())


def cached_path_for(url: str, dest_dir: Path | None = None) -> Path:
    """Return the expected cache path for a URL (may not exist yet)."""
    dest_dir = dest_dir or cache_dir()
    return dest_dir / (_url_hash(url) + _ext_from_url(url))


def is_cached(url: str, dest_dir: Path | None = None) -> bool:
    return cached_path_for(url, dest_dir).exists()


def delete_from_library(post_id: int, folder: str | None = None) -> bool:
    """Delete a saved image from the library. Returns True if a file was deleted."""
    from .config import saved_dir, saved_folder_dir
    search_dir = saved_folder_dir(folder) if folder else saved_dir()
    from .config import MEDIA_EXTENSIONS
    for ext in MEDIA_EXTENSIONS:
        path = search_dir / f"{post_id}{ext}"
        if path.exists():
            path.unlink()
            return True
    return False


def cache_size_bytes(include_thumbnails: bool = True) -> int:
    """Total size of all cached files in bytes."""
    total = sum(f.stat().st_size for f in cache_dir().iterdir() if f.is_file())
    if include_thumbnails:
        total += sum(f.stat().st_size for f in thumbnails_dir().iterdir() if f.is_file())
    return total


def cache_file_count(include_thumbnails: bool = True) -> tuple[int, int]:
    """Return (image_count, thumbnail_count)."""
    images = sum(1 for f in cache_dir().iterdir() if f.is_file())
    thumbs = sum(1 for f in thumbnails_dir().iterdir() if f.is_file()) if include_thumbnails else 0
    return images, thumbs


def evict_oldest(max_bytes: int, protected_paths: set[str] | None = None) -> int:
    """Delete oldest non-protected cached images until under max_bytes. Returns count deleted."""
    protected = protected_paths or set()
    files = sorted(cache_dir().iterdir(), key=lambda f: f.stat().st_mtime)
    deleted = 0
    current = cache_size_bytes(include_thumbnails=False)

    for f in files:
        if current <= max_bytes:
            break
        if not f.is_file() or str(f) in protected:
            continue
        size = f.stat().st_size
        f.unlink()
        current -= size
        deleted += 1

    return deleted


def evict_oldest_thumbnails(max_bytes: int) -> int:
    """Delete oldest thumbnails until under max_bytes. Returns count deleted."""
    td = thumbnails_dir()
    if not td.exists():
        return 0
    files = sorted(td.iterdir(), key=lambda f: f.stat().st_mtime)
    deleted = 0
    current = sum(f.stat().st_size for f in td.iterdir() if f.is_file())
    for f in files:
        if current <= max_bytes:
            break
        if not f.is_file():
            continue
        size = f.stat().st_size
        f.unlink()
        current -= size
        deleted += 1
    return deleted


def clear_cache(clear_images: bool = True, clear_thumbnails: bool = True) -> int:
    """Delete all cached files. Returns count deleted."""
    deleted = 0
    if clear_images:
        for f in cache_dir().iterdir():
            if f.is_file():
                f.unlink()
                deleted += 1
    if clear_thumbnails:
        for f in thumbnails_dir().iterdir():
            if f.is_file():
                f.unlink()
                deleted += 1
    return deleted
