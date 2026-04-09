"""Download manager and local file cache."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
import zipfile
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from .config import cache_dir, thumbnails_dir, USER_AGENT

log = logging.getLogger("booru")

# Hard cap on a single download. Anything advertising larger via
# Content-Length is rejected before allocating; the running-total guard
# in the chunk loop catches lying servers. Generous enough for typical
# booru uploads (long doujinshi/HD video) without leaving the door open
# to multi-GB OOM/disk-fill from a hostile or misconfigured site.
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

# Threshold above which we stream to a tempfile + atomic os.replace
# instead of buffering. Below this, the existing path is fine and the
# regression risk of the streaming rewrite is zero.
STREAM_TO_DISK_THRESHOLD = 50 * 1024 * 1024  # 50 MB

# Cap PIL's auto-DOS guard at 256M pixels (~1 GB raw). Default warns
# silently above ~89M; we want a hard fail so DecompressionBombError
# can be caught and treated as a download failure.
Image.MAX_IMAGE_PIXELS = 256 * 1024 * 1024

# Defends `_convert_ugoira_to_gif` against zip bombs. A real ugoira is
# typically <500 frames at 1080p; these caps comfortably allow legit
# content while refusing million-frame archives.
UGOIRA_MAX_FRAMES = 5000
UGOIRA_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB

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


# Shared httpx client for connection pooling (avoids per-request TLS handshakes).
# Lazily created on first download. Lock guards the check-and-set so concurrent
# first-callers can't both build a client and leak one. Loop affinity is
# guaranteed by routing all downloads through `core.concurrency.run_on_app_loop`
# (see PR2).
_shared_client: httpx.AsyncClient | None = None
_shared_client_lock = threading.Lock()


def _get_shared_client(referer: str = "") -> httpx.AsyncClient:
    global _shared_client
    c = _shared_client
    if c is not None and not c.is_closed:
        return c
    with _shared_client_lock:
        c = _shared_client
        if c is None or c.is_closed:
            c = httpx.AsyncClient(
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/*,video/*,*/*",
                },
                follow_redirects=True,
                timeout=60.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
            _shared_client = c
        return c


async def aclose_shared_client() -> None:
    """Cleanly aclose the cache module's shared download client. Safe to call
    once at app shutdown; no-op if not initialized."""
    global _shared_client
    with _shared_client_lock:
        c = _shared_client
        _shared_client = None
    if c is not None and not c.is_closed:
        try:
            await c.aclose()
        except Exception as e:
            log.warning("cache shared client aclose failed: %s", e)


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
    """Check if a file looks like actual media, not an HTML error page.

    On transient IO errors (file locked, EBUSY, permissions hiccup), returns
    True so the caller does NOT delete the cached file. The previous behavior
    treated IO errors as "invalid", causing a delete + re-download loop on
    every access while the underlying issue persisted.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError as e:
        log.warning("Cannot read %s for validation (%s); treating as valid", path, e)
        return True
    if not header or header.startswith(b'<') or header.startswith(b'<!'):
        return False
    # Check for known magic bytes
    for magic in _IMAGE_MAGIC:
        if header.startswith(magic):
            return True
    # If not a known type but not HTML, assume it's ok
    return b'<html' not in header.lower() and b'<!doctype' not in header.lower()


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0]
    if "." in path.split("/")[-1]:
        return "." + path.split("/")[-1].rsplit(".", 1)[-1]
    return ".jpg"


