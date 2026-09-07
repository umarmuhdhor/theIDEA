#!/usr/bin/env python3
"""CLI for scraping winning hackathon projects.

Examples
--------
  # one Devfolio hackathon (the Onchain Summer example)
  python run.py scrape --source devfolio --hackathon onchain-summer

  # 20 most recent ended Devpost hackathons, winners only
  python run.py scrape --source devpost --max-hackathons 20

  # every ETHGlobal showcase project that won a prize
  python run.py scrape --source ethglobal --max-projects 500

  # mine Hacker News for unmet needs, then rank them
  python run.py mine --source hn --query restaurant --since 180d
  python run.py mine --source reddit --channels smallbusiness restaurateur
  python run.py mine --source stackexchange discourse lemmy appstore
  python run.py extract --limit 100          # LLM: rewrite, judge, rate severity
  python run.py cluster                      # merge duplicate complaints
  python run.py gap --unsolved-only          # pains no winning project attacks
  python run.py problems --top 20 --domain fintech

  python run.py list-hackathons --source devfolio --limit 30
  python run.py search "agent marketplace"
  python run.py export --format jsonl --winners-only
  python run.py stats
"""
from __future__ import annotations

import argparse
import logging
import sys

import cluster as cluster_mod
import extract as extract_mod
import gap as gap_mod
from matcher import TopicMatcher
from scrapers import SCRAPERS, HttpClient
from scrapers.social import SOCIAL_SCRAPERS, to_painpoint
from store import Store

log = logging.getLogger("run")

FLUSH_EVERY = 25   # rows per upsert, and how often progress is logged


def build(source: str, args) -> object:
    client = HttpClient(rate=args.rate, cache=not args.no_cache)
    kwargs = {"client": client}
    if source in ("devpost", "ethglobal"):
        kwargs["fetch_details"] = not args.no_details
    return SCRAPERS[source](**kwargs)


def cmd_scrape(args) -> int:
    store = Store(args.db)
    total = 0
    for source in args.source:
        scraper = build(source, args)
        if not getattr(scraper, "has_project_submissions", True):
            log.warning(
                "[%s] is a hackathon directory, not a project source - it publishes "
                "no submissions to scrape. Use `list-hackathons --source %s` instead.",
                source, source,
            )
            continue
        if args.hackathon:
            targets = [{"slug": h} for h in args.hackathon]
        elif source == "ethglobal" and not args.per_hackathon:
            targets = [None]  # showcase is global; one pass covers every event
        else:
            # Stream the directory rather than materialising it: enumerating all
            # of Devpost's ended hackathons is ~565 requests on its own, and
            # buffering them means nothing reaches the database until that
            # finishes (and nothing survives a crash mid-way).
            targets = scraper.hackathons(limit=args.max_hackathons)

        for i, hack in enumerate(targets, 1):
            label = (hack or {}).get("slug", "<all>") if isinstance(hack, dict) else (hack or "<all>")
            batch, found = [], 0
            try:
                for p in scraper.projects(
                    hackathon=hack,
                    winners_only=not args.all_projects,
                    limit=args.max_projects,
                ):
                    batch.append(p)
                    found += 1
                    # Flush often and report progress: sources like ETHGlobal
                    # expose their whole showcase as a single target, so without
                    # this there is no log line and nothing in the database until
                    # the entire crawl finishes.
                    if len(batch) >= FLUSH_EVERY:
                        total += store.upsert(batch)
                        batch = []
                        log.info("[%s] %s: %d projects so far", source, label, found)
            except Exception as e:
                log.error("[%s] %s failed: %s", source, label, e)
            total += store.upsert(batch)
            log.info("[%s] %d %s -> %d stored so far", source, i, label, total)
            if not batch and not args.all_projects:
                log.info("[%s] %s had no prize-winners (winners are often announced "
                         "weeks after a hackathon ends)", source, label)

    print(f"\nstored/updated {total} projects in {args.db}")
    for row in store.stats():
        print(f"  {row['source']:10s} {row['n']:6d} projects  {row['winners'] or 0:6d} winners  "
              f"{row['hackathons']:5d} hackathons")
    store.close()
    return 0


