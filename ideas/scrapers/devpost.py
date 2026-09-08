"""Devpost scraper.

Devpost exposes a JSON directory of hackathons:

    GET https://devpost.com/api/hackathons?status[]=ended&page=N

Project galleries are server-rendered HTML on the hackathon subdomain
(``<slug>.devpost.com/project-gallery?page=N``); winners carry an
``<img class="winner">`` ribbon in the card. The individual software page
(``devpost.com/software/<slug>``) holds the full write-up, Built With tags,
links, team and the exact prize name.

Requires a browser User-Agent — Devpost's edge returns 403 otherwise.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, HttpClient
from .models import Prize, Project

log = logging.getLogger(__name__)

DIRECTORY = "https://devpost.com/api/hackathons"


def _txt(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


class DevpostScraper(BaseScraper):
    name = "devpost"

    def __init__(self, client: HttpClient | None = None, fetch_details: bool = True, **kw):
        kw.setdefault("headers", {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
        super().__init__(client, **kw)
        self.fetch_details = fetch_details

    # ---- hackathons ------------------------------------------------------
    def hackathons(self, limit: int | None = None, status: str = "ended", query: str = "") -> Iterator[dict]:
        seen, page = 0, 1
        while True:
            params = {"page": page, "status[]": status}
            if query:
                params["search"] = query
            data = self.client.get_json(DIRECTORY, params=params)
            items = data.get("hackathons") or []
            if not items:
                return
            for h in items:
                url = h.get("url", "")
                yield {
                    "slug": re.sub(r"^https?://([^.]+)\..*", r"\1", url),
                    "name": h.get("title", ""),
                    "url": url,
                    "starts_at": "",
                    "ends_at": h.get("submission_period_dates", ""),
                    "themes": [t.get("name", "") for t in (h.get("themes") or [])],
                    "projects_submitted": h.get("registrations_count", 0),
                    "status": h.get("open_state", ""),
                    "raw": h,
                }
                seen += 1
                if limit and seen >= limit:
                    return
            page += 1

    # ---- projects --------------------------------------------------------
    def projects(
        self,
        hackathon: dict | str | None = None,
        winners_only: bool = True,
        limit: int | None = None,
    ) -> Iterator[Project]:
        if hackathon is None:
            raise ValueError("devpost requires a hackathon url or slug")
        meta = hackathon if isinstance(hackathon, dict) else {"slug": hackathon}
        base = meta.get("url") or f"https://{meta['slug']}.devpost.com/"
        if not base.endswith("/"):
            base += "/"

        seen, page, dry_pages = 0, 1, 0
        while True:
            html = self.client.get_text(urljoin(base, "project-gallery"), params={"page": page})
            soup = BeautifulSoup(html, "lxml")
            if not meta.get("name"):
                og = soup.select_one('meta[property="og:site_name"]')
                meta["name"] = og.get("content", "") if og else ""
                meta.setdefault("url", base)
            cards = soup.select(".gallery-item")
            if not cards:
                return

            # Devpost orders the gallery winners-first (measured on
            # revenuecat-shipaton-2025: page 1 = 24/24 winners, page 2 = 6/24,
            # pages 3+ = 0/24). So once a page has no winner ribbon at all we can
            # stop instead of walking the remaining pages -- 3 requests instead
            # of 34 there, and 1 instead of hundreds for an event whose winners
            # are not announced yet. One page of grace guards against a gallery
            # that does not use that ordering.
            if winners_only:
                if not any(c.select_one("img.winner") for c in cards):
                    dry_pages += 1
                    if dry_pages >= 2:
                        return
                else:
                    dry_pages = 0

            for card in cards:
                is_winner = card.select_one("img.winner") is not None
                if winners_only and not is_winner:
                    continue
                p = self._from_card(card, meta, is_winner)
                if p is None:
                    continue
                if self.fetch_details and p.url:
                    try:
                        self._enrich(p)
                    except Exception as e:  # a single dead page must not kill the run
                        log.warning("devpost detail failed for %s: %s", p.url, e)
                yield p
                seen += 1
                if limit and seen >= limit:
                    return
            page += 1

    # ---- mapping ---------------------------------------------------------
    def _from_card(self, card, meta: dict, is_winner: bool) -> Project | None:
        link = card.select_one("a.link-to-software")
        url = link.get("href", "") if link else ""
        if not url:
            return None
        return Project(
            source=self.name,
            source_id=card.get("data-software-id", "") or url.rstrip("/").split("/")[-1],
            url=url,
            title=_txt(card.select_one(".software-entry-name h5")),
            tagline=_txt(card.select_one("p.tagline")),
            themes=list(meta.get("themes") or []),
            is_winner=is_winner,
            prizes=[Prize(name="Winner", rank="Winner")] if is_winner else [],
            hackathon_name=meta.get("name", ""),
            hackathon_slug=meta.get("slug", ""),
            hackathon_url=meta.get("url", ""),
            hackathon_end=meta.get("ends_at", ""),
            team=[
                x.get("title", "")
                for x in card.select(".members .user-photo")
                if x.get("title")
            ],
            likes=int(_txt(card.select_one(".like-count")) or 0) if card.select_one(".like-count") else 0,
        )

    def _enrich(self, p: Project) -> None:
        """Pull the full write-up from the software detail page."""
        soup = BeautifulSoup(self.client.get_text(p.url), "lxml")

        # read the sidebar data first: the description clean-up below removes
        # these nodes from the tree.
        p.tech_stack = [_txt(t) for t in soup.select("#built-with .cp-tag")]
        links = [a.get("href", "") for a in soup.select('[data-role="software-urls"] a')]

        body = soup.select_one("#app-details-left")
        if body:
            for junk in body.select("#built-with, .app-links, script, style"):
                junk.decompose()
            p.description = body.get_text("\n", strip=True)
            p.problem_solved = self._section(body, r"inspiration|problem|what it does")
            p.challenges = self._section(body, r"challenges")

        p.repo_url = next((l for l in links if "github.com" in l or "gitlab.com" in l), "")
        p.demo_url = next((l for l in links if l and l != p.repo_url), "")
        vid = soup.select_one("#app-video iframe, iframe.video-embed")
        if vid:
            p.video_url = vid.get("src", "")

        team = [_txt(x.select_one("a.user-profile-link")) for x in soup.select("li.software-team-member")]
        team = [t for t in team if t]
        if team:
            p.team = team

        prizes: list[Prize] = []
        for entry in soup.select(".software-list-content"):
            hack = entry.select_one("a")
            for li in entry.select("li"):
                label = li.select_one("span.winner")
                if not label:
                    continue
                rank = _txt(li).replace(_txt(label), "", 1).strip(" -–—") or "Winner"
                prizes.append(Prize(name=rank, rank=rank, sponsor=_txt(hack)))
        if prizes:
            p.prizes = prizes
            p.is_winner = True
        p.raw = {"detail_url": p.url}

    @staticmethod
    def _section(body, pattern: str) -> str:
        rx = re.compile(pattern, re.I)
        for h in body.find_all(["h1", "h2", "h3"]):
            if not rx.search(h.get_text()):
                continue
            out = []
            for sib in h.next_siblings:
                if getattr(sib, "name", None) in {"h1", "h2", "h3"}:
                    break
                t = _txt(sib)
                if t:
                    out.append(t)
            return "\n".join(out).strip()
        return ""
