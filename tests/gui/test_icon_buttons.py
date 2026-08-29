"""The 24px glyph toolbar buttons (☆ ↓ ⊘ ⊗ ⧉) must not inherit theme padding.

Bundled themes style `QPushButton { padding: 2px 6px; border: 1px }`. On a
button fixed at 24x24 that leaves a 10px-wide content box, so the glyph is
clipped (the star lost its points). The base stylesheet carries an
attribute-selector rule for `iconBtn="true"` which outranks any theme's
bare `QPushButton` rule on specificity, so it holds under every custom.qss.
"""

from __future__ import annotations

import re


def test_base_qss_zeroes_padding_for_icon_buttons():
    from booru_viewer.gui.base_qss import _BASE_POPOUT_OVERLAY_QSS as qss
    m = re.search(r'QPushButton\[iconBtn="true"\][^{]*\{([^}]*)\}', qss)
    assert m, "no iconBtn rule in base QSS"
    body = m.group(1)
    assert re.search(r"padding\s*:\s*0\s*;", body)
    # QSS min-*/max-* overwrite the widget's own minimum/maximum size, so
    # the rule must pin all four or setFixedSize(24, 24) stops holding and
    # the layout shrinks each button to its glyph width.
    sizes = {k: re.search(rf"{k}\s*:\s*(\d+)px\s*;", body) for k in
             ("min-width", "max-width", "min-height", "max-height")}
    assert all(sizes.values()), sizes
    assert len({m.group(1) for m in sizes.values()}) == 1, "icon buttons must be square and uniform"
    assert re.search(r"font-size\s*:\s*\d+px\s*;", body)


def test_base_qss_comments_are_balanced():
    """A stray `*/` inside a comment (it happened: `min-*/max-*` in prose)
    closes the comment early, Qt rejects the WHOLE application stylesheet,
    and every custom.qss user silently loses their theme. Qt-free guard:
    comment openers and closers must pair up, and nothing comment-like may
    survive outside a comment."""
    from booru_viewer.gui.base_qss import _BASE_POPOUT_OVERLAY_QSS as qss
    assert qss.count("/*") == qss.count("*/"), "unbalanced CSS comment markers"
    stripped = re.sub(r"/\*.*?\*/", "", qss, flags=re.S)
    assert "*/" not in stripped and "/*" not in stripped
    assert stripped.count("{") == stripped.count("}")


def test_icon_button_rule_outranks_theme_overlay_rules():
    """Themes use `QWidget#_slideshow_toolbar QPushButton` (1-0-1). The base
    rule must include an ID + attribute variant (1-1-1) for each overlay
    container or the popout keeps the theme's padding and clips."""
    from booru_viewer.gui.base_qss import _BASE_POPOUT_OVERLAY_QSS as qss
    selector_block = re.search(r'((?:[^{}]*QPushButton\[iconBtn="true"\][^{}]*)+)\{', qss).group(1)
    for container in ("_slideshow_toolbar", "_slideshow_controls", "_preview_controls"):
        assert f'QWidget#{container} QPushButton[iconBtn="true"]' in selector_block, container