def cmd_list_hackathons(args) -> int:
    for source in args.source:
        scraper = build(source, args)
        print(f"\n=== {source} ===")
        for h in scraper.hackathons(limit=args.limit):
            print(f"{h['slug']:40.40s} {h['name'][:50]:52.52s} {h.get('ends_at','')[:10]}")
    return 0


SINCE_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_since(value: str) -> int:
    """'90d' / '6m' / '2y' -> days. A bare number is already days."""
    value = value.strip().lower()
    if value[-1] in SINCE_UNITS:
        return int(float(value[:-1]) * SINCE_UNITS[value[-1]])
    return int(value)


def cmd_mine(args) -> int:
    """Social firehose -> pain filter -> `problems` table."""
    store = Store(args.db)
    since_days = parse_since(args.since)
    stored = 0
    for source in args.source:
        client = HttpClient(rate=args.rate, cache=not args.no_cache)
        scraper = SOCIAL_SCRAPERS[source](client=client)
        seen = kept = 0
        batch = []
        try:
            for post in scraper.posts(
                query=args.query, since_days=since_days, limit=args.limit,
                channels=args.channels or None,
            ):
                seen += 1
                pain = to_painpoint(post, min_weight=args.min_weight)
                if pain is None or pain.score < args.min_score:
                    continue
                batch.append(pain)
                kept += 1
                if len(batch) >= FLUSH_EVERY:
                    stored += store.upsert_problems(batch)
                    batch = []
                    log.info("[%s] %d/%d posts carried a pain signal", source, kept, seen)
        except Exception as e:
            log.error("[%s] mine failed: %s", source, e)
        stored += store.upsert_problems(batch)
        rate = f"{100 * kept / seen:.1f}%" if seen else "n/a"
        log.info("[%s] scanned %d posts, kept %d (%s)", source, seen, kept, rate)

    print(f"\nstored/updated {stored} pain points in {args.db}")
    for row in store.problem_stats():
        print(f"  {row['source']:10s} {row['n']:6d} problems  avg score {row['avg_score']}")
    store.close()
    return 0


def cmd_problems(args) -> int:
    store = Store(args.db)
    if args.search:
        rows = store.search_problems(args.search, limit=args.top)
    else:
        rows = store.top_problems(
            limit=args.top, domain=args.domain, source=args.source,
            min_score=args.min_score, include_rejected=args.include_rejected,
            collapse=not args.expand,
        )
    if not rows:
        print("no pain points yet - run `mine` first")
    for r in rows:
        k = r.keys()
        head = f"[{r['impact']:>6}]" if "impact" in k else f"[{r['score']:>6}]"
        if "frequency" in k and (r["frequency"] or 1) > 1:
            head += f" x{r['frequency']}"
        if "severity" in k and r["severity"]:
            head += f" sev{r['severity']}"
        if "domain" in k and r["domain"]:
            head += f" {r['domain']}"
        if "who" in k and r["who"]:
            head += f" | who: {r['who']}"
        print(f"\n{head}")
        print(f"  {r['problem_statement']}")
        if "signal_terms" in k:
            print(f"  signals: {r['signal_terms'].replace('|', ', ')}"
                  f"   {r['points']}pts {r['comments']}c   {r['posted_at'][:10]}")
        print(f"  {r['url']}")
    if args.export:
        n = store.export_problems_jsonl(args.export, args.min_score)
        print(f"\nwrote {n} rows -> {args.export}")
    store.close()
    return 0


