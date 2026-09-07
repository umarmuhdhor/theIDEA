"""App Store reviews via Apple's public RSS feed — no key, no auth.

The most direct pain source there is. A one-star review is somebody describing,
unprompted, exactly what a product failed to do for them, and the people writing
them are not developers — which is the gap every other source in this project
has.

Two steps, because reviews are per app: the iTunes Search API turns a topic
("restaurant pos", "invoicing") into app ids, then the RSS feed returns their
reviews. Pass numeric ids as `--channels` to skip the discovery step.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from .base import SocialPost, SocialScraper, clean

log = logging.getLogger(__name__)

SEARCH = "https://itunes.apple.com/search"
REVIEWS = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app}/sortBy=mostRecent/json"

COUNTRY = "us"
MAX_PAGES = 10          # Apple stops serving past ~10 pages of 50
APPS_PER_QUERY = 8

#: Only complaints. A five-star review is a testimonial, not a pain point.
MAX_RATING = 3

DEFAULT_QUERIES = (
    "invoicing small business", "restaurant pos", "staff scheduling",
    "inventory management", "expense tracking",
)


class AppStoreScraper(SocialScraper):
    name = "appstore"

    def posts(
        self,
        query: str = "",
        since_days: int = 365,      # the feed is "most recent" only; no date filter
        limit: int | None = None,
        channels: list[str] | tuple[str, ...] | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        apps: list[tuple[str, str]] = []
        if channels:
            apps = [(str(c), str(c)) for c in channels if str(c).isdigit()]
            for term in (c for c in channels if not str(c).isdigit()):
                apps += self._discover(term)
        else:
            for term in ((query,) if query else DEFAULT_QUERIES):
                apps += self._discover(term)
        if not apps:
            log.warning("[appstore] no apps found for %r", query or DEFAULT_QUERIES)
            return

        seen: set[str] = set()
        streams = [self._reviews(aid, name, seen) for aid, name in apps]
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

    def _discover(self, term: str) -> list[tuple[str, str]]:
        try:
            data = self.client.get_json(SEARCH, params={
                "term": term, "entity": "software", "country": COUNTRY,
                "limit": APPS_PER_QUERY,
            })
        except Exception as e:
            log.warning("[appstore] search %r failed: %s", term, e)
            return []
        out = [(str(r["trackId"]), r.get("trackName") or str(r["trackId"]))
               for r in (data.get("results") or []) if r.get("trackId")]
        log.info("[appstore] %r -> %d apps", term, len(out))
        return out

    def _reviews(self, app_id: str, app_name: str, seen: set[str]) -> Iterator[SocialPost]:
        for page in range(1, MAX_PAGES + 1):
            try:
                data = self.client.get_json(
                    REVIEWS.format(country=COUNTRY, page=page, app=app_id)
                )
            except Exception as e:
                log.warning("[appstore] %s page %d failed: %s", app_name, page, e)
                return
            entries = ((data.get("feed") or {}).get("entry")) or []
            if isinstance(entries, dict):          # a single review is not a list
                entries = [entries]
            found = 0
            for e in entries:
                rating = _label(e, "im:rating")
                if not rating:                     # page 1 leads with the app itself
                    continue
                if int(rating) > MAX_RATING:
                    continue
                rid = _label(e, "id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                found += 1
                yield self._post(e, app_id, app_name, int(rating))
            if not entries:
                return

    def _post(self, e: dict, app_id: str, app_name: str, rating: int) -> SocialPost:
        return SocialPost(
            source="appstore",
            source_id=f"{app_id}:{_label(e, 'id')}",
            url=((e.get("author") or {}).get("uri") or {}).get("label", "")
                or f"https://apps.apple.com/{COUNTRY}/app/id{app_id}",
            title=clean(_label(e, "title")),
            text=clean(_label(e, "content")),
            author=(((e.get("author") or {}).get("name") or {}).get("label") or ""),
            channel=f"app/{app_name}"[:80],
            posted_at=_label(e, "updated"),
            # Reviews carry no engagement signal, so the score for these rows is
            # the pain phrases and recency alone. Not faked into looking bigger.
            points=int(_label(e, "im:voteSum") or 0),
            comments=0,
            raw={"rating": rating, "app_id": app_id, "app": app_name,
                 "version": _label(e, "im:version")},
        )


def _label(entry: dict, key: str) -> str:
    v: Any = entry.get(key)
    if isinstance(v, dict):
        return str(v.get("label") or "")
    return str(v or "")
