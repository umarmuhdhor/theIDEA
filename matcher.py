"""Topic matching over the scraped ideas.

The dashboard's plain search is SQLite FTS5: it needs every term to appear.
That is the wrong tool for "show me ideas about sustainability for farmers" —
a project can be squarely on-topic while using none of those exact words
together.

This module builds a small BM25 index instead:

* OR semantics with proper ranking, so partial topic overlap still surfaces
* field weighting, because a term in the title means more than one buried in
  paragraph nine of the write-up
* per-result evidence: which query terms actually hit, and where
* related-term suggestions mined from the top results, so a vague topic can be
  refined into a sharper one

It is lexical, not semantic — there are no embeddings here. Synonyms it has
never seen ("agritech" vs "farming") will not match on their own, which is
exactly why the related-term chips exist.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

K1 = 1.5   # BM25 term-frequency saturation
B = 0.75   # BM25 length normalisation

# Field weights: a hit in the title is worth more than one in the write-up.
FIELD_WEIGHTS = {
    "title": 4,
    "tagline": 3,
    "problem_solved": 2,
    "tech_stack": 2,
    "themes": 2,
    "tracks": 1,
    "description": 1,
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
TRIM_RE = re.compile(r"[.\-]+$")   # "backend." -> "backend"

STOP = set("""
a an and are as at be been but by can could do does for from had has have how i if in into is it
its of on or our so such than that the their then there these they this to was we were what when
where which while who will with would you your using use used build built building make makes made
project projects app apps platform solution based new users user get help
""".split())


def tokenize(text: str) -> list[str]:
    out = []
    for raw in TOKEN_RE.findall((text or "").lower()):
        t = TRIM_RE.sub("", raw)
        if len(t) > 1 and t not in STOP:
            out.append(t)
    return out


@dataclass
class Match:
    key: str
    score: float
    matched: dict[str, list[str]]   # field -> query terms that hit there

    @property
    def matched_terms(self) -> list[str]:
        return sorted({t for ts in self.matched.values() for t in ts})


class TopicMatcher:
    """In-memory BM25 index, rebuilt when the database grows under it."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._row_count = -1
        self._built_at = 0.0
        self.min_rebuild_interval = 30.0
        self.df: Counter[str] = Counter()
        self.docs: dict[str, Counter[str]] = {}
        self.doc_fields: dict[str, dict[str, set[str]]] = {}
        self.doc_len: dict[str, float] = {}
        self.postings: dict[str, set[str]] = defaultdict(set)
        self.avgdl = 1.0
        self.n = 0

    # ---- index -----------------------------------------------------------
    def current_rows(self) -> int:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as c:
            return c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

    def ensure_fresh(self) -> None:
        """Rebuild if rows were added — a crawl may still be filling the table.

        Rate-limited: during a crawl the row count changes constantly, and
        rebuilding the whole index on every request would make each query pay
        for the full corpus scan.
        """
        if self._row_count < 0:
            self.build()
            self._row_count = self.current_rows()
            self._built_at = time.monotonic()
            return
        if time.monotonic() - self._built_at < self.min_rebuild_interval:
            return
        rows = self.current_rows()
        if rows != self._row_count:
            self.build()
            self._row_count = rows
        self._built_at = time.monotonic()

    def build(self) -> None:
        self.df.clear(); self.docs.clear(); self.doc_fields.clear()
        self.doc_len.clear(); self.postings.clear()

        cols = ["key"] + list(FIELD_WEIGHTS)
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(f"SELECT {','.join(cols)} FROM projects").fetchall()

        for r in rows:
            key = r["key"]
            tf: Counter[str] = Counter()
            fields: dict[str, set[str]] = {}
            for field, weight in FIELD_WEIGHTS.items():
                toks = tokenize((r[field] or "").replace("|", " "))
                if not toks:
                    continue
                fields[field] = set(toks)
                for t in toks:
                    tf[t] += weight
            if not tf:
                continue
            self.docs[key] = tf
            self.doc_fields[key] = fields
            self.doc_len[key] = sum(tf.values())
            for t in tf:
                self.df[t] += 1
                self.postings[t].add(key)

        self.n = len(self.docs)
        self.avgdl = (sum(self.doc_len.values()) / self.n) if self.n else 1.0

    # ---- query -----------------------------------------------------------
    def idf(self, term: str) -> float:
        n_q = self.df.get(term, 0)
        if not n_q:
            return 0.0
        return math.log(1 + (self.n - n_q + 0.5) / (n_q + 0.5))

    def match(self, topic: str, limit: int = 40, keys: set[str] | None = None) -> list[Match]:
        self.ensure_fresh()
        terms = tokenize(topic)
        if not terms or not self.n:
            return []

        # Only score documents that contain at least one query term.
        candidates: set[str] = set()
        for t in set(terms):
            candidates |= self.postings.get(t, set())
        if keys is not None:
            candidates &= keys

        out: list[Match] = []
        for key in candidates:
            tf = self.docs[key]
            dl = self.doc_len[key]
            score = 0.0
            for t in set(terms):
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = self.idf(t)
                score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / self.avgdl))
            if score <= 0:
                continue
            hit_fields: dict[str, list[str]] = {}
            for field, toks in self.doc_fields[key].items():
                hits = sorted(set(terms) & toks)
                if hits:
                    hit_fields[field] = hits
            out.append(Match(key=key, score=score, matched=hit_fields))

        out.sort(key=lambda m: -m.score)
        return out[:limit]

    def related_terms(self, topic: str, matches: list[Match], limit: int = 14) -> list[str]:
        """Distinctive terms shared by the top hits — chips to sharpen a vague topic."""
        query = set(tokenize(topic))
        if not matches:
            return []
        top = matches[:25]
        counts: Counter[str] = Counter()
        for m in top:
            for t in self.docs[m.key]:
                if t not in query:
                    counts[t] += 1

        # Rank by lift, not raw frequency: how much more often a term shows up
        # in these results than in the corpus at large. Counting alone just
        # resurfaces common English that slipped past the stop list.
        min_hits = max(2, len(top) // 8)
        scored = []
        for t, c in counts.items():
            df = self.df.get(t, 0)
            if c < min_hits or df < 3:
                continue
            lift = (c / len(top)) / (df / self.n)
            if lift < 3:
                continue
            scored.append((t, lift * math.log1p(c)))
        scored.sort(key=lambda kv: -kv[1])
        return [t for t, _ in scored[:limit]]
