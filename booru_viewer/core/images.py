"""Image thumbnailing and format helpers."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .config import DEFAULT_THUMBNAIL_SIZE, thumbnails_dir


def make_thumbnail(
    source: Path,
    size: tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    dest: Path | None = None,
) -> Path:
    """Create a thumbnail, returning its path. Returns existing if already made."""
    dest = dest or thumbnails_dir() / f"thumb_{source.stem}_{size[0]}x{size[1]}.jpg"
    if dest.exists():
        return dest
    with Image.open(source) as img:
        img.thumbnail(size, Image.Resampling.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(dest, "JPEG", quality=85)
    return dest


def image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size
