"""Auto-detect which API type a booru site uses."""

from __future__ import annotations

import httpx

from ..config import USER_AGENT
from .danbooru import DanbooruClient
from .gelbooru import GelbooruClient
from .moebooru import MoebooruClient
from .e621 import E621Client
from .base import BooruClient


async def detect_site_type(
    url: str,
    api_key: str | None = None,
    api_user: str | None = None,
) -> str | None:
    """
    Probe a URL and return the API type string: 'danbooru', 'gelbooru', or 'moebooru'.
    Returns None if detection fails.
    """
    url = url.rstrip("/")

    from .base import BooruClient as _BC
    # Reuse shared client for site detection
    if _BC._shared_client is None or _BC._shared_client.is_closed:
        _BC._shared_client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=20.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    client = _BC._shared_client
    if True:  # keep indent level
        # Try Danbooru / e621 first — /posts.json is a definitive endpoint
        try:
            params: dict = {"limit": 1}
            if api_key and api_user:
                params["login"] = api_user
                params["api_key"] = api_key
            resp = await client.get(f"{url}/posts.json", params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "posts" in data:
                    # e621/e926 wraps in {"posts": [...]}, with nested file/tags dicts
                    posts = data["posts"]
                    if isinstance(posts, list) and posts:
                        p = posts[0]
                        if isinstance(p.get("file"), dict) and isinstance(p.get("tags"), dict):
                            return "e621"
                    return "danbooru"
                elif isinstance(data, list) and data:
                    # Danbooru returns a flat list of post objects
                    if isinstance(data[0], dict) and any(
                        k in data[0] for k in ("tag_string", "image_width", "large_file_url")
                    ):
                        return "danbooru"
            elif resp.status_code in (401, 403):
                if "e621" in url or "e926" in url:
                    return "e621"
                return "danbooru"
        except Exception:
            pass

        # Try Gelbooru — /index.php?page=dapi
        try:
            params = {
                "page": "dapi", "s": "post", "q": "index", "json": "1", "limit": 1,
            }
            if api_key and api_user:
                params["api_key"] = api_key
                params["user_id"] = api_user
            resp = await client.get(f"{url}/index.php", params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    if any(k in data[0] for k in ("file_url", "preview_url", "directory")):
                        return "gelbooru"
                elif isinstance(data, dict):
                    if "post" in data or "@attributes" in data:
                        return "gelbooru"
            elif resp.status_code in (401, 403):
                if "gelbooru" in url or "safebooru.org" in url or "rule34" in url:
                    return "gelbooru"
        except Exception:
            pass

        # Try Moebooru — /post.json (singular)
        try:
            params = {"limit": 1}
            if api_key and api_user:
                params["login"] = api_user
                params["password_hash"] = api_key
            resp = await client.get(f"{url}/post.json", params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) or (isinstance(data, dict) and "posts" in data):
                    return "moebooru"
            elif resp.status_code in (401, 403):
                return "moebooru"
        except Exception:
            pass

    return None


def client_for_type(
    api_type: str,
    base_url: str,
    api_key: str | None = None,
    api_user: str | None = None,
) -> BooruClient:
    """Return the appropriate client class for an API type string."""
    clients = {
        "danbooru": DanbooruClient,
        "gelbooru": GelbooruClient,
        "moebooru": MoebooruClient,
        "e621": E621Client,
    }
    cls = clients.get(api_type)
    if cls is None:
        raise ValueError(f"Unknown API type: {api_type}")
    return cls(base_url, api_key=api_key, api_user=api_user)
