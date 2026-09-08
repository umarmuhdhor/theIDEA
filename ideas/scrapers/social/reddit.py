"""Reddit via the official OAuth API.

Anonymous access is dead: every `.json` endpoint (www, api, old) now answers 403
or redirects to a login wall, whatever User-Agent you send. So this needs a free
"script" app — https://www.reddit.com/prefs/apps — and reads its credentials
from the environment:

    export REDDIT_CLIENT_ID=...
    export REDDIT_CLIENT_SECRET=...
    export REDDIT_USER_AGENT="python:idea-scraping:v0.1 (by /u/yourname)"   # optional

No account password is involved: the `client_credentials` grant returns an
app-only token that can read everything public, which is all this needs.

Why Reddit matters here: Hacker News is developers complaining about developer
problems. Reddit is where a restaurant owner, a school administrator and a
freight dispatcher complain about theirs — and pain outside your own field is
the pain nobody at the hackathon is already building for.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .base import SocialPost, SocialScraper
from .signals import SERVER_SIDE_PHRASES

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"

#: Non-developer seams. HN already covers the developer ones.
DEFAULT_SUBREDDITS = (
    "smallbusiness", "Entrepreneur", "freelance", "nonprofit",
    "restaurateur", "sysadmin", "Teachers", "Accounting",
)

#: Reddit's search understands quoted phrases, so the pain filter can run
#: server-side instead of downloading a firehose and throwing 99% of it away.
PAIN_QUERIES = tuple(f'"{p}"' for p in SERVER_SIDE_PHRASES)

PAGE = 100
CREDS_HELP = (
    "Reddit needs a free script app: https://www.reddit.com/prefs/apps -> "
    "'create another app' -> type 'script' -> then export REDDIT_CLIENT_ID and "
    "REDDIT_CLIENT_SECRET. Anonymous Reddit JSON is 403 everywhere now."
)


def _window(since_days: int) -> str:
    """Reddit search only takes coarse buckets, so round up to the nearest one."""
    for days, name in ((1, "day"), (7, "week"), (31, "month"), (365, "year")):
        if since_days <= days:
            return name
    return "all"


class RedditScraper(SocialScraper):
    name = "reddit"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._token = ""
        self._token_expires = 0.0

    # ---- auth ------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        if self._token and time.time() < self._token_expires - 60:
            return {"Authorization": f"bearer {self._token}"}

        cid = os.environ.get("REDDIT_CLIENT_ID", "")
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        if not cid or not secret:
            raise RuntimeError(f"REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set. {CREDS_HELP}")
        ua = os.environ.get("REDDIT_USER_AGENT", "python:idea-scraping:v0.1 (by /u/anon)")

        # Deliberately not routed through the response cache: a token is not a
        # document, and it must never touch data/.cache/.
        r = self.client._request(
            "POST", TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"User-Agent": ua},
        )
        if r.status_code == 401:
            raise RuntimeError(f"Reddit rejected those credentials (401). {CREDS_HELP}")
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._token_expires = time.time() + int(tok.get("expires_in", 3600))
        log.info("[reddit] authenticated, token good for %ss", tok.get("expires_in"))
        return {"Authorization": f"bearer {self._token}"}

    def _headers(self) -> dict[str, str]:
        h = self._auth_header()
        h["User-Agent"] = os.environ.get(
            "REDDIT_USER_AGENT", "python:idea-scraping:v0.1 (by /u/anon)"
        )
        return h

    # ---- listing ---------------------------------------------------------
    def posts(
        self,
        query: str = "",
        since_days: int = 365,
        limit: int | None = None,
        channels: list[str] | tuple[str, ...] | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        subs = tuple(channels or DEFAULT_SUBREDDITS)
        queries = (query,) if query else PAIN_QUERIES
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
        window = _window(since_days)

        self._auth_header()          # fail fast on bad creds, before any sweeping
        seen: set[str] = set()
        streams = [
            self._search(sub, q, window, cutoff, seen)
            for sub in subs for q in queries
        ]
        n = 0
        # Round-robin so one busy subreddit cannot eat the whole budget.
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

    def _search(
        self, sub: str, query: str, window: str, cutoff: float, seen: set[str]
    ) -> Iterator[SocialPost]:
        after, n = "", 0
        while True:
            params = {
                "q": query,
                "restrict_sr": 1,
                "sort": "new",
                "t": window,
                "limit": PAGE,
                "raw_json": 1,
            }
            if after:
                params["after"] = after
            try:
                data = self.client.get_json(
                    f"{API}/r/{sub}/search", params=params, headers=self._headers()
                )
            except Exception as e:                       # private sub, 404, ban
                log.warning("[reddit] r/%s %s failed: %s", sub, query, e)
                return
            children = (data.get("data") or {}).get("children") or []
            if not children:
                return
            for c in children:
                d = c.get("data") or {}
                oid = d.get("id") or ""
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                # `sort=new` means once we are past the cutoff every later page
                # is older still.
                if float(d.get("created_utc") or 0) < cutoff:
                    return
                n += 1
                yield self._post(d, sub)
            log.info("[reddit] r/%s %s: %d posts", sub, query, n)
            after = (data.get("data") or {}).get("after") or ""
            if not after:
                return

    # ------------------------------------------------------------------
    def _post(self, d: dict, sub: str) -> SocialPost:
        created = float(d.get("created_utc") or 0)
        return SocialPost(
            source="reddit",
            source_id=str(d.get("id")),
            url="https://www.reddit.com" + (d.get("permalink") or ""),
            title=d.get("title") or "",
            text=d.get("selftext") or "",
            author=d.get("author") or "",
            channel=f"r/{d.get('subreddit') or sub}",
            posted_at=datetime.fromtimestamp(created, timezone.utc).isoformat()
            if created else "",
            points=int(d.get("score") or 0),
            comments=int(d.get("num_comments") or 0),
            raw={k: d.get(k) for k in (
                "id", "title", "selftext", "subreddit", "score", "num_comments",
                "created_utc", "permalink", "author", "link_flair_text", "over_18",
            )},
        )
