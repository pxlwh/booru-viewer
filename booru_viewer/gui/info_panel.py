"""Toggleable info panel showing post details with category-coloured tags."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, Property, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QPushButton, QSizePolicy,
)

from ..core.api.base import Post
from ._source_html import build_source_html

log = logging.getLogger("booru")


# -- Info Panel --

class InfoPanel(QWidget):
    """Toggleable panel showing post details."""

    tag_clicked = Signal(str)

    # Tag category colors. Defaults follow the booru convention (Danbooru,
    # Gelbooru, etc.) so the panel reads naturally to anyone coming from a
    # booru site. Each is exposed as a Qt Property so a custom.qss can
    # override it via `qproperty-tag<Category>Color` selectors on
    # `InfoPanel`. An empty string means "use the default text color"
    # (the General category) and is preserved as a sentinel.
    _tag_artist_color = QColor("#f2ac08")
    _tag_character_color = QColor("#0a0")
    _tag_copyright_color = QColor("#c0f")
    _tag_species_color = QColor("#e44")
    _tag_meta_color = QColor("#888")
    _tag_lore_color = QColor("#888")

    def _get_artist(self): return self._tag_artist_color
    def _set_artist(self, c): self._tag_artist_color = QColor(c) if isinstance(c, str) else c
    tagArtistColor = Property(QColor, _get_artist, _set_artist)

    def _get_character(self): return self._tag_character_color
    def _set_character(self, c): self._tag_character_color = QColor(c) if isinstance(c, str) else c
    tagCharacterColor = Property(QColor, _get_character, _set_character)

    def _get_copyright(self): return self._tag_copyright_color
    def _set_copyright(self, c): self._tag_copyright_color = QColor(c) if isinstance(c, str) else c
    tagCopyrightColor = Property(QColor, _get_copyright, _set_copyright)

    def _get_species(self): return self._tag_species_color
    def _set_species(self, c): self._tag_species_color = QColor(c) if isinstance(c, str) else c
    tagSpeciesColor = Property(QColor, _get_species, _set_species)

    def _get_meta(self): return self._tag_meta_color
    def _set_meta(self, c): self._tag_meta_color = QColor(c) if isinstance(c, str) else c
    tagMetaColor = Property(QColor, _get_meta, _set_meta)

    def _get_lore(self): return self._tag_lore_color
    def _set_lore(self, c): self._tag_lore_color = QColor(c) if isinstance(c, str) else c
    tagLoreColor = Property(QColor, _get_lore, _set_lore)

    def _category_color(self, category: str) -> str:
        """Resolve a category name to a hex color string for inline QSS use.
        Returns "" for the General category (no override → use default text
        color) and unrecognized categories (so callers can render them with
        no color attribute set)."""
        cat = (category or "").lower()
        m = {
            "artist": self._tag_artist_color,
            "character": self._tag_character_color,
            "copyright": self._tag_copyright_color,
            "species": self._tag_species_color,
            "meta": self._tag_meta_color,
            "lore": self._tag_lore_color,
        }
        c = m.get(cat)
        return c.name() if c is not None else ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._categories_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._title = QLabel("No post selected")
        self._title.setStyleSheet("font-weight: bold;")
        self._title.setMinimumWidth(0)
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._title)

        self._details = QLabel()
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextBrowserInteraction)
        self._details.setMaximumHeight(120)
        self._details.setMinimumWidth(0)
        self._details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._details)

        self._tags_label = QLabel("Tags:")
        self._tags_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(self._tags_label)

        self._tags_scroll = QScrollArea()
        self._tags_scroll.setWidgetResizable(True)
        self._tags_scroll.setStyleSheet("QScrollArea { border: none; }")
        self._tags_widget = QWidget()
        self._tags_flow = QVBoxLayout(self._tags_widget)
        self._tags_flow.setContentsMargins(0, 0, 0, 0)
        self._tags_flow.setSpacing(2)
        self._tags_scroll.setWidget(self._tags_widget)
        layout.addWidget(self._tags_scroll, stretch=1)

    def set_post(self, post: Post) -> None:
        log.debug(f"InfoPanel: tag_categories={list(post.tag_categories.keys()) if post.tag_categories else 'empty'}")
        self._title.setText(f"Post #{post.id}")
        filetype = Path(post.file_url.split("?")[0]).suffix.lstrip(".").upper() if post.file_url else "unknown"
        source_html = build_source_html(post.source)
        self._details.setTextFormat(Qt.TextFormat.RichText)
        self._details.setText(
            f"Score: {post.score}<br>"
            f"Rating: {escape(post.rating or 'unknown')}<br>"
            f"Filetype: {escape(filetype)}<br>"
            f"Source: {source_html}"
        )
        self._details.setOpenExternalLinks(True)
        # Clear old tags
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if post.tag_categories:
            # Display tags grouped by category. Colors come from the
            # tag*Color Qt Properties so a custom.qss can override any of
            # them via `InfoPanel { qproperty-tagCharacterColor: ...; }`.
            for category, tags in post.tag_categories.items():
                color = self._category_color(category)
                header = QLabel(f"{category}:")
                header.setStyleSheet(
                    f"font-weight: bold; margin-top: 6px; margin-bottom: 2px;"
                    + (f" color: {color};" if color else "")
                )
                self._tags_flow.addWidget(header)
                for tag in tags[:50]:
                    btn = QPushButton(tag)
                    btn.setFlat(True)
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    style = "QPushButton { text-align: left; padding: 1px 4px; border: none;"
                    if color:
                        style += f" color: {color};"
                    style += " }"
                    btn.setStyleSheet(style)
                    btn.clicked.connect(lambda checked, t=tag: self.tag_clicked.emit(t))
                    self._tags_flow.addWidget(btn)
        elif not self._categories_pending:
            # Flat tag fallback — only when no category fetch is
            # in-flight. When a fetch IS pending, leaving the tags
            # area empty avoids the flat→categorized re-layout hitch
            # (categories arrive ~200ms later and render in one pass).
            for tag in post.tag_list[:100]:
                btn = QPushButton(tag)
                btn.setFlat(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { text-align: left; padding: 1px 4px; border: none; }"
                )
                btn.clicked.connect(lambda checked, t=tag: self.tag_clicked.emit(t))
                self._tags_flow.addWidget(btn)
        self._tags_flow.addStretch()

    def clear(self) -> None:
        self._title.setText("No post selected")
        self._details.setText("")
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
