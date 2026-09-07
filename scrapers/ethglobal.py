"""ETHGlobal scraper.

ETHGlobal is a Next.js app-router site with no public REST API, but every page
ships its React Server Component "flight" payload inline in
``self.__next_f.push([1, "..."])`` calls. That payload contains fully-formed
project objects — including the prizes each project won — so we parse it
directly instead of scraping the DOM.

    https://ethglobal.com/showcase?page=N            -> list of projects + prizes
    https://ethglobal.com/showcase/<slug>-<uuid>     -> full description + howItsMade
    https://ethglobal.com/showcase?events=<slug>     -> single event

Long text fields arrive as RSC references (``"description": "$29"``) that are
resolved from ``29:T<hexlen>,<text>`` markers elsewhere in the stream.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from .base import BaseScraper, HttpClient
from .models import Prize, Project

log = logging.getLogger(__name__)

BASE = "https://ethglobal.com"
CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
TEXT_REF_RE = re.compile(r"(?<![0-9a-zA-Z]):?\b([0-9a-f]{1,5}):T([0-9a-f]+),")
TECH_RE = re.compile(
    r"\b(Solidity|Foundry|Hardhat|Next\.js|React Native|React|TypeScript|Python|Rust|"
    r"Circom|Noir|Halo2|The Graph|IPFS|Chainlink|Uniswap|viem|wagmi|ethers\.js|"
    r"Supabase|Postgres|Redis|Vercel|Tailwind|Solana|Arbitrum|Optimism|zkSync|"
    r"Farcaster|Hedera|Flow|Polygon|LayerZero|Pyth|Privy|Dynamic|Sign Protocol)\b"
)
STR_REF_RE = re.compile(r'(?<![0-9a-zA-Z])([0-9a-f]{1,5}):"((?:[^"\\]|\\.)*)"')


def flight_payload(html: str) -> str:
    """Concatenate and unescape the RSC flight stream embedded in the page."""
    chunks = CHUNK_RE.findall(html)
    if not chunks:
        return ""
    try:
        return json.loads('"' + "".join(chunks) + '"')
    except json.JSONDecodeError:
        return "".join(chunks).encode().decode("unicode_escape", errors="ignore")


def ref_table(payload: str) -> dict[str, str]:
    """Map RSC ids ("29") to their resolved text content."""
    refs: dict[str, str] = {}
    for m in TEXT_REF_RE.finditer(payload):
        rid, nbytes = m.group(1), int(m.group(2), 16)
        refs[rid] = payload[m.end():].encode("utf-8")[:nbytes].decode("utf-8", errors="ignore")
    for m in STR_REF_RE.finditer(payload):
        refs.setdefault(m.group(1), m.group(2))
    return refs


def json_objects(payload: str, anchor: str = '{"uuid":"') -> list[dict]:
    """Brace-match every JSON object in the stream that starts with `anchor`."""
    out, i = [], 0
    while True:
        i = payload.find(anchor, i)
        if i < 0:
            return out
        depth, in_str, esc = 0, False, False
        for j in range(i, len(payload)):
            c = payload[j]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            out.append(json.loads(payload[i:j + 1]))
                        except json.JSONDecodeError:
                            pass
                        break
        i += 1


class EthGlobalScraper(BaseScraper):
    name = "ethglobal"

    def __init__(self, client: HttpClient | None = None, fetch_details: bool = True, **kw):
        super().__init__(client, **kw)
        self.fetch_details = fetch_details

    # ---- hackathons ------------------------------------------------------
    def hackathons(self, limit: int | None = None) -> Iterator[dict]:
        """Events are discovered from the showcase itself (each project names its event)."""
        html = self.client.get_text(f"{BASE}/events")
        payload = flight_payload(html)
        seen: set[str] = set()
        count = 0
        for obj in json_objects(payload, '{"slug":"'):
            slug, name = obj.get("slug"), obj.get("name")
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)
            yield {
                "slug": slug,
                "name": name,
                "url": f"{BASE}/events/{slug}",
                "starts_at": obj.get("startTime", "") or "",
                "ends_at": obj.get("endTime", "") or "",
                "themes": [],
                "status": "",
                "raw": obj,
            }
            count += 1
            if limit and count >= limit:
                return

    # ---- projects --------------------------------------------------------
    def projects(
        self,
        hackathon: dict | str | None = None,
        winners_only: bool = True,
        limit: int | None = None,
    ) -> Iterator[Project]:
        event_slug = None
        if isinstance(hackathon, dict):
            event_slug = hackathon.get("slug")
        elif isinstance(hackathon, str):
            event_slug = hackathon

        seen_ids: set[str] = set()
        count, page = 0, 1
        while True:
            params: dict[str, Any] = {"page": page}
            if event_slug:
                params["events"] = event_slug
            html = self.client.get_text(f"{BASE}/showcase", params=params)
            payload = flight_payload(html)
            refs = ref_table(payload)
            cards = [o for o in json_objects(payload) if o.get("slug") and o.get("name")]
            fresh = [o for o in cards if o.get("uuid") not in seen_ids]
            if not fresh:
                return
            for obj in fresh:
                seen_ids.add(obj.get("uuid", ""))
                prizes = self._prizes(obj.get("prizes") or [])
                if winners_only and not prizes:
                    continue
                p = self._to_project(obj, refs, prizes)
                if self.fetch_details:
                    try:
                        self._enrich(p)
                    except Exception as e:
                        log.warning("ethglobal detail failed for %s: %s", p.url, e)
                yield p
                count += 1
                if limit and count >= limit:
                    return
            page += 1

    # ---- mapping ---------------------------------------------------------
    @staticmethod
    def _prizes(raw: list) -> list[Prize]:
        out = []
        for pr in raw:
            if not isinstance(pr, dict):
                continue
            inner = pr.get("prize") or {}
            sponsor = ((inner.get("sponsor") or {}).get("name")) or ""
            out.append(
                Prize(
                    name=inner.get("name", "") or pr.get("name", ""),
                    rank=pr.get("name", ""),           # "1st place", "Finalist"
                    sponsor=sponsor,
                    track=inner.get("type", ""),       # "tier" | "finalist" | "partner"
                )
            )
        return out

    def _to_project(self, obj: dict, refs: dict[str, str], prizes: list[Prize]) -> Project:
        uuid = obj.get("uuid", "")
        slug = obj.get("slug", "")
        event = obj.get("event") or {}
        meta = obj.get("meta") or {}
        return Project(
            source=self.name,
            source_id=uuid or slug,
            url=f"{BASE}/showcase/{slug}-{uuid}" if uuid else f"{BASE}/showcase/{slug}",
            title=obj.get("name", ""),
            tagline=obj.get("tagline", "") or "",
            description=self._deref(obj.get("description"), refs),
            problem_solved=meta.get("autoSummary", "") or "",
            challenges=self._deref(obj.get("howItsMade"), refs),
            is_winner=bool(prizes),
            prizes=prizes,
            hackathon_name=event.get("name", ""),
            hackathon_slug=event.get("slug", ""),
            hackathon_url=f"{BASE}/events/{event.get('slug')}" if event.get("slug") else "",
            hackathon_start=event.get("startTime", "") or "",
            demo_url=obj.get("url") or "",
            repo_url=(obj.get("primaryRepository") or {}).get("url") or obj.get("sourceCodeUrl") or "",
            video_url=((obj.get("video") or {}).get("muxUrl")) or "",
            raw={k: v for k, v in obj.items() if k not in {"banner", "logo", "screenshots", "video"}},
        )

    def _enrich(self, p: Project) -> None:
        # The showcase list page ships prize objects with their text fields
        # hoisted out by RSC de-duplication, so prize names only exist on the
        # detail page. Same for the event slug.
        needs_prizes = any(not (pr.name or pr.rank) for pr in p.prizes) or not p.prizes
        if p.description and p.challenges and p.hackathon_slug and not needs_prizes:
            return
        payload = flight_payload(self.client.get_text(p.url))
        refs = ref_table(payload)
        for obj in json_objects(payload):
            if obj.get("uuid") != p.source_id:
                continue
            p.description = p.description or self._deref(obj.get("description"), refs)
            p.challenges = p.challenges or self._deref(obj.get("howItsMade"), refs)
            p.repo_url = p.repo_url or (obj.get("primaryRepository") or {}).get("url", "")
            p.demo_url = p.demo_url or (obj.get("url") or "")
            event = obj.get("event") or {}
            p.hackathon_slug = p.hackathon_slug or event.get("slug", "")
            p.hackathon_name = p.hackathon_name or event.get("name", "")
            if p.hackathon_slug and not p.hackathon_url:
                p.hackathon_url = f"{BASE}/events/{p.hackathon_slug}"
            fresh = self._prizes(obj.get("prizes") or [])
            if any(pr.name or pr.rank for pr in fresh):
                p.prizes = fresh
            p.is_winner = bool([pr for pr in p.prizes if pr.name or pr.rank])
            p.tracks = sorted({pr.track for pr in p.prizes if pr.track})
            p.raw = {k: v for k, v in obj.items()
                     if k not in {"banner", "logo", "screenshots", "video"}}
            break
        # ETHGlobal has no structured tech field; approximate it from the
        # "how it's made" write-up. Ambiguous English words (Base, Go) are
        # deliberately excluded to keep this low-noise.
        p.tech_stack = sorted({
            t for t in re.findall(TECH_RE, f"{p.description}\n{p.challenges}")
        }, key=str.lower)

    @staticmethod
    def _deref(value, refs: dict[str, str]) -> str:
        if not isinstance(value, str):
            return ""
        if value.startswith("$") and len(value) <= 8:
            return refs.get(value[1:], "")
        return value