def cmd_extract(args) -> int:
    store = Store(args.db)
    res = extract_mod.run(
        store, limit=args.limit, batch_size=args.batch,
        model=args.model, dry_run=args.dry_run,
    )
    if not args.dry_run:
        print(f"\njudged {res['updated']} of {res['seen']} rows with {args.model}"
              f"  ({res['rejected']} rejected as not-a-problem)")
        if res["updated"]:
            print("run `cluster` next so frequency and impact catch up")
    store.close()
    return 0


def cmd_cluster(args) -> int:
    store = Store(args.db)
    st = cluster_mod.run(store, threshold=args.threshold)
    print(f"{st['rows']} rows -> {st['clusters']} clusters "
          f"({st['merged']} merges, largest {st['largest']})")
    store.close()
    return 0


def cmd_gap(args) -> int:
    """Pain points ranked by how little of the project corpus already answers them."""
    store = Store(args.db)
    rows = gap_mod.analyze(
        store, TopicMatcher(args.db), top=args.top, min_coverage=args.coverage,
        domain=args.domain, source=args.source, min_impact=args.min_impact,
        unsolved_only=args.unsolved_only,
    )
    gap_mod.hydrate(store.conn, rows)
    if not rows:
        print("nothing to report - run `mine` first, then `cluster`")
    for r in rows:
        head = f"[gap {r['gap_score']:>6}]  impact {r['impact']}"
        if (r["frequency"] or 1) > 1:
            head += f" x{r['frequency']}"
        if r["domain"]:
            head += f"  {r['domain']}"
        print(f"\n{head}")
        print(f"  {r['problem_statement']}")
        if r["who"]:
            print(f"  who: {r['who']}")
        if r["solved_by"]:
            print(f"  {r['solved_by']} project(s) already attack this:")
            for pr in r["projects"]:
                print(f"    - {pr.get('title','?')} ({pr.get('coverage')} coverage)"
                      f"  {pr.get('hackathon_name','')}")
        else:
            print("  no project in the corpus attacks this  <- white space")
        print(f"  {r['url']}")
    store.close()
    return 0


def cmd_search(args) -> int:
    store = Store(args.db)
    rows = store.search(args.query, limit=args.limit)
    if not rows:
        print("no matches")
    for r in rows:
        print(f"\n{r['title']}  [{r['source']}]")
        print(f"  {r['tagline']}")
        print(f"  {r['hackathon_name']} — {r['prize_names']}")
        print(f"  {r['url']}")
    store.close()
    return 0


