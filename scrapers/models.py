"""Unified data model shared by every source scraper."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Prize:
    name: str = ""
    rank: str = ""          # "1st place", "Winner", "Finalist", ...
    sponsor: str = ""
    track: str = ""
    description: str = ""


@dataclass
class Project:
    """One hackathon submission, normalised across Devfolio / Devpost / ETHGlobal."""

    # provenance
    source: str = ""                 # devfolio | devpost | ethglobal
    source_id: str = ""              # uuid/slug on the source platform
    url: str = ""

    # the idea itself
    title: str = ""
    tagline: str = ""
    description: str = ""            # full write-up, markdown/plain
    problem_solved: str = ""         # "the problem it solves" style section
    challenges: str = ""             # "challenges we ran into"

    # classification
    themes: list[str] = field(default_factory=list)   # hackathon themes/tracks
    tracks: list[str] = field(default_factory=list)   # prize tracks the project entered
    tech_stack: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)

    # outcome
    is_winner: bool = False
    prizes: list[Prize] = field(default_factory=list)

    # context
    hackathon_name: str = ""
    hackathon_slug: str = ""
    hackathon_url: str = ""
    hackathon_start: str = ""
    hackathon_end: str = ""

    # people & artefacts
    team: list[str] = field(default_factory=list)
    demo_url: str = ""
    repo_url: str = ""
    video_url: str = ""

    # signals
    likes: int = 0
    views: int = 0
    submitted_at: str = ""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable dedupe key."""
        base = f"{self.source}:{self.source_id or self.url or self.title}"
        return hashlib.sha1(base.encode()).hexdigest()[:16]

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        if not include_raw:
            d.pop("raw", None)
        return d

    def to_row(self) -> dict[str, Any]:
        """Flat row for SQLite / CSV."""
        return {
            "key": self.key,
            "source": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "tagline": self.tagline,
            "description": self.description,
            "problem_solved": self.problem_solved,
            "challenges": self.challenges,
            "themes": "|".join(self.themes),
            "tracks": "|".join(self.tracks),
            "tech_stack": "|".join(self.tech_stack),
            "platforms": "|".join(self.platforms),
            "is_winner": int(self.is_winner),
            "prizes": json.dumps([asdict(p) for p in self.prizes], ensure_ascii=False),
            "prize_names": "|".join(filter(None, (p.rank or p.name for p in self.prizes))),
            "hackathon_name": self.hackathon_name,
            "hackathon_slug": self.hackathon_slug,
            "hackathon_url": self.hackathon_url,
            "hackathon_start": self.hackathon_start,
            "hackathon_end": self.hackathon_end,
            "team": "|".join(self.team),
            "demo_url": self.demo_url,
            "repo_url": self.repo_url,
            "video_url": self.video_url,
            "likes": self.likes,
            "views": self.views,
            "submitted_at": self.submitted_at,
            "raw": json.dumps(self.raw, ensure_ascii=False),
        }


ROW_COLUMNS = [
    "key", "source", "source_id", "url", "title", "tagline", "description",
    "problem_solved", "challenges", "themes", "tracks", "tech_stack", "platforms",
    "is_winner", "prizes", "prize_names", "hackathon_name", "hackathon_slug",
    "hackathon_url", "hackathon_start", "hackathon_end", "team", "demo_url",
    "repo_url", "video_url", "likes", "views", "submitted_at", "raw",
]


@dataclass
class PainPoint:
    """One problem statement mined from a social/forum post.

    Deliberately *not* a `Project`: a project is somebody's answer, a pain point
    is somebody's question. They live in separate tables and only meet in the
    matcher, where "how many projects already attack this pain" becomes the
    novelty signal.
    """

    # provenance
    source: str = ""                 # hn | reddit | ...
    source_id: str = ""
    url: str = ""
    channel: str = ""                # subreddit / HN tag / app id
    author: str = ""
    posted_at: str = ""
    lang: str = "en"

    # the problem itself
    problem_statement: str = ""      # one normalised sentence
    raw_text: str = ""               # the post/comment it came from
    title: str = ""
    who: str = ""                    # "restaurant owner", "indie dev", ...
    domain: str = ""                 # fintech | devtools | health | ...

    # signals
    signal_terms: list[str] = field(default_factory=list)  # which pain phrases fired
    points: int = 0
    comments: int = 0
    score: float = 0.0               # signal x engagement x recency
    frequency: int = 1               # cluster size, filled in by the clustering pass
    cluster_id: str = ""
    impact: float = 0.0              # score x cluster size x severity

    # LLM enrichment (empty until `run.py extract` has seen the row)
    severity: int = 0                # 1-5, 0 = not judged yet
    is_problem: bool = True          # the model's verdict: is this really a problem?
    extracted_by: str = ""           # model id that wrote the fields above

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        base = f"{self.source}:{self.source_id or self.url}"
        return hashlib.sha1(base.encode()).hexdigest()[:16]

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        if not include_raw:
            d.pop("raw", None)
        return d

    def to_row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "channel": self.channel,
            "author": self.author,
            "posted_at": self.posted_at,
            "lang": self.lang,
            "problem_statement": self.problem_statement,
            "raw_text": self.raw_text,
            "title": self.title,
            "who": self.who,
            "domain": self.domain,
            "signal_terms": "|".join(self.signal_terms),
            "points": self.points,
            "comments": self.comments,
            "score": self.score,
            "frequency": self.frequency,
            "cluster_id": self.cluster_id,
            "impact": self.impact,
            "severity": self.severity,
            "is_problem": int(self.is_problem),
            "extracted_by": self.extracted_by,
            "raw": json.dumps(self.raw, ensure_ascii=False),
        }


PROBLEM_COLUMNS = [
    "key", "source", "source_id", "url", "channel", "author", "posted_at", "lang",
    "problem_statement", "raw_text", "title", "who", "domain", "signal_terms",
    "points", "comments", "score", "frequency", "cluster_id", "impact",
    "severity", "is_problem", "extracted_by", "raw",
]

#: columns of `PROBLEM_COLUMNS` that are not TEXT, with their SQLite declaration
PROBLEM_NUMERIC = {
    "points": "INTEGER DEFAULT 0",
    "comments": "INTEGER DEFAULT 0",
    "score": "REAL DEFAULT 0",
    "frequency": "INTEGER DEFAULT 1",
    "impact": "REAL DEFAULT 0",
    "severity": "INTEGER DEFAULT 0",
    "is_problem": "INTEGER DEFAULT 1",
}
