"""Join the two halves: which mined problems has nobody built for yet.

`problems` holds what people complain about. `projects` holds ~8,000 hackathon
submissions — what people already built. Neither is interesting alone. The
answer to "what should we build this weekend" is the pain point with high
impact and *no* project attacking it.

Matching a problem to projects is `matcher.py`'s BM25 index, but a raw BM25
score cannot answer "is this project about this problem": scores are only
comparable within one query, so a fixed cut-off means nothing and "top hit"
always returns something, which would make every problem look solved.

So the test here is **coverage**: how much of the problem statement's
distinctive vocabulary (idf-weighted, so "restaurant" counts and "system" does
not) actually appears in the project. A project covering 60% of what makes the
problem specific is plausibly attacking it; one covering 15% shares a word.
That is comparable across queries and it is explainable — every count comes
with the terms that produced it.
"""
from __future__ import annotations

import logging
from typing import Any

from matcher import Match, TopicMatcher, tokenize

log = logging.getLogger(__name__)

#: Share of the problem's idf-weighted vocabulary a project must cover before it
#: counts as attacking that problem. Calibrated against this corpus (~8k
#: projects) on hand-written statements: at 5 terms / 0.5 a farming-irrigation
#: problem finds 12 projects and a game-dev-tooling one 38, while a rambling
#: forum post and a deliberately nonsense sentence both find 0. Raising it to
#: 0.6 loses the farming match; dropping to 0.4 starts admitting one-word
#: coincidences.
DEFAULT_COVERAGE = 0.5

#: How many BM25 hits to examine per problem before applying the coverage test.
CANDIDATES = 200

#: Coverage is measured over the N most distinctive terms, not the whole
#: statement. A lifted sentence can run 40 words, and demanding 60% of *that*
#: is unmeetable - every problem would look like white space, which is the one
#: failure mode this tool must not have.
TOP_TERMS = 5


def query_weights(m: TopicMatcher, text: str, top_terms: int = TOP_TERMS) -> dict[str, float]:
    """The most distinctive terms of a problem statement, idf-weighted.

    Terms the corpus has never seen are dropped: they cannot be covered by any
    project, so keeping them would make every problem look unsolved. What
    remains is capped at the `top_terms` rarest, which is what actually
    identifies the problem.
    """
    scored = []
    for t in set(tokenize(text)):
        idf = m.idf(t)
        if idf > 0:
            scored.append((idf, t))
    scored.sort(reverse=True)
    return {t: idf for idf, t in scored[:top_terms]}


def coverage(hit: Match, weights: dict[str, float]) -> float:
    total = sum(weights.values())
    if not total:
        return 0.0
    got = sum(weights.get(t, 0.0) for t in hit.matched_terms)
    return got / total


def analyze(
    store,
    matcher: TopicMatcher,
    top: int = 20,
    min_coverage: float = DEFAULT_COVERAGE,
    domain: str = "",
    source: str = "",
    min_impact: float = 0.0,
    unsolved_only: bool = False,
    per_problem: int = 5,
) -> list[dict[str, Any]]:
    """Rank pain points by how little of the hackathon corpus already answers them."""
    problems = store.top_problems(limit=max(top * 3, top), domain=domain, source=source)
    matcher.ensure_fresh()

    out: list[dict[str, Any]] = []
    for p in problems:
        if (p["impact"] or 0) < min_impact:
            continue
        statement = p["problem_statement"] or p["title"] or ""
        weights = query_weights(matcher, statement)
        if not weights:
            continue

        hits = matcher.match(statement, limit=CANDIDATES)
        attacking = []
        for h in hits:
            cov = coverage(h, weights)
            if cov >= min_coverage:
                attacking.append((cov, h))
        attacking.sort(key=lambda ch: -ch[0])

        solved = len(attacking)
        if unsolved_only and solved:
            continue

        # Every project that already attacks the problem halves the opportunity;
        # the first one hurts most, which is what the reciprocal does.
        gap_score = round((p["impact"] or 0) / (1 + solved), 2)

        out.append({
            "key": p["key"],
            "problem_statement": statement,
            "who": p["who"],
            "domain": p["domain"],
            "source": p["source"],
            "url": p["url"],
            "impact": p["impact"],
            "frequency": p["frequency"],
            "severity": p["severity"],
            "solved_by": solved,
            "gap_score": gap_score,
            "projects": [
                {"key": h.key, "coverage": round(cov, 2),
                 "matched_terms": h.matched_terms}
                for cov, h in attacking[:per_problem]
            ],
        })

    out.sort(key=lambda d: -d["gap_score"])
    return out[:top]


def hydrate(conn, rows: list[dict]) -> list[dict]:
    """Fill in project titles/urls for the matches, in one query."""
    keys = [pr["key"] for r in rows for pr in r["projects"]]
    if not keys:
        return rows
    qs = ",".join("?" * len(keys))
    got = {
        r["key"]: dict(r)
        for r in conn.execute(
            f"SELECT key, title, tagline, url, hackathon_name, is_winner, prize_names "
            f"FROM projects WHERE key IN ({qs})", keys
        )
    }
    for r in rows:
        for pr in r["projects"]:
            pr.update(got.get(pr["key"], {}))
    return rows