def _convert_ugoira_to_gif(zip_path: Path) -> Path:
    """Convert a Pixiv ugoira zip (numbered JPEG/PNG frames) to an animated GIF.

    Defends against zip bombs by capping frame count and cumulative
    uncompressed size, both checked from `ZipInfo` headers BEFORE any
    decompression. Falls back to returning the original zip on any error
    so the caller still has a usable file.
    """
    import io
    gif_path = zip_path.with_suffix(".gif")
    if gif_path.exists():
        return gif_path
    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = [zi for zi in zf.infolist()
                     if Path(zi.filename).suffix.lower() in _IMG_EXTS]
            if len(infos) > UGOIRA_MAX_FRAMES:
                log.warning(
                    "Ugoira %s has %d frames (cap %d); skipping conversion",
                    zip_path.name, len(infos), UGOIRA_MAX_FRAMES,
                )
                return zip_path
            total_uncompressed = sum(zi.file_size for zi in infos)
            if total_uncompressed > UGOIRA_MAX_UNCOMPRESSED_BYTES:
                log.warning(
                    "Ugoira %s uncompressed size %d exceeds cap %d; skipping",
                    zip_path.name, total_uncompressed, UGOIRA_MAX_UNCOMPRESSED_BYTES,
                )
                return zip_path
            infos.sort(key=lambda zi: zi.filename)
            frames = []
            for zi in infos:
                try:
                    data = zf.read(zi)
                    with Image.open(io.BytesIO(data)) as im:
                        frames.append(im.convert("RGBA"))
                except Exception as e:
                    log.debug("Skipping ugoira frame %s: %s", zi.filename, e)
                    continue
    except (zipfile.BadZipFile, OSError) as e:
        log.warning("Ugoira zip read failed for %s: %s", zip_path.name, e)
        return zip_path
    if not frames:
        return zip_path
    try:
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=80, loop=0, disposal=2,
        )
    except Exception as e:
        log.warning("Ugoira GIF write failed for %s: %s", zip_path.name, e)
        return zip_path
    if gif_path.exists():
        zip_path.unlink()
    return gif_path


def _convert_animated_to_gif(source_path: Path) -> Path:
    """Convert animated PNG or WebP to GIF for Qt playback.

    Writes a `.failed` sentinel sibling on conversion failure so we don't
    re-attempt every access — re-trying on every paint of a malformed
    file used to chew CPU silently.
    """
    gif_path = source_path.with_suffix(".gif")
    if gif_path.exists():
        return gif_path
    sentinel = source_path.with_suffix(source_path.suffix + ".convfailed")
    if sentinel.exists():
        return source_path
    try:
        with Image.open(source_path) as img:
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
    except Exception as e:
        log.warning("Animated->GIF conversion failed for %s: %s", source_path.name, e)
        try:
            sentinel.touch()
        except OSError:
            pass
    return source_path


def _referer_for(parsed) -> str:
    """Build a Referer header value for booru CDNs that gate downloads.

    Uses proper hostname suffix matching instead of substring `in` to avoid
    `imgblahgelbooru.attacker.com` falsely mapping to `gelbooru.com`.
    """
    netloc = parsed.netloc
    bare = netloc.split(":", 1)[0].lower()  # strip any port
    referer_host = netloc
    if bare.endswith(".gelbooru.com") or bare == "gelbooru.com":
        referer_host = "gelbooru.com"
    elif bare.endswith(".donmai.us") or bare == "donmai.us":
        referer_host = "danbooru.donmai.us"
    return f"{parsed.scheme}://{referer_host}/"


# Per-URL coalescing locks. When two callers race on the same URL (e.g.
# grid prefetch + an explicit click on the same thumbnail), only one
# does the actual download; the other waits and reads the cached file.
# Loop-bound, but the existing module is already loop-bound, so this
# doesn't make anything worse and is fixed cleanly in PR2.
_url_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


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

    async with _url_locks[_url_hash(url)]:
        # Check if a ugoira zip was already converted to gif
        if local.suffix.lower() == ".zip":
            gif_path = local.with_suffix(".gif")
            if gif_path.exists():
                return gif_path
            # If the zip is cached but not yet converted, convert it now.
            # PIL frame iteration is CPU-bound and would block the asyncio
            # loop for hundreds of ms — run it in a worker thread instead.
            if local.exists() and zipfile.is_zipfile(local):
                return await asyncio.to_thread(_convert_ugoira_to_gif, local)

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
                    converted = await asyncio.to_thread(_convert_animated_to_gif, local)
                    if converted != local:
                        return converted
                return local
            else:
                local.unlink()  # Remove corrupt cache entry

        parsed = urlparse(url)
        referer = _referer_for(parsed)
        log_connection(url)
        req_headers = {"Referer": referer}

        if client is None:
            client = _get_shared_client()

        await _do_download(client, url, req_headers, local, progress_callback)

        # Verify the downloaded file
        if not _is_valid_media(local):
            local.unlink()
            raise ValueError("Downloaded file is not valid media")

        # Convert ugoira zip to animated GIF (PIL is sync + CPU-bound;
        # off-load to a worker so we don't block the asyncio loop).
        if local.suffix.lower() == ".zip" and zipfile.is_zipfile(local):
            local = await asyncio.to_thread(_convert_ugoira_to_gif, local)
        # Convert animated PNG/WebP to GIF for Qt playback
        elif local.suffix.lower() in (".png", ".webp"):
            local = await asyncio.to_thread(_convert_animated_to_gif, local)
    return local


