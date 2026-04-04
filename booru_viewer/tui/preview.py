"""Image preview widget with Kitty graphics protocol support."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from textual.widgets import Static

from ..core.config import GREEN, DIM_GREEN, BG


def _supports_kitty() -> bool:
    """Check if the terminal likely supports the Kitty graphics protocol."""
    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    return "kitty" in term or "kitty" in term_program


def _kitty_display(path: str, cols: int = 80, rows: int = 24) -> str:
    """Generate Kitty graphics protocol escape sequence for an image."""
    try:
        data = Path(path).read_bytes()
        b64 = base64.standard_b64encode(data).decode("ascii")

        # Send in chunks (Kitty protocol requires chunked transfer for large images)
        chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
        output = ""
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            m = 0 if is_last else 1
            if i == 0:
                output += f"\033_Ga=T,f=100,m={m},c={cols},r={rows};{chunk}\033\\"
            else:
                output += f"\033_Gm={m};{chunk}\033\\"
        return output
    except Exception:
        return ""


class ImagePreview(Static):
    """Image preview panel. Uses Kitty graphics protocol on supported terminals,
    otherwise shows image metadata."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._path: str | None = None
        self._info: str = ""
        self._use_kitty = _supports_kitty()

    def show_image(self, path: str, info: str = "") -> None:
        self._path = path
        self._info = info

        if self._use_kitty and self._path:
            # Write Kitty escape directly to terminal, show info in widget
            size = self.size
            kitty_seq = _kitty_display(path, cols=size.width, rows=size.height - 2)
            if kitty_seq:
                sys.stdout.write(kitty_seq)
                sys.stdout.flush()
            self.update(f"\n{info}")
        else:
            # Fallback: show file info
            try:
                from PIL import Image
                with Image.open(path) as img:
                    w, h = img.size
                    fmt = img.format or "unknown"
                size_kb = Path(path).stat().st_size / 1024
                text = (
                    f"  Image: {Path(path).name}\n"
                    f"  Size:  {w}x{h} ({size_kb:.0f} KB)\n"
                    f"  Format: {fmt}\n"
                    f"\n  {info}\n"
                    f"\n  (Kitty graphics protocol not detected;\n"
                    f"   run in Kitty terminal for inline preview)"
                )
            except Exception:
                text = f"  {info}\n\n  (Cannot read image)"
            self.update(text)

    def clear(self) -> None:
        self._path = None
        self._info = ""
        if self._use_kitty:
            # Clear Kitty images
            sys.stdout.write("\033_Ga=d;\033\\")
            sys.stdout.flush()
        self.update("")
