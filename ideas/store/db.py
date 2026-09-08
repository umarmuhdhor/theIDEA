"""SQLite storage plus CSV / JSONL export, with full-text search over ideas."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from ..scrapers.models import (
    PROBLEM_COLUMNS, PROBLEM_NUMERIC, ROW_COLUMNS, PainPoint, Project,
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projects (
    {', '.join(f'{c} TEXT' for c in ROW_COLUMNS if c not in ('is_winner', 'likes', 'views'))},
    is_winner INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (key)
);
CREATE INDEX IF NOT EXISTS idx_source   ON projects(source);
CREATE INDEX IF NOT EXISTS idx_winner   ON projects(is_winner);
CREATE INDEX IF NOT EXISTS idx_hack     ON projects(hackathon_slug);

-- A regular (content-carrying) FTS5 table: a contentless one cannot return
-- the key column needed to join back to `projects`.
CREATE VIRTUAL TABLE IF NOT EXISTS projects_fts USING fts5(
    key UNINDEXED, title, tagline, description, problem_solved, tech_stack, themes
);

-- Pain points live beside projects, not inside them: a project is an answer,
-- a pain point is a question. They meet only in the matcher.
CREATE TABLE IF NOT EXISTS problems (
    {', '.join(f'{c} TEXT' for c in PROBLEM_COLUMNS if c not in PROBLEM_NUMERIC)},
    {', '.join(f'{c} {t}' for c, t in PROBLEM_NUMERIC.items())},
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (key)
);
CREATE INDEX IF NOT EXISTS idx_prob_source ON problems(source);
CREATE INDEX IF NOT EXISTS idx_prob_score  ON problems(score);
CREATE INDEX IF NOT EXISTS idx_prob_domain ON problems(domain);

CREATE VIRTUAL TABLE IF NOT EXISTS problems_fts USING fts5(
    key UNINDEXED, problem_statement, raw_text, title, who, domain, channel
);
"""


