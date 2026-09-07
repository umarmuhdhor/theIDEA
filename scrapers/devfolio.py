"""Devfolio scraper.

Devfolio powers per-hackathon subdomains such as https://onchain-summer.devfolio.co.
Both the hackathon directory and the project gallery are backed by a public
Elasticsearch-style search API, so no HTML parsing is required.

    POST https://api.devfolio.co/api/search/hackathons
    POST https://api.devfolio.co/api/search/projects   (filter="winners")

A single search hit already carries the full write-up, tech stack, team,
prize tracks and prizes, so one request per page of 50 is all we need.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from .base import BaseScraper, HttpClient
from .models import Prize, Project

log = logging.getLogger(__name__)

API = "https://api.devfolio.co/api"
# Page size matters for completeness, not just speed: the ranking ties across
# shards, so every from/size boundary is a chance to drop a document. A full
# sweep of onchain-summer's 575 winners yields 555 distinct projects at
# size=50 and 574 at size=200 -- but the two sweeps miss *different* documents,
# so alternating the page size across sweeps recovers all 575.
PAGE = 200
SWEEP_SIZES = (200, 50, 100, 25)
ES_WINDOW = 10_000  # Elasticsearch from+size ceiling
MAX_SWEEPS = 4      # re-sweeps to recover the documents a single sweep still skips

# prizes that mean "you showed up", not "you won"
PARTICIPATION_RE = re.compile(r"participation|attendance|swag|consolation", re.I)


class DevfolioScraper(BaseScraper):
    name = "devfolio"

    def __init__(self, client: HttpClient | None = None, exclude_participation: bool = True, **kw):
        kw.setdefault("headers", {"Origin": "https://devfolio.co", "Referer": "https://devfolio.co/"})
        super().__init__(client, **kw)
        self.exclude_participation = exclude_participation

    # ---- hackathons ------------------------------------------------------
    def hackathons(self, limit: int | None = None, query: str = "") -> Iterator[dict]:
        """Yield hackathon records (newest-indexed first). ~1.7k available."""
        # Same unstable ranking as the project search, so de-duplicate here too:
        # a repeated slug would mean re-crawling that hackathon's whole gallery.
        seen = 0
        seen_slugs: set[str] = set()
        for offset in range(0, ES_WINDOW, PAGE):
            data = self.client.post_json(
                f"{API}/search/hackathons",
                {"q": query, "from": offset, "size": PAGE},
            )
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                return
            for h in hits:
                src = h.get("_source", {})
                slug = src.get("slug", "")
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                yield {
                    "slug": src.get("slug", ""),
                    "name": src.get("name", ""),
                    "url": f"https://{src.get('slug','')}.devfolio.co",
                    "starts_at": src.get("starts_at", "") or "",
                    "ends_at": src.get("ends_at", "") or "",
                    "themes": [t.get("name", "") for t in (src.get("themes") or []) if isinstance(t, dict)],
                    "projects_submitted": src.get("projects_submitted", 0),
                    "status": src.get("status", ""),
                    "raw": src,
                }
                seen += 1
                if limit and seen >= limit:
                    return

    # ---- projects --------------------------------------------------------
    def projects(
        self,
        hackathon: dict | str | None = None,
        winners_only: bool = True,
        limit: int | None = None,
    ) -> Iterator[Project]:
        if hackathon is None:
            raise ValueError("devfolio requires a hackathon slug")
        meta = hackathon if isinstance(hackathon, dict) else {"slug": hackathon}
        slug = meta["slug"]
        subdomain = f"https://{slug}.devfolio.co"

        payload_base = {
            "hackathon_slugs": [slug],
            "q": "",
            "filter": "winners" if winners_only else "all",
            "prizes": [],
            "prize_tracks": [],
            "tracks": [],
            "size": PAGE,
        }
        headers = {"Origin": subdomain, "Referer": f"{subdomain}/"}

        # The API ranks by relevance across several Elasticsearch shards and the
        # ordering is not a total order, so scores tie and from/size windows both
        # repeat and skip documents. One linear sweep of onchain-summer returns
        # 575 hits containing only 553 distinct projects. Sweep repeatedly,
        # de-duplicating on uuid, until a pass turns up nothing new.
        seen_ids: set[str] = set()
        emitted = 0
        total: int | None = None

        for sweep in range(MAX_SWEEPS):
            added = 0
            size = SWEEP_SIZES[sweep % len(SWEEP_SIZES)]
            # Later sweeps must bypass the response cache, otherwise they replay
            # an earlier sweep's pages and can never surface a missed document.
            max_age = 0 if sweep else 86400 * 7
            for offset in range(0, min(total or ES_WINDOW, ES_WINDOW), size):
                data = self.client.post_json(
                    f"{API}/search/projects",
                    {**payload_base, "size": size, "from": offset},
                    max_age=max_age,
                    headers=headers,
                )
                if total is None:
                    total = data.get("hits", {}).get("total", {}).get("value", 0)
                hits = data.get("hits", {}).get("hits", [])
                if not hits:
                    break
                for h in hits:
                    src = h.get("_source", {})
                    uid = src.get("uuid") or src.get("slug")
                    if not uid or uid in seen_ids:
                        continue
                    seen_ids.add(uid)
                    added += 1
                    p = self._to_project(src, meta)
                    if p is None:
                        continue
                    yield p
                    emitted += 1
                    if limit and emitted >= limit:
                        return
            log.info(
                "[devfolio] %s sweep %d (size=%d): %d unique of %s reported",
                slug, sweep + 1, size, len(seen_ids), total,
            )
            # `total is not None`, not `total` -- a hackathon with zero winners
            # reports total 0, which is falsy, and would otherwise run every
            # remaining sweep against an empty result set.
            if total is not None and len(seen_ids) >= total:
                return
            # Don't stop on a single empty sweep: each sweep uses a different
            # page size and they miss different documents, so a sweep that adds
            # nothing says nothing about the next one. Only give up once every
            # page size has been tried and the last one still found nothing.
            if sweep >= len(SWEEP_SIZES) - 1 and added == 0:
                return
        log.warning(
            "[devfolio] %s: recovered %d of %d reported after %d sweeps",
            slug, len(seen_ids), total, MAX_SWEEPS,
        )

    # ---- mapping ---------------------------------------------------------
    def _to_project(self, s: dict, meta: dict) -> Project | None:
        if s.get("hidden") or s.get("banned") or s.get("flagged"):
            return None

        prizes: list[Prize] = []
        for pr in s.get("prizes") or []:
            name = pr.get("name", "")
            if self.exclude_participation and PARTICIPATION_RE.search(name):
                continue
            sponsors = pr.get("sponsors") or []
            prizes.append(
                Prize(
                    name=name,
                    rank=name,
                    sponsor=", ".join(x.get("name", "") for x in sponsors if isinstance(x, dict)),
                    description=(pr.get("desc") or "")[:1000],
                )
            )

        sections = {d.get("title", ""): d.get("content", "") for d in (s.get("description") or [])}
        problem = next((v for k, v in sections.items() if "problem" in k.lower()), "")
        challenges = next((v for k, v in sections.items() if "challenge" in k.lower()), "")
        full = "\n\n".join(f"## {k}\n{v}" for k, v in sections.items() if v)

        links = self._split_links(s.get("links"))
        repo = next((l for l in links if "github.com" in l or "gitlab.com" in l), "")
        demo = next((l for l in links if l != repo), "")

        slug = s.get("slug", "")
        return Project(
            source=self.name,
            source_id=s.get("uuid", "") or slug,
            url=f"https://devfolio.co/projects/{slug}" if slug else "",
            title=s.get("name", ""),
            tagline=s.get("tagline", "") or "",
            description=full,
            problem_solved=problem,
            challenges=challenges,
            themes=[t.get("name", "") for t in (s.get("tracks") or []) if isinstance(t, dict)],
            tracks=[t.get("name", "") for t in (s.get("prize_tracks") or []) if isinstance(t, dict)],
            tech_stack=[t.get("name", "") for t in (s.get("hashtags") or []) if isinstance(t, dict)],
            platforms=list(s.get("platforms") or []),
            is_winner=bool(prizes),
            prizes=prizes,
            hackathon_name=meta.get("name", "") or (s.get("hackathon") or {}).get("name", ""),
            hackathon_slug=meta.get("slug", ""),
            hackathon_url=meta.get("url", ""),
            hackathon_start=meta.get("starts_at", ""),
            hackathon_end=meta.get("ends_at", ""),
            team=[
                " ".join(filter(None, [m.get("first_name"), m.get("last_name")])).strip()
                or m.get("username", "")
                for m in (s.get("members") or [])
            ],
            demo_url=demo,
            repo_url=repo,
            video_url=s.get("video_url", "") or "",
            likes=s.get("likes", 0) or 0,
            views=s.get("views", 0) or 0,
            submitted_at=s.get("published_at", "") or s.get("created_at", "") or "",
            raw=s,
        )

    @staticmethod
    def _split_links(raw) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            parts = raw
        else:
            parts = re.split(r"[,\s]+", str(raw))
        return [p.strip() for p in parts if p and p.strip().startswith("http")]
