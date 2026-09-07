"""Lemmy — the federated Reddit-alike, open API, no auth.

Worth having precisely because Reddit is walled: same shape of content (a person
posting a grievance to a topical community, with votes and comments as a
severity signal) and no credentials to obtain.

Its search is fuzzy rather than phrase-exact, so unlike Reddit and Stack
Exchange the filtering cannot be pushed server-side. Pain phrases are sent as
plain terms to narrow the pull, and `signals.py` still decides what is kept.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import SocialPost, SocialScraper, clean
from .signals import SERVER_SIDE_PHRASES

log = logging.getLogger(__name__)

DEFAULT_INSTANCE = "lemmy.world"
PAGE = 50
MAX_PAGES = 4


class LemmyScraper(SocialScraper):
    name = "lemmy"

    def __init__(self, *a, instance: str = DEFAULT_INSTANCE, **kw):
        super().__init__(*a, **kw)
        self.instance = instance

    def posts(
        self,
        query: str = "",
        since_days: int = 365,
        limit: int | None = None,
        channels: list[str] | tuple[str, ...] | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        communities = tuple(channels or ("",))     # "" = the whole instance
        terms = (query,) if query else SERVER_SIDE_PHRASES
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

        seen: set[str] = set()
        streams = [self._search(c, t, cutoff, seen) for c in communities for t in terms]
        n = 0
        while streams:
            for stream in list(streams):
                try:
                    yield next(stream)
                except StopIteration:
                    streams.remove(stream)
                    continue
                n += 1
                if limit and n >= limit:
                    return

    def _search(self, community: str, term: str, cutoff, seen: set[str]) -> Iterator[SocialPost]:
        for page in range(1, MAX_PAGES + 1):
            params = {"q": term, "type_": "Posts", "sort": "TopAll",
                      "limit": PAGE, "page": page}
            if community:
                params["community_name"] = community
            try:
                data = self.client.get_json(
                    f"https://{self.instance}/api/v3/search", params=params
                )
            except Exception as e:
                log.warning("[lemmy] %r %r failed: %s", community, term, e)
                return
            posts = data.get("posts") or []
            if not posts:
                return
            for view in posts:
                post = view.get("post") or {}
                pid = str(post.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                when = _when(post.get("published"))
                if when and when < cutoff:
                    continue
                yield self._post(view)

    def _post(self, view: dict) -> SocialPost:
        post = view.get("post") or {}
        counts = view.get("counts") or {}
        community = (view.get("community") or {}).get("name") or ""
        return SocialPost(
            source="lemmy",
            source_id=str(post.get("id")),
            url=post.get("ap_id") or f"https://{self.instance}/post/{post.get('id')}",
            title=clean(post.get("name")),
            text=clean(post.get("body")),
            author=((view.get("creator") or {}).get("name") or ""),
            channel=f"c/{community}",
            posted_at=post.get("published") or "",
            points=int(counts.get("score") or 0),
            comments=int(counts.get("comments") or 0),
            raw={"id": post.get("id"), "community": community,
                 "upvotes": counts.get("upvotes"), "downvotes": counts.get("downvotes")},
        )


def _when(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
