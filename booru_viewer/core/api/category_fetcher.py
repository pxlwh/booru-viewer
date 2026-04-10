"""Per-post HTML scrape + per-tag cache for boorus that don't return
tag categories inline (Gelbooru-shape, Moebooru).

Optionally accelerated by a batch-tag-API fast path when the attached
BooruClient declares a ``_tag_api_url`` AND has credentials. The fast
path fetches up to 500 tag types per request via the booru's tag DAPI,
avoiding per-post HTML scraping entirely on sites that support it.

The per-post HTML scrape path is the correctness baseline — it works on
every Gelbooru fork and every Moebooru deployment regardless of auth or
API quirks. The batch API is an optimization that short-circuits it
when possible.

Architectural note: Moebooru's ``/tag.json?limit=0`` returns the entire
tag database in one request. A future "download tag database" feature
can pre-populate ``tag_types`` via that endpoint, after which
``try_compose_from_cache`` succeeds for every post without any per-post
HTTP. The cache-compose fast path already supports this — no
CategoryFetcher changes needed, just a new "populate cache from dump"
entry point.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BooruClient, Post
    from ..db import Database

log = logging.getLogger("booru")

# ---------------------------------------------------------------------------
# HTML parser for the universal `class="tag-type-X"` convention
# ---------------------------------------------------------------------------

# Two-pass approach:
#   1. Find each tag-type element and its full inner content.
#   2. Within the content, extract the tag name from the `tags=NAME`
#      URL parameter in the search link.
#
# This handles the cross-site variation cleanly:
#   - Gelbooru proper: only has `?` wiki links (no `tags=` param) →
#     returns 0 results, which is fine because Gelbooru uses the
#     batch tag API instead of HTML scraping.
#   - Rule34 / Safebooru.org: two <a> links per tag — `?` wiki link
#     + `<a href="...tags=TAGNAME">display name</a>`. We extract from
#     the URL, not the display text.
#   - yande.re / Konachan (Moebooru): same two-link pattern, but the
#     URL is `/post?tags=TAGNAME` instead of `page=post&s=list&tags=`.
#
# The `tags=` extraction gives us the canonical underscore form
# directly from the URL, no display-text normalization needed.
_TAG_ELEMENT_RE = re.compile(
    r'class="[^"]*tag-type-([a-z]+)[^"]*"[^>]*>'  # class containing tag-type-NAME
    r'(.*?)'                                        # inner content (lazy)
    r'</(?:li|span|td|div)>',                       # closing tag
    re.DOTALL,
)
_TAG_NAME_RE = re.compile(r'tags=([^&"<>\s]+)')

# HTML class name -> Capitalized label (matches danbooru.py / e621.py)
_LABEL_MAP: dict[str, str] = {
    "general":   "General",
    "artist":    "Artist",
    "character": "Character",
    "copyright": "Copyright",
    "metadata":  "Meta",
    "meta":      "Meta",
    "species":   "Species",
    "circle":    "Circle",
    "style":     "Style",
}

# Gelbooru tag DAPI integer code -> Capitalized label (for fetch_via_tag_api)
_GELBOORU_TYPE_MAP: dict[int, str] = {
    0: "General",
    1: "Artist",
    3: "Copyright",
    4: "Character",
    5: "Meta",
    # 2 = Deprecated — intentionally omitted
}

# Canonical display order for category-grouped tags.  Matches the
# insertion order danbooru.py and e621.py produce for their inline
# categorization, so the info panel renders consistently across all
# booru types.
_CATEGORY_ORDER = [
    "Artist", "Character", "Copyright", "Species",
    "General", "Meta", "Lore",
]


# ---------------------------------------------------------------------------
# CategoryFetcher
# ---------------------------------------------------------------------------

class CategoryFetcher:
    """Fetch and cache tag categories for boorus without inline data.

    Three entry points share one cache:

    * ``try_compose_from_cache`` — instant, no HTTP.
    * ``fetch_via_tag_api`` — batch fast path for Gelbooru proper.
    * ``fetch_post`` — per-post HTML scrape, universal fallback.

    ``ensure_categories`` and ``prefetch_batch`` are the public
    dispatch methods that route through these.
    """

    _PREFETCH_CONCURRENCY = 3  # safebooru.org soft-limits at >3

    def __init__(
        self,
        client: "BooruClient",
        db: "Database",
        site_id: int,
    ) -> None:
        self._client = client
        self._db = db
        self._site_id = site_id
        self._sem = asyncio.Semaphore(self._PREFETCH_CONCURRENCY)
        self._inflight: dict[int, asyncio.Task] = {}

        self._batch_api_works: bool | None = None
        # Probe state for the batch tag API:
        #
        #   None  — not yet probed, OR last probe hit a transient
        #           error (HTTP error, timeout, parse exception).
        #           Next prefetch_batch will retry the probe.
        #   True  — probe succeeded (response contained >=1 of the
        #           requested names). Batch API used for all future
        #           calls on this instance.
        #   False — probe got a clean HTTP 200 with zero matching
        #           names for ANY of the requested tags.  The API
        #           is structurally broken on this site (Rule34's
        #           ``names=`` filter returns unrelated tags).
        #           Per-post HTML used for all future calls.
        #
        # Transition to False is permanent for the instance lifetime.
        # Transition to True is permanent for the instance lifetime.
        # None -> None on transient error preserves retry ability.

    # ----- cache compose (instant, no HTTP) -----

    def try_compose_from_cache(self, post: "Post") -> bool:
        """Build ``post.tag_categories`` from cached labels.

        Populates ``post.tag_categories`` with whatever tags ARE
        cached, even if some are missing.  Returns True when at least
        one tag was categorized (meaning the post is usable — the
        info panel can render categories, templates can resolve
        ``%artist%`` / ``%character%`` etc.).  Returns False only
        when the cache has literally nothing for any of the post's
        tags, which means a fetch is needed.

        Tags not in the cache are simply absent from the category
        dict.  They stay in ``post.tags`` (the flat string) and can
        be picked up by a later per-post fetch if needed.
        """
        tags = post.tag_list
        if not tags:
            return True
        cached = self._db.get_tag_labels(self._site_id, tags)
        if not cached:
            return False
        cats: dict[str, list[str]] = {}
        for tag in tags:
            label = cached.get(tag)
            if label:
                cats.setdefault(label, []).append(tag)
        if cats:
            post.tag_categories = _canonical_order(cats)
        return bool(cats)

    # ----- batch tag API fast path -----

    def _batch_api_available(self) -> bool:
        """True when the attached client declares a tag API endpoint
        AND has credentials configured."""
        return (
            self._client._tag_api_url() is not None
            and bool(self._client.api_key)
            and bool(self._client.api_user)
        )

    async def fetch_via_tag_api(self, posts: list["Post"]) -> int:
        """Batch-fetch tag types via the booru's tag DAPI.

        Collects every unique uncached tag name across ``posts``,
        chunks into 500-name batches, GETs the tag DAPI for each
        chunk, writes the results to the cache, then runs
        ``try_compose_from_cache`` on every post.

        Returns the count of newly-cached tags.
        """
        # Collect unique uncached tag names
        all_tags: set[str] = set()
        for p in posts:
            all_tags.update(p.tag_list)
        if not all_tags:
            return 0
        cached = self._db.get_tag_labels(self._site_id, list(all_tags))
        missing = [t for t in all_tags if t not in cached]
        if not missing:
            for p in posts:
                self.try_compose_from_cache(p)
            return 0

        tag_api_url = self._client._tag_api_url()
        if tag_api_url is None:
            return 0

        new_labels: dict[str, str] = {}
        BATCH = 500
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            params: dict = {
                "page": "dapi",
                "s": "tag",
                "q": "index",
                "json": "1",
                "names": " ".join(chunk),
                "limit": len(chunk),
            }
            if self._client.api_key and self._client.api_user:
                key = self._client.api_key.strip().lstrip("&")
                user = self._client.api_user.strip().lstrip("&")
                if key and not key.startswith("api_key="):
                    params["api_key"] = key
                if user and not user.startswith("user_id="):
                    params["user_id"] = user
            try:
                resp = await self._client._request("GET", tag_api_url, params=params)
                resp.raise_for_status()
            except Exception as e:
                log.warning("Batch tag API failed (%d names): %s: %s",
                            len(chunk), type(e).__name__, e)
                continue
            for name, type_int in _parse_tag_response(resp):
                label = _GELBOORU_TYPE_MAP.get(type_int)
                if label:
                    new_labels[name] = label

        if new_labels:
            self._db.set_tag_labels(self._site_id, new_labels)
        # Compose from the now-warm cache
        for p in posts:
            self.try_compose_from_cache(p)
        return len(new_labels)

    # ----- per-post HTML scrape (universal fallback) -----

    async def fetch_post(self, post: "Post") -> bool:
        """Scrape the post-view HTML page for categorized tags.

        Works on every Gelbooru fork and every Moebooru deployment.
        Does NOT require auth.  Returns True on success.
        """
        url = self._client._post_view_url(post)
        if url is None:
            return False
        async with self._sem:
            try:
                resp = await self._client._request("GET", url)
                resp.raise_for_status()
            except Exception as e:
                log.warning("Category HTML fetch for #%d failed: %s: %s",
                            post.id, type(e).__name__, e)
                return False
        cats, labels = _parse_post_html(resp.text)
        if not cats:
            return False
        post.tag_categories = _canonical_order(cats)
        if labels:
            self._db.set_tag_labels(self._site_id, labels)
        return True

    # ----- dispatch: ensure (single post) -----

    async def ensure_categories(self, post: "Post") -> None:
        """Idempotent.  Guarantee ``post.tag_categories`` is populated.

        Dispatch:
          1. Already populated → return.
          2. Cache compose → return if complete.
          3. Batch tag API (if available + probe passed) → return.
          4. Per-post HTML scrape → return.

        Coalesces concurrent calls for the same ``post.id``.
        """
        if post.tag_categories:
            return
        if self.try_compose_from_cache(post):
            return

        # Coalesce: if there's an in-flight fetch for this post, await it
        existing = self._inflight.get(post.id)
        if existing is not None and not existing.done():
            await existing
            return

        task = asyncio.create_task(self._do_ensure(post))
        self._inflight[post.id] = task
        try:
            await task
        finally:
            self._inflight.pop(post.id, None)

    async def _do_ensure(self, post: "Post") -> None:
        """Inner dispatch for ensure_categories."""
        # Batch API path (for single-post ensure, e.g. click or save)
        if self._batch_api_works is True and self._batch_api_available():
            await self.fetch_via_tag_api([post])
            if post.tag_categories:
                return
        # HTML fallback
        await self.fetch_post(post)

    # ----- dispatch: prefetch (batch, fire-and-forget) -----

    async def prefetch_batch(self, posts: list["Post"]) -> None:
        """Background prefetch for a page of search results.

        ONE fetch path per invocation — no mixing batch API + HTML
        scrape in the same call.

        Dispatch (exactly one branch executes per call):

          a. ``_batch_api_works is True``
             → ``fetch_via_tag_api`` for all uncached posts.

          b. ``_batch_api_works is None`` AND capability check passes
             → ``fetch_via_tag_api`` as the probe.
               - HTTP 200 + >=1 requested name matched
                 → ``_batch_api_works = True``.  Done.
               - HTTP 200 + 0 requested names matched
                 → ``_batch_api_works = False``.  Stop.
                   Do NOT fall through to HTML in this call.
               - HTTP error / timeout / parse exception
                 → ``_batch_api_works`` stays None.  Stop.
                   Next call retries the probe.

          c. ``_batch_api_works is False``, OR no ``_tag_api_url``,
             OR no auth
             → per-post ``ensure_categories`` for each uncached post,
               bounded by ``Semaphore(_PREFETCH_CONCURRENCY)``.
        """
        # Step 1: cache-compose everything we can
        uncached: list["Post"] = []
        for p in posts:
            if p.tag_categories:
                continue
            if not self.try_compose_from_cache(p):
                uncached.append(p)
        if not uncached:
            return

        # Step 2: route decision
        if self._batch_api_works is True and self._batch_api_available():
            # Branch (a): batch API known to work
            try:
                await self.fetch_via_tag_api(uncached)
            except Exception as e:
                log.warning("Batch prefetch failed: %s: %s", type(e).__name__, e)
            return

        if self._batch_api_works is None and self._batch_api_available():
            # Branch (b): probe
            try:
                result = await self._probe_batch_api(uncached)
            except Exception as e:
                # Transient error → leave _batch_api_works = None, stop
                log.info("Batch API probe error (will retry next search): %s: %s",
                         type(e).__name__, e)
                return
            if result is True:
                # Probe succeeded — results already cached, posts composed
                return
            elif result is False:
                # Probe failed cleanly — stop, don't fall through to HTML
                return
            else:
                # result is None — transient, stop, retry next call
                return

        # Branch (c): per-post HTML scrape
        tasks = []
        for p in uncached:
            if not p.tag_categories:
                tasks.append(asyncio.create_task(self.ensure_categories(p)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_batch_api(self, posts: list["Post"]) -> bool | None:
        """Probe whether the batch tag API works on this site.

        Returns:
          True  — probe succeeded, _batch_api_works set to True,
                  results already cached.
          False — clean HTTP 200 with 0 matching names,
                  _batch_api_works set to False.
          None  — transient error, _batch_api_works stays None.
        """
        # Collect a sample of uncached tag names for the probe
        all_tags: set[str] = set()
        for p in posts:
            all_tags.update(p.tag_list)
        cached = self._db.get_tag_labels(self._site_id, list(all_tags))
        missing = [t for t in all_tags if t not in cached]
        if not missing:
            # Everything's cached — can't probe, assume batch works
            self._batch_api_works = True
            for p in posts:
                self.try_compose_from_cache(p)
            return True

        tag_api_url = self._client._tag_api_url()
        if tag_api_url is None:
            return None

        # Send one batch request
        chunk = missing[:500]
        params: dict = {
            "page": "dapi",
            "s": "tag",
            "q": "index",
            "json": "1",
            "names": " ".join(chunk),
            "limit": len(chunk),
        }
        if self._client.api_key and self._client.api_user:
            key = self._client.api_key.strip().lstrip("&")
            user = self._client.api_user.strip().lstrip("&")
            if key and not key.startswith("api_key="):
                params["api_key"] = key
            if user and not user.startswith("user_id="):
                params["user_id"] = user

        try:
            resp = await self._client._request("GET", tag_api_url, params=params)
        except Exception:
            # Network/timeout error → transient, leave None
            return None

        if resp.status_code != 200:
            # Non-200 → transient, leave None
            return None

        try:
            entries = list(_parse_tag_response(resp))
        except Exception:
            # Parse error → transient, leave None
            return None

        # Check if ANY of the returned names match what we asked for
        asked = set(chunk)
        matched: dict[str, str] = {}
        for name, type_int in entries:
            label = _GELBOORU_TYPE_MAP.get(type_int)
            if label:
                matched[name] = label

        got_any = any(n in asked for n in matched)

        if got_any:
            self._batch_api_works = True
            if matched:
                self._db.set_tag_labels(self._site_id, matched)
            # Fetch any remaining missing tags via the batch path
            await self.fetch_via_tag_api(posts)
            return True
        else:
            # Clean 200 but zero matching names → structurally broken
            self._batch_api_works = False
            return False


# ---------------------------------------------------------------------------
# Parsers (module-level, stateless)
# ---------------------------------------------------------------------------

def _parse_post_html(html: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Extract tag categories from a Gelbooru-shape / Moebooru post-view page.

    Returns ``(categories_dict, labels_dict)`` where:
      - ``categories_dict`` is ``{label: [tag_names]}`` ready for
        ``post.tag_categories``.
      - ``labels_dict`` is ``{tag_name: label}`` ready for
        ``db.set_tag_labels``.

    Uses a two-pass approach: find each ``tag-type-X`` element, then
    extract the tag name from the ``tags=NAME`` URL parameter inside
    the element's links. This avoids the `?` wiki-link ambiguity
    (Gelbooru-forks have a ``?`` link before the actual tag link).
    Returns empty on Gelbooru proper (whose post page only has ``?``
    links with no ``tags=`` parameter); that's fine because Gelbooru
    uses the batch tag API instead.
    """
    from urllib.parse import unquote

    cats: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for m in _TAG_ELEMENT_RE.finditer(html):
        type_class = m.group(1).lower()
        content = m.group(2)
        label = _LABEL_MAP.get(type_class)
        if not label:
            continue
        tag_match = _TAG_NAME_RE.search(content)
        if not tag_match:
            continue
        tag_name = unquote(tag_match.group(1)).strip().lower()
        if not tag_name:
            continue
        cats.setdefault(label, []).append(tag_name)
        labels[tag_name] = label
    return cats, labels


