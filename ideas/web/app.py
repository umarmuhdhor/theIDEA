#!/usr/bin/env python3
"""Local dashboard for browsing the scraped hackathon ideas.

    ./run.sh dashboard                         # http://localhost:5050

Reads the same SQLite file the scrapers write to, so it stays useful while a
crawl is still running — the counters update on refresh.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ..analysis import gap as gap_mod
from ..analysis.matcher import TopicMatcher
from ..store import Store

app = Flask(__name__)
DB_PATH = "data/hackathon_ideas.db"
_matcher: TopicMatcher | None = None


def matcher() -> TopicMatcher:
    global _matcher
    if _matcher is None or _matcher.db_path != DB_PATH:
        _matcher = TopicMatcher(DB_PATH)
    return _matcher

# Every column is qualified: the FTS query joins projects_fts, which also has a
# `key` column, so a bare name is ambiguous there.
LIST_COLS = ", ".join(
    f"p.{c}" for c in (
        "key", "source", "title", "tagline", "url", "hackathon_name", "hackathon_slug",
        "prize_names", "tech_stack", "themes", "tracks", "is_winner", "likes", "views",
        "submitted_at",
    )
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def split(row: sqlite3.Row, *fields) -> dict:
    d = dict(row)
    for f in fields:
        d[f] = [x for x in (d.get(f) or "").split("|") if x]
    return d


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    with connect() as c:
        problems = c.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
        unsolved_src = c.execute("SELECT COUNT(DISTINCT source) FROM problems").fetchone()[0]
        by_source = [dict(r) for r in c.execute(
            "SELECT source, COUNT(*) n, COALESCE(SUM(is_winner),0) winners, "
            "COUNT(DISTINCT hackathon_slug) hackathons FROM projects GROUP BY source ORDER BY n DESC"
        )]
        total = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        winners = c.execute("SELECT COUNT(*) FROM projects WHERE is_winner=1").fetchone()[0]
        hacks = c.execute("SELECT COUNT(DISTINCT hackathon_slug) FROM projects").fetchone()[0]
    return jsonify({"total": total, "winners": winners, "hackathons": hacks,
                    "problems": problems, "problem_sources": unsolved_src,
                    "by_source": by_source})


@app.route("/api/facets")
def api_facets():
    """Values for the filter dropdowns, plus the most common tech tags."""
    with connect() as c:
        hackathons = [dict(r) for r in c.execute(
            "SELECT hackathon_slug slug, MAX(hackathon_name) name, source, COUNT(*) n "
            "FROM projects WHERE hackathon_slug != '' GROUP BY hackathon_slug, source "
            "ORDER BY n DESC LIMIT 300"
        )]
        tech: dict[str, int] = {}
        for (blob,) in c.execute("SELECT tech_stack FROM projects WHERE tech_stack != ''"):
            for t in blob.split("|"):
                if t:
                    tech[t] = tech.get(t, 0) + 1
    top_tech = sorted(tech.items(), key=lambda kv: -kv[1])[:60]
    return jsonify({"hackathons": hackathons, "tech": [{"name": k, "n": v} for k, v in top_tech]})


@app.route("/api/projects")
def api_projects():
    q = (request.args.get("q") or "").strip()
    source = request.args.get("source") or ""
    hackathon = request.args.get("hackathon") or ""
    tech = request.args.get("tech") or ""
    winners_only = request.args.get("winners") == "1"
    sort = request.args.get("sort") or "likes"
    page = max(1, int(request.args.get("page") or 1))
    per = min(200, int(request.args.get("per") or 50))

    where, params = [], []
    if source:
        where.append("p.source = ?"); params.append(source)
    if hackathon:
        where.append("p.hackathon_slug = ?"); params.append(hackathon)
    if tech:
        where.append("('|' || p.tech_stack || '|') LIKE ?"); params.append(f"%|{tech}|%")
    if winners_only:
        where.append("p.is_winner = 1")

    order = {
        "likes": "p.likes DESC, p.views DESC",
        "views": "p.views DESC",
        "recent": "p.submitted_at DESC",
        "title": "p.title COLLATE NOCASE ASC",
    }.get(sort, "p.likes DESC")

    with connect() as c:
        if q:
            # FTS5 MATCH on the search table, then re-filter on the main table.
            base = (f"SELECT {LIST_COLS} FROM projects_fts f JOIN projects p ON p.key = f.key "
                    "WHERE projects_fts MATCH ?")
            args = [fts_query(q)] + params
            cond = (" AND " + " AND ".join(where)) if where else ""
            rows = c.execute(f"{base}{cond} ORDER BY rank LIMIT ? OFFSET ?",
                             args + [per, (page - 1) * per]).fetchall()
            total = c.execute(f"SELECT COUNT(*) FROM projects_fts f JOIN projects p ON p.key = f.key "
                              f"WHERE projects_fts MATCH ?{cond}", args).fetchone()[0]
        else:
            cond = (" WHERE " + " AND ".join(where)) if where else ""
            rows = c.execute(f"SELECT {LIST_COLS} FROM projects p{cond} "
                             f"ORDER BY {order} LIMIT ? OFFSET ?",
                             params + [per, (page - 1) * per]).fetchall()
            total = c.execute(f"SELECT COUNT(*) FROM projects p{cond}", params).fetchone()[0]

    items = [split(r, "prize_names", "tech_stack", "themes", "tracks") for r in rows]
    return jsonify({"total": total, "page": page, "per": per, "items": items})


@app.route("/api/overview")
def api_overview():
    """Aggregates for the overview panel."""
    with connect() as c:
        sources = [dict(r) for r in c.execute(
            "SELECT source, COUNT(*) n, COALESCE(SUM(is_winner),0) winners "
            "FROM projects GROUP BY source ORDER BY n DESC")]
        hackathons = [dict(r) for r in c.execute(
            "SELECT MAX(hackathon_name) name, hackathon_slug slug, source, COUNT(*) n, "
            "COALESCE(SUM(is_winner),0) winners FROM projects WHERE hackathon_slug != '' "
            "GROUP BY hackathon_slug ORDER BY n DESC LIMIT 12")]
        recent = [dict(r) for r in c.execute(
            f"SELECT {LIST_COLS} FROM projects p WHERE p.submitted_at != '' "
            "ORDER BY p.submitted_at DESC LIMIT 8")]
        tech: dict[str, int] = {}
        for (blob,) in c.execute("SELECT tech_stack FROM projects WHERE tech_stack != ''"):
            for t in blob.split("|"):
                if t:
                    tech[t] = tech.get(t, 0) + 1
        prize: dict[str, int] = {}
        for (blob,) in c.execute("SELECT prize_names FROM projects WHERE prize_names != ''"):
            for t in blob.split("|"):
                if t:
                    prize[t] = prize.get(t, 0) + 1
    top = lambda d, k: [{"name": a, "n": b} for a, b in sorted(d.items(), key=lambda kv: -kv[1])[:k]]
    return jsonify({
        "sources": sources, "hackathons": hackathons,
        "tech": top(tech, 15), "prizes": top(prize, 10),
        "recent": [split(r, "prize_names", "tech_stack", "themes", "tracks") for r in recent],
    })


@app.route("/api/match")
def api_match():
    """Rank ideas against a free-text topic (BM25, see matcher.py)."""
    topic = (request.args.get("topic") or "").strip()
    if not topic:
        return jsonify({"topic": "", "items": [], "related": [], "total": 0})

    winners_only = request.args.get("winners") == "1"
    source = request.args.get("source") or ""
    limit = min(100, int(request.args.get("limit") or 40))

    scope: set[str] | None = None
    if winners_only or source:
        where, params = [], []
        if winners_only:
            where.append("is_winner = 1")
        if source:
            where.append("source = ?"); params.append(source)
        with connect() as c:
            scope = {r[0] for r in c.execute(
                f"SELECT key FROM projects WHERE {' AND '.join(where)}", params)}

    m = matcher()
    hits = m.match(topic, limit=limit, keys=scope)
    related = m.related_terms(topic, hits)
    if not hits:
        return jsonify({"topic": topic, "items": [], "related": [], "total": 0})

    order = {h.key: i for i, h in enumerate(hits)}
    marks = {h.key: h for h in hits}
    qs = ",".join("?" * len(hits))
    with connect() as c:
        rows = c.execute(f"SELECT {LIST_COLS} FROM projects p WHERE p.key IN ({qs})",
                         list(order)).fetchall()
    items = []
    top_score = hits[0].score or 1.0
    for r in sorted(rows, key=lambda r: order[r["key"]]):
        d = split(r, "prize_names", "tech_stack", "themes", "tracks")
        h = marks[r["key"]]
        d["score"] = round(h.score, 2)
        d["relevance"] = round(100 * h.score / top_score)
        d["matched"] = h.matched
        d["matched_terms"] = h.matched_terms
        items.append(d)
    return jsonify({"topic": topic, "items": items, "related": related, "total": len(items)})


@app.route("/api/problems")
def api_problems():
    """Mined pain points, ranked by impact. One row per cluster by default."""
    q = (request.args.get("q") or "").strip()
    store = Store(DB_PATH, readonly=True)
    try:
        if q:
            rows = [dict(r) for r in store.search_problems(fts_query(q), limit=100)]
        else:
            rows = [dict(r) for r in store.top_problems(
                limit=min(200, int(request.args.get("limit") or 60)),
                domain=request.args.get("domain") or "",
                source=request.args.get("source") or "",
                include_rejected=request.args.get("rejected") == "1",
                collapse=request.args.get("expand") != "1",
            )]
        domains = [dict(r) for r in store.conn.execute(
            "SELECT domain, COUNT(*) n FROM problems WHERE domain != '' "
            "GROUP BY domain ORDER BY n DESC")]
        sources = [dict(r) for r in store.conn.execute(
            "SELECT source, COUNT(*) n FROM problems GROUP BY source ORDER BY n DESC")]
    finally:
        store.close()
    for r in rows:
        r["signal_terms"] = [x for x in (r.get("signal_terms") or "").split("|") if x]
    return jsonify({"items": rows, "total": len(rows),
                    "domains": domains, "sources": sources})


@app.route("/api/gap")
def api_gap():
    """The join: pain points ranked by how little of the corpus answers them."""
    store = Store(DB_PATH, readonly=True)
    try:
        rows = gap_mod.analyze(
            store, matcher(),
            top=min(60, int(request.args.get("top") or 20)),
            min_coverage=float(request.args.get("coverage") or gap_mod.DEFAULT_COVERAGE),
            domain=request.args.get("domain") or "",
            source=request.args.get("source") or "",
            unsolved_only=request.args.get("unsolved") == "1",
        )
        gap_mod.hydrate(store.conn, rows)
    finally:
        store.close()
    return jsonify({"items": rows, "total": len(rows)})


@app.route("/api/project/<key>")
def api_project(key: str):
    with connect() as c:
        row = c.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    d = split(row, "prize_names", "tech_stack", "themes", "tracks", "platforms", "team")
    d.pop("raw", None)
    d["prizes"] = json.loads(d.get("prizes") or "[]")
    return jsonify(d)


def fts_query(q: str) -> str:
    """Turn a plain phrase into a safe FTS5 AND-query."""
    terms = [t for t in "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in q).split() if t]
    return " AND ".join(f'"{t}"*' for t in terms) or '""'


def main() -> None:
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/hackathon_ideas.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    DB_PATH = args.db
    if not Path(DB_PATH).exists():
        raise SystemExit(f"no database at {DB_PATH} — run a scrape first")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
