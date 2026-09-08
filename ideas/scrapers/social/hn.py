"""Hacker News via the Algolia search API — no key, no auth, no scraping.

https://hn.algolia.com/api  •  ~10k requests/hour, JSON, documented.

Pagination note: Algolia caps `page` at 1000 documents total, so a plain
`page=0,1,2,...` walk silently stops after 1000 hits no matter how many the
query reports. `search_by_date` returns strict reverse-chronological order, so
this sweeps with a `created_at_i` cursor instead: every request is page 0 of a
shrinking time window. Depth is then unbounded, and each window is a distinct
URL so the on-disk cache still works.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import SocialPost, SocialScraper, clean

log = logging.getLogger(__name__)

API = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://news.ycombinator.com/item?id="

#: `story` and `comment` are where problems get stated; `ask_hn` is the richest
#: seam of all (people literally posting "how do you deal with X").
DEFAULT_TAGS = ("ask_hn", "story", "comment")

PAGE = 200


class HNScraper(SocialScraper):
    name = "hn"

    def posts(
        self,
        query: str = "",
        since_days: int = 365,
        limit: int | None = None,
        tags: tuple[str, ...] | list[str] = DEFAULT_TAGS,
        **kw,
    ) -> Iterator[SocialPost]:
        since_ts = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
        seen: set[str] = set()
        # Round-robin the tags rather than draining them in order: `ask_hn`
        # alone can exceed any sane `limit`, and the comment stream — where
        # most of the real complaining happens — would then never be reached.
        streams = [self._stream(t, query, since_ts, seen) for t in tags]
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

    def _stream(
        self, tag: str, query: str, since_ts: int, seen: set[str]
    ) -> Iterator[SocialPost]:
        cursor = int(datetime.now(timezone.utc).timestamp()) + 1
        n = 0
        while True:
            params = {
                "query": query,
                "tags": tag,
                "numericFilters": f"created_at_i>{since_ts},created_at_i<{cursor}",
                "hitsPerPage": PAGE,
            }
            data = self.client.get_json(API, params=params)
            hits = data.get("hits") or []
            if not hits:
                return
            for h in hits:
                oid = str(h.get("objectID") or "")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                n += 1
                yield self._post(h, tag)
            log.info("[hn] %s: %d posts", tag, n)
            if len(hits) < PAGE:        # window exhausted, no older page to ask for
                return
            # +1 so items sharing the boundary second are re-fetched rather than
            # skipped; `seen` drops the repeats.
            last = min(int(h.get("created_at_i") or cursor) for h in hits)
            if last + 1 >= cursor:      # no forward progress, bail out
                return
            cursor = last + 1

    # ------------------------------------------------------------------
    def _post(self, h: dict, tag: str) -> SocialPost:
        is_comment = bool(h.get("comment_text"))
        title = clean(h.get("title") or h.get("story_title") or "")
        text = clean(h.get("comment_text") or h.get("story_text") or "")
        return SocialPost(
            source="hn",
            source_id=str(h.get("objectID")),
            url=ITEM + str(h.get("objectID")),
            title="" if is_comment else title,
            # A comment's story title is context, not the comment's own claim,
            # so it is prefixed into the text rather than used as the title.
            text=f"(re: {title}) {text}" if is_comment and title else text,
            author=h.get("author") or "",
            channel=tag,
            posted_at=h.get("created_at") or "",
            points=int(h.get("points") or 0),
            comments=int(h.get("num_comments") or 0),
            raw=h,
        )