class Store:
    def __init__(self, path: str | Path = "data/hackathon_ideas.db", readonly: bool = False):
        self.path = Path(path)
        if readonly:
            # The dashboard reads while a crawl may be writing; opening ro also
            # means no accidental schema work from a viewer process.
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that a database created by an older version is missing.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so new
        columns (the LLM-extraction and clustering fields) would otherwise only
        appear in freshly created databases.
        """
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(problems)")}
        for col in PROBLEM_COLUMNS:
            if col not in have:
                decl = PROBLEM_NUMERIC.get(col, "TEXT")
                self.conn.execute(f"ALTER TABLE problems ADD COLUMN {col} {decl}")
        # Idempotent repair: a row that has never been ranked falls back to its
        # raw score. `is_problem = 1` protects the zeroes the extractor writes
        # on purpose when it rejects a row.
        self.conn.execute(
            "UPDATE problems SET impact = score "
            "WHERE (impact IS NULL OR impact = 0) AND COALESCE(is_problem,1) = 1"
        )

    # ---- write -----------------------------------------------------------
    def upsert(self, projects: Iterable[Project]) -> int:
        rows = [p.to_row() for p in projects]
        if not rows:
            return 0
        cols = ROW_COLUMNS
        sql = (
            f"INSERT INTO projects ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(key) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "key")
        )
        self.conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        self.conn.executemany(
            "DELETE FROM projects_fts WHERE key = ?", [(r["key"],) for r in rows]
        )
        self.conn.executemany(
            "INSERT INTO projects_fts (key,title,tagline,description,problem_solved,tech_stack,themes) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (r["key"], r["title"], r["tagline"], r["description"],
                 r["problem_solved"], r["tech_stack"], r["themes"])
                for r in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    # ---- read ------------------------------------------------------------
    def count(self, where: str = "", args: tuple = ()) -> int:
        q = "SELECT COUNT(*) FROM projects" + (f" WHERE {where}" if where else "")
        return self.conn.execute(q, args).fetchone()[0]

    def stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, COUNT(*) n, SUM(is_winner) winners, "
            "COUNT(DISTINCT hackathon_slug) hackathons FROM projects GROUP BY source"
        ).fetchall()

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.title, p.tagline, p.url, p.hackathon_name, p.prize_names, p.source "
            "FROM projects_fts f JOIN projects p ON p.key = f.key "
            "WHERE projects_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()

    # ---- problems --------------------------------------------------------
    def upsert_problems(self, problems: Iterable[PainPoint]) -> int:
        rows = [p.to_row() for p in problems]
        if not rows:
            return 0
        cols = PROBLEM_COLUMNS
        sql = (
            f"INSERT INTO problems ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(key) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "key")
        )
        self.conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        self.conn.executemany(
            "DELETE FROM problems_fts WHERE key = ?", [(r["key"],) for r in rows]
        )
        self.conn.executemany(
            "INSERT INTO problems_fts (key,problem_statement,raw_text,title,who,domain,channel) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (r["key"], r["problem_statement"], r["raw_text"], r["title"],
                 r["who"], r["domain"], r["channel"])
                for r in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def top_problems(
        self,
        limit: int = 20,
        domain: str = "",
        source: str = "",
        min_score: float = 0.0,
        include_rejected: bool = False,
        collapse: bool = True,
    ) -> list[sqlite3.Row]:
        where, args = ["score >= ?"], [min_score]
        if not include_rejected:
            where.append("is_problem = 1")
        if domain:
            where.append("domain = ?")
            args.append(domain)
        if source:
            where.append("source = ?")
            args.append(source)
        args.append(limit)
        # One row per cluster by default - a cluster of six is one problem six
        # people reported, not six problems. SQLite pairs the bare columns with
        # the row that produced MAX(), so the representative is the strongest
        # member rather than an arbitrary one.
        group = (
            " GROUP BY COALESCE(NULLIF(cluster_id,''), key)" if collapse else ""
        )
        agg = "MAX(impact) impact" if collapse else "impact"
        return self.conn.execute(
            f"SELECT key, score, {agg}, frequency, severity, domain, who, source, "
            "channel, posted_at, points, comments, signal_terms, problem_statement, "
            "title, url, extracted_by, is_problem "
            f"FROM problems WHERE {' AND '.join(where)}{group} "
            "ORDER BY impact DESC, score DESC LIMIT ?",
            args,
        ).fetchall()

    def unextracted(self, limit: int = 100, model: str = "") -> list[sqlite3.Row]:
        """Highest-scoring rows the LLM pass has not judged yet."""
        return self.conn.execute(
            "SELECT key, title, raw_text, problem_statement, channel, source, url, score "
            "FROM problems WHERE COALESCE(extracted_by,'') != ? "
            "ORDER BY score DESC LIMIT ?",
            (model, limit),
        ).fetchall()

    def apply_extraction(self, updates: dict[str, dict]) -> int:
        """Write back what the LLM produced, keeping the heuristic fields as
        fallbacks whenever the model returned nothing for a field."""
        n = 0
        for key, u in updates.items():
            self.conn.execute(
                "UPDATE problems SET "
                "problem_statement = COALESCE(NULLIF(?,''), problem_statement), "
                "who = COALESCE(NULLIF(?,''), who), "
                "domain = COALESCE(NULLIF(?,''), domain), "
                "severity = ?, is_problem = ?, extracted_by = ?, impact = ? "
                "WHERE key = ?",
                (u.get("problem_statement", ""), u.get("who", ""), u.get("domain", ""),
                 int(u.get("severity", 0)), int(u.get("is_problem", True)),
                 u.get("extracted_by", ""), float(u.get("impact", 0.0)), key),
            )
            n += 1
            # FTS carries the statement, so it has to follow the rewrite.
            self.conn.execute("DELETE FROM problems_fts WHERE key = ?", (key,))
        self.conn.executemany(
            "INSERT INTO problems_fts (key,problem_statement,raw_text,title,who,domain,channel) "
            "SELECT key,problem_statement,raw_text,title,who,domain,channel FROM problems WHERE key = ?",
            [(k,) for k in updates],
        )
        self.conn.commit()
        return n

    def cluster_input(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT key, problem_statement, title, score, severity FROM problems"
        ).fetchall()

    def apply_clusters(self, rows: list[tuple]) -> int:
        """rows = [(cluster_id, frequency, impact, key), ...]"""
        self.conn.executemany(
            "UPDATE problems SET cluster_id = ?, frequency = ?, impact = ? WHERE key = ?",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def search_problems(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.problem_statement, p.who, p.domain, p.score, p.url, p.source "
            "FROM problems_fts f JOIN problems p ON p.key = f.key "
            "WHERE problems_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()

    def problem_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT source, COUNT(*) n, ROUND(AVG(score), 2) avg_score, "
            "COUNT(DISTINCT domain) domains FROM problems GROUP BY source"
        ).fetchall()

    def export_problems_jsonl(self, path: str | Path, min_score: float = 0.0) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with path.open("w", encoding="utf-8") as fh:
            for row in self.conn.execute(
                "SELECT * FROM problems WHERE score >= ? ORDER BY score DESC", (min_score,)
            ):
                d = dict(row)
                d.pop("raw", None)
                d["signal_terms"] = [x for x in (d.get("signal_terms") or "").split("|") if x]
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                n += 1
        return n

    # ---- export ----------------------------------------------------------
    def export_jsonl(self, path: str | Path, winners_only: bool = False) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        q = "SELECT * FROM projects" + (" WHERE is_winner=1" if winners_only else "")
        n = 0
        with path.open("w", encoding="utf-8") as fh:
            for row in self.conn.execute(q):
                d = dict(row)
                d["prizes"] = json.loads(d.get("prizes") or "[]")
                d.pop("raw", None)
                for k in ("themes", "tracks", "tech_stack", "platforms", "team", "prize_names"):
                    d[k] = [x for x in (d.get(k) or "").split("|") if x]
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                n += 1
        return n

    def export_csv(self, path: str | Path, winners_only: bool = False) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [c for c in ROW_COLUMNS if c != "raw"]
        q = f"SELECT {','.join(cols)} FROM projects" + (" WHERE is_winner=1" if winners_only else "")
        n = 0
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for row in self.conn.execute(q):
                w.writerow(list(row))
                n += 1
        return n

    def close(self) -> None:
        self.conn.close()
