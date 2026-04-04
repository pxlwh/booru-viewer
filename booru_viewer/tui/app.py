"""Main Textual TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Header, Footer, Static, Input, Label, Button, ListView, ListItem

from ..core.db import Database
from ..core.api.base import Post
from ..core.api.detect import client_for_type
from ..core.cache import download_image
from ..core.config import GREEN, DIM_GREEN, BG, BG_LIGHT, BG_LIGHTER, BORDER


class PostList(ListView):
    """Scrollable list of posts with selection."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._post_count = 0

    async def set_posts(self, posts: list[Post], db: Database | None = None, site_id: int | None = None) -> None:
        self._post_count = len(posts)
        await self.clear()
        for i, post in enumerate(posts):
            fav = ""
            if db and site_id and db.is_favorited(site_id, post.id):
                fav = " [*]"
            rating = (post.rating or "?")[0].upper()
            label = f"#{post.id}{fav}  {rating}  s:{post.score:>4}  {post.width}x{post.height}"
            item = ListItem(Label(label), id=f"post-{i}")
            await self.append(item)

    @staticmethod
    def _get_index(item: ListItem) -> int:
        if item and item.id and item.id.startswith("post-"):
            return int(item.id.split("-")[1])
        return -1

    @property
    def selected_index(self) -> int:
        if self.highlighted_child and self.highlighted_child.id:
            return self._get_index(self.highlighted_child)
        return -1


class InfoBar(Static):
    """Bottom info line showing selected post details."""
    pass


