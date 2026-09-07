"""Unstop scraper (hackathon directory only).

Unstop's public API is generous about *events* and silent about *submissions*:

    GET https://unstop.com/api/public/opportunity/search-result?opportunity=hackathons&page=N
    GET https://unstop.com/api/public/competition/<id>

The competition detail carries the prize structure, required skills, organiser,
dates and registration counts — but `teams` and `players` come back empty and
`overall_leaderboard` is 0. Sampling twelve ended hackathons found zero with any
public team or leaderboard data, so there are no winning project write-ups to
scrape here: results live behind a participant/organiser login.

That makes Unstop a useful *discovery* source — 6,200+ hackathons, most of them
Indian campus and corporate events that never appear on Devpost or Devfolio —
but not a source of project ideas. `hackathons()` is fully implemented;
`projects()` deliberately yields nothing and says why.
"""
from __future__ import annotations

import logging
from typing import Iterator

from .base import BaseScraper, HttpClient
from .models import Project

log = logging.getLogger(__name__)

API = "https://unstop.com/api/public"
PAGE = 30


class UnstopScraper(BaseScraper):
    name = "unstop"
    has_project_submissions = False

    def __init__(self, client: HttpClient | None = None, **kw):
        kw.setdefault("headers", {"Accept": "application/json", "Referer": "https://unstop.com/hackathons"})
        super().__init__(client, **kw)

    # ---- hackathons ------------------------------------------------------
    def hackathons(
        self,
        limit: int | None = None,
        query: str = "",
        opportunity: str = "hackathons",
    ) -> Iterator[dict]:
        seen, page = 0, 1
        while True:
            params = {"opportunity": opportunity, "page": page, "per_page": PAGE}
            if query:
                params["searchTerm"] = query
            payload = self.client.get_json(f"{API}/opportunity/search-result", params=params)
            block = payload.get("data") or {}
            items = block.get("data") or []
            if not items:
                return
            for x in items:
                yield self._to_hackathon(x)
                seen += 1
                if limit and seen >= limit:
                    return
            total = block.get("total") or 0
            if page * PAGE >= total:
                return
            page += 1

    def detail(self, opportunity_id: int | str) -> dict:
        """Full competition record: prize breakdown, rounds, skills, organiser."""
        payload = self.client.get_json(f"{API}/competition/{opportunity_id}")
        return (payload.get("data") or {}).get("competition") or {}

    # ---- projects --------------------------------------------------------
    def projects(self, hackathon=None, winners_only: bool = True, limit: int | None = None
                 ) -> Iterator[Project]:
        log.warning(
            "[unstop] no project submissions to scrape: Unstop keeps teams, "
            "leaderboards and results behind a login, so only hackathon metadata "
            "is public. Use `list-hackathons --source unstop` for discovery, then "
            "look the event up on Devpost/Devfolio for the actual projects."
        )
        return
        yield  # pragma: no cover - makes this a generator

    # ---- mapping ---------------------------------------------------------
    @staticmethod
    def _to_hackathon(x: dict) -> dict:
        org = x.get("organisation") or {}
        prizes = x.get("prizes") or []
        themes = []
        for bucket in ("tags", "required_skills", "filters"):
            for t in x.get(bucket) or []:
                name = t.get("name") if isinstance(t, dict) else t
                if name:
                    themes.append(str(name))
        return {
            "slug": x.get("public_url", "") or str(x.get("id", "")),
            "name": x.get("title", ""),
            "url": x.get("seo_url") or f"https://unstop.com/{x.get('public_url','')}",
            "starts_at": x.get("start_date", "") or "",
            "ends_at": x.get("end_date", "") or "",
            "themes": sorted(set(themes)),
            "projects_submitted": x.get("registerCount", 0) or 0,
            "status": x.get("status", "") or "",
            "raw": {
                "id": x.get("id"),
                "organisation": org.get("name", ""),
                "subtype": x.get("subtype", ""),
                "views": x.get("viewsCount", 0),
                "prizes": [
                    {"rank": p.get("rank", ""), "cash": p.get("cash"), "others": p.get("others", "")}
                    for p in prizes if isinstance(p, dict)
                ],
            },
        }
