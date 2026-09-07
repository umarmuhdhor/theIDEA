"""Discourse forums — every instance exposes the same JSON API, no auth.

Thousands of niche communities run Discourse, and each one is a room full of
people who share a job and complain about it in that job's own vocabulary.
That specificity is the point: "the export takes four clicks per student" only
ever gets written down somewhere like this.

Any host works — `--channels forum.example.com` — because the endpoints are
identical across instances. The defaults are five large public ones, verified
to answer `/search.json`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import SocialPost, SocialScraper, clean
from .signals import SERVER_SIDE_PHRASES

log = logging.getLogger(__name__)

#: `community.home-assistant.io` answers /search.json from a browser but 403s
#: this client, so it is left out rather than logging a warning on every run.
DEFAULT_HOSTS = (
    "meta.discourse.org",
    "forum.obsidian.md",
    "discourse.mozilla.org",
    "community.openai.com",
)

MAX_PAGES = 5


class DiscourseScraper(SocialScraper):
    name = "discourse"

    def posts(
        self,
        query: str = "",
        since_days: int = 365,
        limit: int | None = None,
        channels: list[str] | tuple[str, ...] | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        hosts = tuple(channels or DEFAULT_HOSTS)
        phrases = (query,) if query else SERVER_SIDE_PHRASES
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

        seen: set[str] = set()
        streams = [self._search(h, ph, cutoff, seen) for h in hosts for ph in phrases]
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

    def _search(self, host: str, phrase: str, cutoff, seen: set[str]) -> Iterator[SocialPost]:
        for page in range(1, MAX_PAGES + 1):
            try:
                data = self.client.get_json(
                    f"https://{host}/search.json",
                    params={"q": f'"{phrase}"', "page": page},
                )
            except Exception as e:
                log.warning("[discourse] %s %r failed: %s", host, phrase, e)
                return
            posts = data.get("posts") or []
            if not posts:
                return
            topics = {t["id"]: t for t in (data.get("topics") or [])}
            for p in posts:
                pid = f"{host}:{p.get('id')}"
                if pid in seen:
                    continue
                seen.add(pid)
                when = _when(p.get("created_at"))
                if when and when < cutoff:
                    continue
                yield self._post(p, topics.get(p.get("topic_id")) or {}, host)

    def _post(self, p: dict, topic: dict, host: str) -> SocialPost:
        slug = topic.get("slug") or "t"
        tid = p.get("topic_id")
        return SocialPost(
            source="discourse",
            source_id=f"{host}:{p.get('id')}",
            url=f"https://{host}/t/{slug}/{tid}/{p.get('post_number', 1)}",
            title=clean(topic.get("title")),
            # Search returns a blurb, not the full post. Enough to fire the pain
            # filter and to read; the full text would cost one request per hit.
            text=clean(p.get("blurb")),
            author=p.get("username") or "",
            channel=host,
            posted_at=p.get("created_at") or "",
            points=int(topic.get("like_count") or p.get("like_count") or 0),
            comments=max(0, int(topic.get("posts_count") or 1) - 1),
            raw={"post_id": p.get("id"), "topic_id": tid, "host": host,
                 "views": topic.get("views"), "category_id": topic.get("category_id")},
        )


def _when(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