class BooruTUI(App):
    """Booru viewer TUI application."""

    TITLE = "booru-viewer"
    CSS = f"""
    Screen {{
        background: {BG};
        color: {GREEN};
    }}

    Header {{
        background: {BG};
        color: {GREEN};
    }}

    Footer {{
        background: {BG};
        color: {DIM_GREEN};
    }}

    #top-bar {{
        height: 3;
        layout: horizontal;
        padding: 0 1;
    }}

    #top-bar Label {{
        width: auto;
        padding: 1 1;
        color: {DIM_GREEN};
    }}

    #top-bar Input {{
        width: 1fr;
    }}

    #top-bar Button {{
        width: auto;
        min-width: 8;
        margin-left: 1;
    }}

    #nav-bar {{
        height: 3;
        layout: horizontal;
        padding: 0 1;
    }}

    #nav-bar Button {{
        width: auto;
        min-width: 10;
        margin-right: 1;
    }}

    #nav-bar .page-info {{
        width: auto;
        padding: 1 1;
        color: {DIM_GREEN};
    }}

    #main {{
        height: 1fr;
    }}

    #post-list {{
        width: 1fr;
        min-width: 40;
        border-right: solid {BORDER};
    }}

    #right-panel {{
        width: 1fr;
        min-width: 30;
    }}

    #preview {{
        height: 1fr;
    }}

    #info-bar {{
        height: 3;
        padding: 0 1;
        color: {DIM_GREEN};
        border-top: solid {BORDER};
    }}

    #status {{
        height: 1;
        padding: 0 1;
        color: {DIM_GREEN};
    }}

    ListView {{
        background: {BG};
        color: {GREEN};
    }}

    ListView > ListItem {{
        background: {BG};
        color: {DIM_GREEN};
        padding: 0 1;
    }}

    ListView > ListItem.--highlight {{
        background: {BG_LIGHTER};
        color: {GREEN};
    }}

    ListItem:hover {{
        background: {BG_LIGHTER};
    }}

    Button {{
        background: {BG_LIGHT};
        color: {GREEN};
        border: solid {BORDER};
    }}

    Button:hover {{
        background: {DIM_GREEN};
        color: {BG};
    }}

    Button.-active {{
        background: {DIM_GREEN};
        color: {BG};
    }}

    Input {{
        background: {BG_LIGHT};
        color: {GREEN};
        border: solid {BORDER};
    }}

    Input:focus {{
        border: solid {GREEN};
    }}
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("slash", "focus_search", "/Search", show=True),
        Binding("f", "toggle_favorite", "Fav", show=True, priority=True),
        Binding("escape", "unfocus", "Back", show=True),
        Binding("n", "next_page", "Next", show=True, priority=True),
        Binding("p", "prev_page", "Prev", show=True, priority=True),
        Binding("o", "open_in_default", "Open", show=True, priority=True),
        Binding("i", "show_info", "Info", show=True, priority=True),
        Binding("s", "cycle_site", "Site", show=True, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._db = Database()
        self._posts: list[Post] = []
        self._current_page = 1
        self._current_tags = ""
        self._current_site = None
        self._show_info = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top-bar"):
            yield Label("No site", id="site-label")
            yield Input(placeholder="Search tags... (/)", id="search-input")
            yield Button("Go", id="search-btn")
        with Horizontal(id="nav-bar"):
            yield Label("Page 1", classes="page-info", id="page-info")
            yield Button("Prev", id="prev-btn")
            yield Button("Next", id="next-btn")
        with Horizontal(id="main"):
            yield PostList(id="post-list")
            with Vertical(id="right-panel"):
                yield Static("", id="preview")
                yield InfoBar("Select a post", id="info-bar")
        yield Label("Ready", id="status")
        yield Footer()

    def on_mount(self) -> None:
        sites = self._db.get_sites()
        if sites:
            self._current_site = sites[0]
            try:
                self.query_one("#site-label", Label).update(f"[{self._current_site.name}]")
            except NoMatches:
                pass
            self._set_status(f"Connected to {self._current_site.name}")

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#status", Label).update(msg)
        except NoMatches:
            pass

    def _make_client(self):
        if not self._current_site:
            return None
        s = self._current_site
        return client_for_type(s.api_type, s.url, s.api_key, s.api_user)

    # -- Events --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "search-btn":
            self._do_search_from_input()
        elif bid == "prev-btn":
            self.action_prev_page()
        elif bid == "next-btn":
            self.action_next_page()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._do_search_from_input()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update info when navigating the list."""
        if event.item:
            idx = PostList._get_index(event.item)
            if idx >= 0:
                self._update_info(idx)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter key on a list item = load preview."""
        if event.item:
            idx = PostList._get_index(event.item)
            if 0 <= idx < len(self._posts):
                self._load_preview(idx)

    # -- Search --

    def _do_search_from_input(self) -> None:
        try:
            inp = self.query_one("#search-input", Input)
            self._current_tags = inp.value.strip()
        except NoMatches:
            return
        self._current_page = 1
        self._do_search()

    def _do_search(self) -> None:
        if not self._current_site:
            self._set_status("No site configured")
            return
        self._set_status("Searching...")
        try:
            self.query_one("#page-info", Label).update(f"Page {self._current_page}")
        except NoMatches:
            pass

        tags = self._current_tags
        page = self._current_page
        blacklisted = self._db.get_blacklisted_tags()
        search_tags = tags
        for bt in blacklisted:
            search_tags += f" -{bt}"

        async def _search(self=self):
            client = self._make_client()
            if not client:
                return
            try:
                posts = await client.search(tags=search_tags.strip(), page=page)
                self._posts = posts
                self._set_status(f"{len(posts)} results")
                try:
                    post_list = self.query_one("#post-list", PostList)
                    await post_list.set_posts(posts, self._db, self._current_site.id if self._current_site else None)
                except NoMatches:
                    pass
            except Exception as e:
                self._set_status(f"Error: {e}")
            finally:
                await client.close()

        self.run_worker(_search(), exclusive=True)

    # -- Info --

    def _update_info(self, index: int) -> None:
        if 0 <= index < len(self._posts):
            post = self._posts[index]
            status = f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]"
            self._set_status(status)
            if self._show_info:
                tags_preview = " ".join(post.tag_list[:15])
                if len(post.tag_list) > 15:
                    tags_preview += "..."
                info = (
                    f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]\n"
                    f"Tags: {tags_preview}"
                )
                if post.source:
                    info += f"\nSource: {post.source}"
                try:
                    self.query_one("#info-bar", InfoBar).update(info)
                except NoMatches:
                    pass

    # -- Preview --

    def _load_preview(self, index: int) -> None:
        if index < 0 or index >= len(self._posts):
            return
        post = self._posts[index]
        self._set_status(f"Loading #{post.id}...")

        async def _load(self=self):
            try:
                path = await download_image(post.file_url)
                try:
                    from .preview import ImagePreview
                    preview = self.query_one("#preview", Static)
                    # Show image info in the preview area
                    info = (
                        f"  Post #{post.id}\n"
                        f"  Size: {post.width}x{post.height}\n"
                        f"  Score: {post.score}\n"
                        f"  Rating: {post.rating or '?'}\n"
                        f"  Cached: {path}\n"
                    )
                    if post.source:
                        info += f"  Source: {post.source}\n"
                    info += f"\n  Tags: {' '.join(post.tag_list[:20])}"
                    preview.update(info)
                except NoMatches:
                    pass
                self._set_status(f"Loaded #{post.id}")
            except Exception as e:
                self._set_status(f"Error: {e}")

        self.run_worker(_load())

    # -- Actions --

    def action_focus_search(self) -> None:
        try:
            self.query_one("#search-input", Input).focus()
        except NoMatches:
            pass

    def action_unfocus(self) -> None:
        try:
            self.query_one("#post-list", PostList).focus()
        except NoMatches:
            pass

    def action_next_page(self) -> None:
        self._current_page += 1
        self._do_search()

    def action_prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            self._do_search()

    async def action_toggle_favorite(self) -> None:
        post_list = self.query_one("#post-list", PostList)
        idx = post_list.selected_index
        if idx < 0 or idx >= len(self._posts) or not self._current_site:
            return
        post = self._posts[idx]
        site_id = self._current_site.id

        if self._db.is_favorited(site_id, post.id):
            self._db.remove_favorite(site_id, post.id)
            self._set_status(f"Unfavorited #{post.id}")
            await post_list.set_posts(self._posts, self._db, site_id)
        else:
            self._set_status(f"Favoriting #{post.id}...")

            async def _fav(self=self):
                try:
                    path = await download_image(post.file_url)
                    self._db.add_favorite(
                        site_id=site_id,
                        post_id=post.id,
                        file_url=post.file_url,
                        preview_url=post.preview_url,
                        tags=post.tags,
                        rating=post.rating,
                        score=post.score,
                        source=post.source,
                        cached_path=str(path),
                    )
                    self._set_status(f"Favorited #{post.id}")
                    try:
                        post_list = self.query_one("#post-list", PostList)
                        await post_list.set_posts(self._posts, self._db, site_id)
                    except NoMatches:
                        pass
                except Exception as e:
                    self._set_status(f"Error: {e}")

            self.run_worker(_fav())

    def action_open_in_default(self) -> None:
        post_list = self.query_one("#post-list", PostList)
        idx = post_list.selected_index
        if idx < 0 or idx >= len(self._posts):
            return
        post = self._posts[idx]
        from ..core.cache import cached_path_for
        path = cached_path_for(post.file_url)
        if path.exists():
            import subprocess, sys
            if sys.platform == "linux":
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                import os
                os.startfile(str(path))
            self._set_status(f"Opened #{post.id}")
        else:
            self._set_status("Not cached — press Enter to download first")

    def action_show_info(self) -> None:
        self._show_info = not self._show_info
        if self._show_info:
            post_list = self.query_one("#post-list", PostList)
            self._update_info(post_list.selected_index)
        else:
            try:
                self.query_one("#info-bar", InfoBar).update("Info hidden (press i)")
            except NoMatches:
                pass

    def action_cycle_site(self) -> None:
        sites = self._db.get_sites()
        if not sites:
            self._set_status("No sites configured")
            return
        if self._current_site:
            ids = [s.id for s in sites]
            try:
                idx = ids.index(self._current_site.id)
                next_site = sites[(idx + 1) % len(sites)]
            except ValueError:
                next_site = sites[0]
        else:
            next_site = sites[0]
        self._current_site = next_site
        try:
            self.query_one("#site-label", Label).update(f"[{next_site.name}]")
        except NoMatches:
            pass
        self._set_status(f"Switched to {next_site.name}")

    def on_unmount(self) -> None:
        self._db.close()


def run() -> None:
    app = BooruTUI()
    app.run()
