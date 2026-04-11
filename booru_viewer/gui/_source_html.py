"""Pure helper for the info-panel Source line.

Lives in its own module so the helper can be unit-tested from CI
without pulling in PySide6. ``info_panel.py`` imports it.
"""

from __future__ import annotations

from html import escape


def build_source_html(source: str | None) -> str:
    """Build the rich-text fragment for the Source line in the info panel.

    The fragment is inserted into a QLabel set to RichText format with
    setOpenExternalLinks(True) — that means QTextBrowser parses any HTML
    in *source* as markup. Without escaping, a hostile booru can break
    out of the href attribute, inject ``<img>`` tracking pixels, or make
    the visible text disagree with the click target.

    The href is only emitted for an http(s) URL; everything else is
    rendered as escaped plain text. Both the href value and the visible
    display text are HTML-escaped (audit finding #6).
    """
    if not source:
        return "none"
    # Truncate display text but keep the full URL for the link target.
    display = source if len(source) <= 60 else source[:57] + "..."
    if source.startswith(("http://", "https://")):
        return (
            f'<a href="{escape(source, quote=True)}" '
            f'style="color: #4fc3f7;">{escape(display)}</a>'
        )
    return escape(display)