def _parse_tag_response(resp) -> list[tuple[str, int]]:
    """Parse a Gelbooru-shaped tag DAPI response, JSON or XML.

    Gelbooru proper honors ``json=1`` and returns JSON.  Rule34 and
    Safebooru.org return XML even with ``json=1``.  We sniff the
    body's first non-whitespace char to choose a parser.

    Returns ``[(name, type_int), ...]``.
    """
    body = resp.text.lstrip()
    if not body:
        return []
    out: list[tuple[str, int]] = []
    if body.startswith("<"):
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            log.warning("Tag XML parse failed: %s", e)
            return []
        for tag in root.iter("tag"):
            name = tag.get("name")
            type_val = tag.get("type")
            if name and type_val is not None:
                try:
                    out.append((name, int(type_val)))
                except (ValueError, TypeError):
                    pass
    else:
        try:
            data = resp.json()
        except Exception as e:
            log.warning("Tag JSON parse failed: %s", e)
            return []
        if isinstance(data, dict):
            data = data.get("tag", [])
        if not isinstance(data, list):
            return []
        for entry in data:
            name = entry.get("name")
            type_val = entry.get("type")
            if name and type_val is not None:
                try:
                    out.append((name, int(type_val)))
                except (ValueError, TypeError):
                    pass
    return out


def _canonical_order(cats: dict[str, list[str]]) -> dict[str, list[str]]:
    """Reorder to Artist > Character > Copyright > ... > Meta."""
    ordered: dict[str, list[str]] = {}
    for label in _CATEGORY_ORDER:
        if label in cats:
            ordered[label] = cats[label]
    for label in cats:
        if label not in ordered:
            ordered[label] = cats[label]
    return ordered