async def _do_download(
    client: httpx.AsyncClient,
    url: str,
    req_headers: dict,
    local: Path,
    progress_callback,
) -> None:
    """Perform the actual HTTP fetch and write to `local`.

    Splits on size: small/unknown payloads buffer in memory and write atomically;
    large payloads stream to a tempfile in the same directory and `os.replace`
    on completion. The split keeps the existing fast-path for thumbnails (which
    is the vast majority of downloads) while preventing OOM on multi-hundred-MB
    videos. Both paths enforce `MAX_DOWNLOAD_BYTES` against the advertised
    Content-Length AND the running total (servers can lie about length).
    """
    async with client.stream("GET", url, headers=req_headers) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            raise ValueError("Server returned HTML instead of media (possible captcha/block)")

        try:
            total = int(resp.headers.get("content-length", 0))
        except (TypeError, ValueError):
            total = 0
        if total > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"Download too large: {total} bytes (cap {MAX_DOWNLOAD_BYTES})"
            )

        if total >= STREAM_TO_DISK_THRESHOLD:
            # Large download: stream to tempfile in the same dir, atomic replace.
            local.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{local.name}.", suffix=".part", dir=str(local.parent)
            )
            tmp_path = Path(tmp_name)
            try:
                downloaded = 0
                with os.fdopen(fd, "wb") as out:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        out.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"Download exceeded cap mid-stream: {downloaded} bytes"
                            )
                        if progress_callback:
                            progress_callback(downloaded, total)
                os.replace(tmp_path, local)
            except BaseException:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        else:
            # Small/unknown size: buffer in memory, write whole.
            chunks: list[bytes] = []
            downloaded = 0
            async for chunk in resp.aiter_bytes(8192):
                chunks.append(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Download exceeded cap mid-stream: {downloaded} bytes"
                    )
                if progress_callback:
                    progress_callback(downloaded, total)
            local.write_bytes(b"".join(chunks))


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


def delete_from_library(post_id: int, folder: str | None = None, db=None) -> bool:
    """Delete every saved copy of `post_id` from the library.

    Returns True if at least one file was deleted.

    The `folder` argument is kept for back-compat with existing call sites
    but is now ignored — we walk every library folder by post id and delete
    all matches. This is what makes the "bookmark folder ≠ library folder"
    separation work: a bookmark no longer needs to know which folder its
    library file lives in. It also cleans up duplicates left by the old
    pre-fix "save to folder = copy" bug in a single Unsave action.

    Pass `db` to also match templated filenames (post-refactor saves
    that aren't named {post_id}.{ext}) and to clean up the library_meta
    row in the same call. Without `db`, only digit-stem files are
    found and the meta row stays — that's the old broken behavior,
    preserved as a fallback for callers that don't have a Database
    handle.
    """
    from .config import find_library_files
    matches = find_library_files(post_id, db=db)
    deleted = False
    for path in matches:
        try:
            path.unlink()
            deleted = True
        except OSError:
            pass
    # Always drop the meta row, even when no files were unlinked.
    # Two cases this matters for:
    #   1. Files were on disk and unlinked — meta row is now stale.
    #   2. Files were already gone (orphan meta row from a previous
    #      delete that didn't clean up). The user asked to "unsave"
    #      this post and the meta should reflect that, even if
    #      there's nothing left on disk.
    # Without this cleanup the post stays "saved" in the DB and
    # is_post_in_library lies forever. The lookup is keyed by
    # post_id so this is one cheap DELETE regardless of how many
    # copies were on disk.
    if db is not None:
        try:
            db.remove_library_meta(post_id)
        except Exception:
            pass
    return deleted


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