def cmd_export(args) -> int:
    store = Store(args.db)
    out = args.out or f"data/ideas.{args.format}"
    n = (store.export_jsonl if args.format == "jsonl" else store.export_csv)(out, args.winners_only)
    print(f"wrote {n} rows -> {out}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    print(f"total projects: {store.count()}   winners: {store.count('is_winner=1')}")
    for row in store.stats():
        print(f"  {row['source']:10s} {row['n']:6d} projects  {row['winners'] or 0:6d} winners  "
              f"{row['hackathons']:5d} hackathons")
    probs = store.problem_stats()
    if probs:
        print(f"\npain points: {store.conn.execute('SELECT COUNT(*) FROM problems').fetchone()[0]}")
        for row in probs:
            print(f"  {row['source']:10s} {row['n']:6d} problems  avg score {row['avg_score']:6}  "
                  f"{row['domains']:3d} domains")
    store.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape winning hackathon project ideas")
    ap.add_argument("--db", default="data/hackathon_ideas.db")
    ap.add_argument("--rate", type=float, default=1.0, help="min seconds between requests")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape")
    s.add_argument("--source", nargs="+", choices=list(SCRAPERS), default=["devfolio"])
    s.add_argument("--hackathon", nargs="*", help="explicit hackathon slug(s)")
    s.add_argument("--max-hackathons", type=int, default=10)
    s.add_argument("--max-projects", type=int, default=None, help="cap per hackathon")
    s.add_argument("--all-projects", action="store_true", help="not just winners")
    s.add_argument("--no-details", action="store_true", help="skip per-project detail fetches")
    s.add_argument("--per-hackathon", action="store_true", help="ethglobal: iterate events instead of the global showcase")
    s.set_defaults(func=cmd_scrape)

    l = sub.add_parser("list-hackathons")
    l.add_argument("--source", nargs="+", choices=list(SCRAPERS), default=["devfolio"])
    l.add_argument("--limit", type=int, default=50)
    l.add_argument("--no-details", action="store_true")
    l.set_defaults(func=cmd_list_hackathons)

    m = sub.add_parser("mine", help="mine social sources for problem statements")
    m.add_argument("--source", nargs="+", choices=list(SOCIAL_SCRAPERS), default=["hn"])
    m.add_argument("--query", default="", help="keyword filter; empty = firehose")
    m.add_argument("--since", default="365d", help="how far back: 90d / 6m / 2y")
    m.add_argument("--channels", nargs="+", default=[],
                   help="what to sweep, per source: reddit=subreddits, "
                        "stackexchange=site names, discourse=forum hosts, "
                        "lemmy=communities, appstore=app ids or search terms")
    m.add_argument("--limit", type=int, default=500, help="max posts to scan per source")
    m.add_argument("--min-weight", type=int, default=3,
                   help="minimum summed pain-phrase weight to keep a post; "
                        "3 = one strong phrase or two weak ones, 2 = looser/noisier")
    m.add_argument("--min-score", type=float, default=0.0,
                   help="minimum final score (signal x engagement x recency)")
    m.set_defaults(func=cmd_mine)

    pb = sub.add_parser("problems", help="rank the pain points already mined")
    pb.add_argument("--top", type=int, default=20)
    pb.add_argument("--domain", default="")
    pb.add_argument("--source", default="")
    pb.add_argument("--min-score", type=float, default=0.0)
    pb.add_argument("--search", default="", help="FTS query over the statements")
    pb.add_argument("--expand", action="store_true",
                    help="show every member of a cluster, not just its top row")
    pb.add_argument("--include-rejected", action="store_true",
                    help="also show rows the LLM pass judged not-a-problem")
    pb.add_argument("--export", help="also write matching rows to a JSONL file")
    pb.set_defaults(func=cmd_problems)

    ex = sub.add_parser("extract", help="LLM pass: rewrite, judge and rate mined rows")
    ex.add_argument("--limit", type=int, default=100, help="highest-scoring rows to send")
    ex.add_argument("--batch", type=int, default=extract_mod.BATCH, help="rows per API call")
    ex.add_argument("--model", default=extract_mod.DEFAULT_MODEL)
    ex.add_argument("--dry-run", action="store_true",
                    help="print the prompt and the first batch, call nothing")
    ex.set_defaults(func=cmd_extract)

    cl = sub.add_parser("cluster", help="merge duplicate complaints, set frequency")
    cl.add_argument("--threshold", type=float, default=cluster_mod.DEFAULT_THRESHOLD,
                    help="cosine similarity to merge two statements (0-1)")
    cl.set_defaults(func=cmd_cluster)

    g = sub.add_parser("gap", help="cross-match pain points against the scraped projects")
    g.add_argument("--top", type=int, default=15)
    g.add_argument("--coverage", type=float, default=gap_mod.DEFAULT_COVERAGE,
                   help="share of the problem's distinctive terms a project must "
                        "cover to count as attacking it (0-1)")
    g.add_argument("--domain", default="")
    g.add_argument("--source", default="")
    g.add_argument("--min-impact", type=float, default=0.0)
    g.add_argument("--unsolved-only", action="store_true",
                   help="only pains no project attacks at all")
    g.set_defaults(func=cmd_gap)

    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=cmd_search)

    e = sub.add_parser("export")
    e.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    e.add_argument("--out")
    e.add_argument("--winners-only", action="store_true")
    e.set_defaults(func=cmd_export)

    st = sub.add_parser("stats")
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
