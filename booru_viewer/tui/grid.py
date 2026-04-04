"""Thumbnail grid widget for the Textual TUI."""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import Static
from textual.reactive import reactive

from ..core.api.base import Post
from ..core.db import Database
from ..core.config import GREEN, DIM_GREEN, BG


class ThumbnailCell(Static):
    """A single post cell in the grid."""

    def __init__(self, index: int, post: Post, favorited: bool = False) -> None:
        self._index = index
        self._post = post
        self._favorited = favorited
        self._selected = False
        super().__init__()

    def compose_content(self) -> str:
        fav = " *" if self._favorited else ""
        rating = self._post.rating or "?"
        return (
            f"#{self._post.id}{fav}\n"
            f"[{rating}] s:{self._post.score}\n"
            f"{self._post.width}x{self._post.height}"
        )

    def on_mount(self) -> None:
        self.update(self.compose_content())
        self._apply_style()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_style()

    def set_favorited(self, favorited: bool) -> None:
        self._favorited = favorited
        self.update(self.compose_content())

    def _apply_style(self) -> None:
        if self._selected:
            self.styles.background = DIM_GREEN
            self.styles.color = BG
            self.styles.border = ("solid", GREEN)
        else:
            self.styles.background = BG
            self.styles.color = GREEN if self._favorited else DIM_GREEN
            self.styles.border = ("solid", DIM_GREEN)

    def on_click(self) -> None:
        self.post_message(CellClicked(self._index))


class CellClicked:
    """Message sent when a cell is clicked."""
    def __init__(self, index: int) -> None:
        self.index = index


class ThumbnailGrid(Static):
    """Grid of post cells with keyboard navigation."""

    BINDINGS = [
        Binding("j", "move_down", "Down", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("h", "move_left", "Left", show=False),
        Binding("l", "move_right", "Right", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("up", "move_up", "Up", show=False),
        Binding("left", "move_left", "Left", show=False),
        Binding("right", "move_right", "Right", show=False),
    ]

    selected_index: int = reactive(-1, init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cells: list[ThumbnailCell] = []
        self._posts: list[Post] = []
        self._cols = 4
        self.can_focus = True

    def set_posts(
        self, posts: list[Post], db: Database | None = None, site_id: int | None = None
    ) -> None:
        self._posts = posts
        # Remove old cells
        for cell in self._cells:
            cell.remove()
        self._cells.clear()

        lines = []
        for i, post in enumerate(posts):
            fav = False
            if db and site_id:
                fav = db.is_favorited(site_id, post.id)

            fav_marker = " *" if fav else ""
            rating = post.rating or "?"
            selected = " >> " if i == 0 else "    "
            lines.append(
                f"{selected}#{post.id}{fav_marker}  [{rating}]  "
                f"s:{post.score}  {post.width}x{post.height}"
            )

        self.selected_index = 0 if posts else -1
        self.update("\n".join(lines) if lines else "No results. Search for tags above.")

    def update_favorite_status(self, index: int, favorited: bool) -> None:
        """Refresh the display for a single post's favorite status."""
        if 0 <= index < len(self._posts):
            self.set_posts(self._posts)  # Simple refresh

    def _render_list(self) -> None:
        lines = []
        for i, post in enumerate(self._posts):
            selected = " >> " if i == self.selected_index else "    "
            lines.append(
                f"{selected}#{post.id}  [{post.rating or '?'}]  "
                f"s:{post.score}  {post.width}x{post.height}"
            )
        self.update("\n".join(lines) if lines else "No results.")

    def action_move_down(self) -> None:
        if self._posts and self.selected_index < len(self._posts) - 1:
            self.selected_index += 1
            self._render_list()

    def action_move_up(self) -> None:
        if self._posts and self.selected_index > 0:
            self.selected_index -= 1
            self._render_list()

    def action_move_right(self) -> None:
        self.action_move_down()

    def action_move_left(self) -> None:
        self.action_move_up()
