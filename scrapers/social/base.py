"""Shared shape for social/forum sources.

A social source yields raw `SocialPost`s. Turning one into a `PainPoint` is the
job of `signals.to_painpoint`, so every source shares one pain-detection pass
instead of each reimplementing its own heuristics.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..base import BaseScraper

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """HN and Reddit both hand back HTML fragments; flatten to plain text."""
    if not text:
        return ""
    text = _TAG.sub(" ", text)
    return _WS.sub(" ", html.unescape(text)).strip()


@dataclass
class SocialPost:
    source: str = ""
    source_id: str = ""
    url: str = ""
    title: str = ""
    text: str = ""
    author: str = ""
    channel: str = ""        # subreddit, HN tag, app id
    posted_at: str = ""      # ISO 8601
    points: int = 0
    comments: int = 0
    lang: str = "en"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def body(self) -> str:
        """Title + text, which is what the pain heuristics read."""
        return f"{self.title}. {self.text}".strip(". ").strip()


class SocialScraper(BaseScraper):
    """Every social source implements `posts()`."""

    name = "social"
    has_project_submissions = False

    def posts(
        self,
        query: str = "",
        since_days: int = 365,
        limit: int | None = None,
        **kw,
    ) -> Iterator[SocialPost]:
        raise NotImplementedError
