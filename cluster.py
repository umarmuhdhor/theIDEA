"""Group pain points that are the same complaint worded differently.

Frequency is the single most useful signal a problem carries — "forty people
said this" outranks "one person said this" no matter how loud the one person
was — and it does not exist until near-duplicates are merged.

Lexical, like `matcher.py`, and for the same reason: no embedding model, no API
call, no download, and the result is deterministic and explainable. The cost is
the usual one — "agritech" and "farming" will not merge unless the corpus uses
both. Related terms in `matcher.py` exist to bridge that gap; here, the answer
is more sources rather than fancier maths.

The pairwise comparison is kept tractable with an inverted index: two statements
are only compared if they share a term that is not near-ubiquitous, so the work
is proportional to real overlap rather than to n squared.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict

from scrapers.social.signals import impact

log = logging.getLogger(__name__)

TOKEN = re.compile(r"[a-z][a-z0-9'+-]{2,}")

STOP = {
    "the", "and", "for", "that", "this", "with", "you", "your", "not", "but",
    "are", "was", "were", "have", "has", "had", "can", "cant", "get", "got",
    "would", "could", "should", "just", "like", "there", "their", "them", "they",
    "what", "when", "where", "which", "who", "why", "how", "all", "any", "some",
    "one", "our", "out", "own", "too", "very", "really", "actually", "someone",
    "something", "anything", "everything", "than", "then", "into", "from", "about",
    "way", "ways", "want", "need", "make", "made", "use", "used", "using", "does",
    "doing", "done", "still", "even", "much", "more", "most", "its", "his", "her",
}

#: A term in more than this share of the corpus carries no signal, and pairing
#: every row that contains "problem" would put the whole table in one bucket.
#: The floor matters: at n=5 a share alone would drop any term appearing twice,
#: which is exactly the term that identifies a duplicate.
MAX_DF_SHARE = 0.25
MIN_DF_CAP = 20

#: Tuned on one-sentence statements, where a genuine duplicate scores ~0.35-0.40
#: and an unrelated pair ~0.10. Merging is transitive (union-find), so a chain of
#: near-misses can still join two ends that are not alike - raise the threshold
#: if clusters start looking like topics rather than duplicates.
DEFAULT_THRESHOLD = 0.32


#: Suffix stripping, longest first. Not Porter - just enough that "schedule",
#: "schedules" and "scheduling" become one token, which is the difference
#: between merging a duplicate complaint and missing it. The 4-character floor
#: keeps "owner" from collapsing to "own".
SUFFIXES = ("ings", "ing", "edly", "ed", "ers", "er", "ies", "es", "ly", "s")


def stem(t: str) -> str:
    for suf in SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            t = t[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    # "schedule" -> "schedul" so it meets "scheduling" -> "schedul"
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]
    return t


def tokens(text: str) -> set[str]:
    out = set()
    for t in TOKEN.findall((text or "").lower()):
        if t in STOP:
            continue
        t = stem(t)
        if t not in STOP and len(t) >= 3:
            out.add(t)
    return out


class _Union:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build(rows: list, threshold: float = DEFAULT_THRESHOLD) -> tuple[list[tuple], dict]:
    """Return (`[(cluster_id, frequency, impact, key), ...]`, stats)."""
    docs = {}
    for r in rows:
        text = f"{r['problem_statement']} {r['title'] or ''}"
        tk = tokens(text)
        if tk:
            docs[r["key"]] = tk

    n = len(docs)
    if n < 2:
        return [], {"rows": n, "clusters": n, "merged": 0, "largest": n}

    df: dict[str, int] = defaultdict(int)
    for tk in docs.values():
        for t in tk:
            df[t] += 1

    # tf-idf weights; tf is binary because these are one-sentence documents
    weights = {
        key: {t: math.log(n / df[t]) for t in tk}
        for key, tk in docs.items()
    }
    norms = {k: math.sqrt(sum(w * w for w in v.values())) or 1.0 for k, v in weights.items()}

    index: dict[str, list[str]] = defaultdict(list)
    df_cap = max(MIN_DF_CAP, int(n * MAX_DF_SHARE))
    for key, tk in docs.items():
        for t in tk:
            if df[t] <= df_cap:
                index[t].append(key)

    candidates: set[tuple[str, str]] = set()
    for keys in index.values():
        if len(keys) < 2:
            continue
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                candidates.add((a, b) if a < b else (b, a))

    uf = _Union()
    for key in docs:
        uf.find(key)
    merged = 0
    for a, b in candidates:
        wa, wb = weights[a], weights[b]
        if len(wb) < len(wa):
            wa, wb = wb, wa
        dot = sum(w * wb.get(t, 0.0) for t, w in wa.items())
        if dot / (norms[a] * norms[b]) >= threshold:
            if uf.find(a) != uf.find(b):
                merged += 1
            uf.union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for key in docs:
        groups[uf.find(key)].append(key)

    by_key = {r["key"]: r for r in rows}
    updates = []
    for members in groups.values():
        # Deterministic id: the same membership always yields the same id, so a
        # re-run does not churn cluster ids for unchanged clusters.
        cid = hashlib.sha1("|".join(sorted(members)).encode()).hexdigest()[:12]
        freq = len(members)
        for key in members:
            r = by_key[key]
            updates.append((cid, freq, impact(r["score"], freq, r["severity"] or 0), key))

    # Rows with no usable tokens keep a cluster of one.
    for r in rows:
        if r["key"] not in docs:
            updates.append(("", 1, impact(r["score"], 1, r["severity"] or 0), r["key"]))

    stats = {
        "rows": len(rows),
        "clusters": len(groups) + (len(rows) - n),
        "merged": merged,
        "largest": max((len(m) for m in groups.values()), default=1),
    }
    return updates, stats


def run(store, threshold: float = DEFAULT_THRESHOLD) -> dict:
    rows = store.cluster_input()
    updates, stats = build(rows, threshold)
    if updates:
        store.apply_clusters(updates)
    return stats
