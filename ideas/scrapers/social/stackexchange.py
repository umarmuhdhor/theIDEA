"""Stack Exchange via the official API — no key, no auth, 300 requests/day.

Not just Stack Overflow: the network is 365 sites, and the interesting ones here
are the non-developer ones. `workplace`, `money`, `academia`, `parenting`,
`law`, `travel` and `freelancing` are full of people describing a process that
wastes their week, which is exactly the seam HN cannot reach.

Two deviations from the other sources, both measured rather than assumed:

* `body=` matches a literal phrase and looked ideal, but returns almost nothing
  — 2 hits for "wish there was" across all of `workplace`, ever. `q=` on the
  same phrase returns a full page of 50. So the phrases are sent as `q=` and
  `signals.py` does the precision work locally, as it does for Hacker News.
* `since_days` is **not** sent as `fromdate`. Constrained to the last two years
  these sites return 0-1 results per query: the network's traffic collapsed and
  its value now is the archive, where a question with no good answer is still a
  problem with no good tool. Age is not ignored — `signals.py` decays a row's
  score as it gets older.

Results are sorted by `creation` rather than `votes`. Sorting by votes returns
the sites' all-time greatest hits (measured: 2012-2019, scoring ~0.02 after
recency decay, so they can never surface); newest-first returns 2020-2025 for
the same query and the same quota.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

from .base import SocialPost, SocialScraper, clean
from .signals import SERVER_SIDE_PHRASES

log = logging.getLogger(__name__)

API = "https://api.stackexchange.com/2.3/search/advanced"

#: Deliberately weighted away from programming: `serverfault` and `webapps` are
#: the only technical ones, and both are about *using* tools rather than writing
#: them, which is where tooling gaps show up.
DEFAULT_SITES = (
    "workplace", "money", "academia", "freelancing",
    "webapps", "serverfault", "pm", "law",
)

PAGE = 50


class StackExchangeScraper(SocialScraper):
    name = "stackexchange"

    def posts(
        self,
        query: str = "",
        since_days: int = 365,      # noqa: ARG002 - see the module docstring
        limit: int | None = None,
        channels: list[str] | tuple[str, ...] | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        sites = tuple(channels or DEFAULT_SITES)
        phrases = (query,) if query else SERVER_SIDE_PHRASES

        seen: set[str] = set()
        streams = [self._search(site, ph, seen) for site in sites for ph in phrases]
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

    def _search(self, site: str, phrase: str, seen: set[str]) -> Iterator[SocialPost]:
        page = 1
        while True:
            params = {
                "order": "desc",
                "sort": "creation",
                "q": phrase,
                "site": site,
                "pagesize": PAGE,
                "page": page,
                # `withbody` is the difference between a title and the actual
                # complaint; the default filter omits the question text.
                "filter": "withbody",
            }
            try:
                data = self.client.get_json(API, params=params)
            except Exception as e:
                log.warning("[stackexchange] %s %r failed: %s", site, phrase, e)
                return
            items = data.get("items") or []
            for it in items:
                qid = f"{site}:{it.get('question_id')}"
                if qid in seen:
                    continue
                seen.add(qid)
                yield self._post(it, site)
            quota = data.get("quota_remaining")
            if quota is not None and quota < 10:
                log.warning("[stackexchange] quota nearly exhausted (%s left today)", quota)
                return
            if not data.get("has_more") or not items:
                return
            page += 1

    def _post(self, it: dict, site: str) -> SocialPost:
        created = it.get("creation_date") or 0
        return SocialPost(
            source="stackexchange",
            source_id=f"{site}:{it.get('question_id')}",
            url=it.get("link") or "",
            title=clean(it.get("title")),
            text=clean(it.get("body"))[:4000],
            author=((it.get("owner") or {}).get("display_name") or ""),
            channel=f"se/{site}",
            posted_at=datetime.fromtimestamp(created, timezone.utc).isoformat() if created else "",
            points=int(it.get("score") or 0),
            comments=int(it.get("answer_count") or 0),
            raw={k: it.get(k) for k in (
                "question_id", "title", "link", "score", "answer_count",
                "is_answered", "creation_date", "tags",
            )},
        )
