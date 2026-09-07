# Hackathon Winner Idea Scraper

Collects the **winning projects** from public hackathons — the idea, the problem it
solves, the tech stack, the prize it won and the team — into a single searchable
SQLite database, then exports to JSONL/CSV.

Three sources are implemented today:

| Source | What it gives you |
|---|---|
| **Devfolio** (`onchain-summer.devfolio.co` and ~1,750 others) | Public search API. Richest fields: separate "problem it solves" / "challenges we ran into" sections, verified tech-stack tags, prize tracks, sponsors. |
| **Devpost** (5,086 ended hackathons) | Biggest and most general. Winner ribbon on the gallery card, full write-up + "Built With" tags on the project page. |
| **ETHGlobal** ([showcase](https://ethglobal.com/showcase)) | No API — parses the Next.js RSC payload. Every prize the project won, plus an AI-generated one-line summary the site itself publishes. |
| **Unstop** (6,200+ hackathons) | **Directory only — no project ideas.** Unstop publishes events, prize structures and required skills, but keeps teams, leaderboards and results behind a login. Use it to *discover* events, then find the projects on Devpost/Devfolio. |

See **[SOURCES.md](SOURCES.md)** for the wider directory of places to find hackathon
winners (Unstop, DoraHacks, lablab.ai, Taikai, MLH, Kaggle, …) and what data hook each
one has.

## Install

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

## Use

```bash
# the example from the brief: Onchain Summer winners
./.venv/bin/python run.py scrape --source devfolio --hackathon onchain-summer
```

```bash
# 20 most recent ended Devpost hackathons, winners only
./.venv/bin/python run.py --rate 1.5 scrape --source devpost --max-hackathons 20
```

```bash
# every prize-winning ETHGlobal showcase project (one global pass, all events)
./.venv/bin/python run.py scrape --source ethglobal --max-projects 500
```

```bash
# browse what is available before committing to a crawl
./.venv/bin/python run.py list-hackathons --source devfolio --limit 40
```

```bash
# full-text search the ideas you collected
./.venv/bin/python run.py search "ai agent marketplace"
```

```bash
./.venv/bin/python run.py export --format jsonl --winners-only
./.venv/bin/python run.py stats
```

### Options that matter

| Flag | Effect |
|---|---|
| `--rate 1.5` | seconds between requests (global flag, goes **before** the subcommand) |
| `--no-cache` | bypass the on-disk response cache in `data/.cache/` |
| `--all-projects` | store every submission, not only winners |
| `--no-details` | skip the per-project detail fetch (much faster, loses the long write-up) — Devpost & ETHGlobal only |
| `--max-projects N` | cap per hackathon |
| `--per-hackathon` | ETHGlobal: walk events one by one instead of the global showcase |

`--hackathon` applies to every `--source` you pass, so give it one source at a time.

## Mining problem statements (`mine`)

The scrapers above collect *answers* — projects people already built. `mine` collects
the other half: **unmet problems**, straight from the people complaining about them.

```bash
# every Hacker News post/comment of the last 180 days that reads like an unmet need
./.venv/bin/python run.py mine --source hn --since 180d --limit 1500
```

```bash
# every source that needs no credentials at all
./.venv/bin/python run.py mine --source hn stackexchange discourse lemmy appstore --since 2y
```

```bash
# Reddit: where non-developers complain (needs a free API app, see below)
./.venv/bin/python run.py mine --source reddit --since 1y --limit 600
./.venv/bin/python run.py mine --source reddit --channels restaurateur logistics --since 2y
```

### The sources

| Source | Auth | `--channels` means | What it is good for |
|---|---|---|---|
| **hn** | none | HN tags | Developer pain, and `Ask HN` threads that are pure problem statements |
| **stackexchange** | none | site names (`workplace`, `money`, `academia`, …) | 365 sites, most of them not about programming |
| **discourse** | none | forum hosts | Any Discourse instance — niche communities in their own vocabulary |
| **lemmy** | none | communities | Reddit-shaped content with no credentials to obtain |
| **appstore** | none | app ids or search terms | 1-3★ reviews: non-developers describing what a product fails to do |
| **reddit** | free app | subreddits | The biggest non-developer seam, but see below |

Only Reddit needs credentials. The other five run out of the box.

```bash
# narrow it to a space you care about
./.venv/bin/python run.py mine --source hn --query restaurant --since 2y --limit 500
./.venv/bin/python run.py problems --top 20 --domain fintech
./.venv/bin/python run.py problems --search "inventory" --export data/problems.jsonl
```

### How it works

```
HN Algolia -> pain-phrase filter -> problem sentence + who + domain -> score -> problems table
```

**No LLM.** `scrapers/social/signals.py` holds ~16 weighted regex families — from
`"I'd pay for"` and `"why is there no"` (weight 3) down to `"tedious"` and
`"takes forever"` (weight 1). A post is kept only when the weights sum to
`--min-weight` (default 3: one strong phrase, or a couple of weak ones together).
That throws away ~99% of the firehose for free, which matters because the eventual
LLM extraction pass should only ever read what survives here.

Each kept post becomes a `PainPoint`: the single sentence carrying the strongest
phrase, plus a `who` pulled from explicit self-identification (`"as a restaurant
owner, …"`), a `domain` guessed from a keyword map, and a score:

```
score = signal_weight x (1 + log1p(points + 2 x comments)) x 0.5 ^ (age_years)
```

Comments count double — a thread people argue in is a pain people share — and the
yearly halving stops four-year-old complaints (which may well be solved by now) from
crowding out live ones.

`problems` is a **separate table** from `projects`, on purpose: a project is
somebody's answer, a pain point is somebody's question. They are meant to meet in the
matcher, where "how many winning projects already attack this pain" becomes the
novelty signal.

Reddit is the exception to the filter-locally rule: its search understands quoted
phrases, so the weight-3 patterns run **server-side** as the query itself
(`"wish there was"`, `"why is there no"`, …). The keep rate there is ~30-60%, not
~1%, because the firehose was never downloaded in the first place.

### Source: Stack Exchange

Free, keyless, 300 requests/day. Two things were measured rather than assumed:

- `body=` matches a literal phrase and looked perfect, but returns almost nothing
  — 2 hits for `"wish there was"` across all of `workplace`, ever. The same phrase
  as `q=` returns a full page of 50, so the phrases go through `q=` and
  `signals.py` filters locally.
- **`--since` is ignored here.** Constrained to two years these sites return 0-1
  results per query — the network's traffic collapsed, and its value now is the
  archive. Results come back newest-first (`sort=creation`): sorting by votes
  returns the all-time greatest hits, measured at 2012-2019, which score ~0.02
  after recency decay and can never surface.

### Source: Discourse

Every Discourse instance exposes the same JSON API with no auth, so
`--channels forum.example.com` works for any of the thousands of them. Search
returns a blurb rather than the full post — enough to fire the filter and to
read, without one extra request per hit. Highest keep rate of any source
measured here (~58%), because the phrase search runs server-side.

`community.home-assistant.io` answers a browser but 403s this client, so it is
left out of the defaults.

### Source: Lemmy

Reddit-shaped, open API, no credentials — which is the point, given Reddit's
wall. Its search is fuzzy rather than phrase-exact, so filtering cannot be pushed
server-side; phrases are sent as plain terms and `signals.py` does the rest
(~9% keep rate).

### Source: App Store reviews

Apple's public review RSS, no key. The most direct pain source available: a
one-star review is somebody describing, unprompted, what a product failed to do
for them — and they are not developers.

Two steps, because reviews are per app: the iTunes Search API turns a topic into
app ids, then the RSS feed returns their reviews. Pass numeric ids as
`--channels` to skip discovery. Only 1-3★ reviews are read; a five-star review is
a testimonial, not a pain point.

Reviews carry no engagement signal, so these rows score on pain phrases and
recency alone — not inflated with a fake vote count.

### Source: Hacker News

`hn.algolia.com/api/v1` — no key, no auth, ~10k requests/hour, and it indexes comments
as first-class documents, which is where most real complaining happens. The scraper
sweeps `ask_hn`, `story` and `comment` round-robin (draining them in order would let
`ask_hn` eat the whole `--limit`).

Algolia caps `page` at 1000 documents, so a plain page walk silently stops there. This
uses a `created_at_i` cursor instead: every request is page 0 of a shrinking time
window, so depth is unbounded and each window stays a distinct, cacheable URL.

### Source: Reddit

**Needs credentials.** Every anonymous Reddit JSON endpoint now answers 403 or
redirects to a login wall — `www`, `api` and `old` alike, with any User-Agent. Make a
free app at <https://www.reddit.com/prefs/apps> ("create another app" → type
**script**), then:

```bash
export REDDIT_CLIENT_ID=xxxxxxxx
export REDDIT_CLIENT_SECRET=xxxxxxxxxxxxxxxx
export REDDIT_USER_AGENT="python:idea-scraping:v0.1 (by /u/yourname)"   # optional
```

No account password is involved. The scraper uses the `client_credentials` grant,
which returns an app-only token that reads public data and nothing else. The token is
kept in memory and never written to `data/.cache/`.

Defaults sweep eight non-developer subreddits — `smallbusiness`, `Entrepreneur`,
`freelance`, `nonprofit`, `restaurateur`, `sysadmin`, `Teachers`, `Accounting` —
crossed with six pain phrases, round-robin so one busy subreddit cannot eat the whole
`--limit`. Override with `--channels`, and `--query` replaces the six phrases with
your own search.

`--since` is rounded **up** to Reddit's coarse search buckets (`day`/`week`/`month`/
`year`/`all`), then filtered exactly by timestamp on the way through.

## Turning signals into statements (`extract`, `cluster`)

`mine` is a recall device. Two passes turn what it kept into something rankable.

### `extract` — the LLM pass

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./.venv/bin/python run.py extract --limit 100        # or --dry-run to see the prompt first
```

Sends the highest-scoring un-judged rows to Claude in batches of 20 and gets back,
per post: a rewritten one-sentence `problem_statement`, `who` suffers, a `domain`, a
1-5 `severity`, and `is_problem` — the one judgement regex cannot make. A launch post
bragging about the "workaround" it replaces trips every keyword and is not a pain
point; the model rejects it, and `problems` hides it unless you pass
`--include-rejected`.

Structured outputs (`output_config.format`) guarantee parseable JSON. Rows are marked
with the model that judged them, so a re-run is free and switching `--model` re-judges
everything. Heuristic values survive wherever the model returns an empty field.

Default model is `claude-haiku-4-5` — this is bulk classification over pre-filtered
text. Pass `--model claude-opus-5` for sharper statements at ~5x the input price.

### `cluster` — merging duplicate complaints

```bash
./.venv/bin/python run.py cluster            # --threshold 0.32 by default
```

Forty people describing the same friction in forty different sentences is the single
strongest signal in the table, and it does not exist until they are merged. This is
TF-IDF cosine over the statements with union-find, deliberately lexical for the same
reason `matcher.py` is: no model download, no API call, deterministic and explainable.

Candidate pairs come from an inverted index — two rows are only compared if they share
a term that is not near-ubiquitous — so the work tracks real overlap instead of n².
Light suffix stripping merges `schedule` / `schedules` / `scheduling`, which is the
difference between catching a duplicate and missing it.

Measured on hand-written duplicates: a genuine pair scores 0.35-0.40, an unrelated
pair 0.10. Merging is transitive, so raise `--threshold` if clusters start looking
like topics rather than duplicates.

`problems` then shows **one row per cluster** (its strongest member, marked `x3`);
`--expand` lists every member.

### Ranking: `impact`

```
impact = score x (1 + ln(frequency)) x severity/3
```

`score` is the heuristic signal from `mine`, `frequency` comes from `cluster`,
`severity` from `extract`. Severity 3 is neutral, so a row no LLM has judged is
neither promoted nor punished, and `problems` sorts by `impact` whether or not you
have run the other two passes.

### Known limits

- **Reddit is submissions only.** Comments would cost one request per post; the
  selftext of a complaint post is usually the complaint anyway.
- **Precision over recall.** At `--min-weight 3` roughly 0.5–1% of scanned posts are
  kept. Lower it to 2 for a wider, noisier net.
- **Before `extract`, the statement is a lifted sentence, not a summary.** `"No more
  scheduling by hand."` is a real signal but a poor problem statement.
- **Clustering is lexical.** "agritech" and "farming" will not merge unless the corpus
  uses both. The fix is more sources, not a higher threshold.
- **HN is developers.** Expect devtools/data-ai to dominate. Non-technical pain needs
  Reddit or app-store reviews.
- Re-running `mine` upserts by post id, so rows kept under a looser `--min-weight`
  stay until you delete them.

## The join: `gap`

This is the point of keeping both tables. `problems` is what people complain
about; `projects` is ~13,000 things people already built. The answer to "what
should we build this weekend" is a pain point with high impact that **nothing in
the corpus attacks**.

```bash
./.venv/bin/python run.py gap --top 15
./.venv/bin/python run.py gap --unsolved-only --domain fintech
```

```
[gap   3.69]  impact 11.08
  Ask HN: Why isn't there a cursor for video games development.
  2 project(s) already attack this:
    - InteractionKit (0.61 coverage)  Kiroween
    - MS Paint: Ctrl Z through time (0.6 coverage)  Kiro Hackathon

[gap   9.03]  impact 9.03  data-ai
  <statement>
  no project in the corpus attacks this  <- white space
```

### Why coverage, not a BM25 score

A raw BM25 score cannot answer "is this project about this problem" — scores are
only comparable *within* one query, so a fixed cut-off is meaningless and the top
hit always exists, which would make every problem look solved.

The test is **coverage**: how much of the problem's distinctive vocabulary
(idf-weighted, so "restaurant" counts and "system" does not) actually appears in
the project. Coverage is measured over the **five rarest terms** rather than the
whole sentence — a lifted forum sentence can run 40 words, and demanding 60% of
that is unmeetable, which would paint everything as white space.

Calibrated against this corpus on hand-written statements:

| statement | projects at >=50% coverage |
|---|---|
| farmers cannot tell when crops need irrigation | 12 |
| why isn't there a cursor for game development | 38 |
| rambling forum post about LLM "awareness" | 0 |
| deliberately nonsense sentence | 0 |

Then `gap_score = impact / (1 + projects_attacking)` — the first competitor halves
the opportunity, the tenth barely moves it.

## Dashboard

```bash
./.venv/bin/python dashboard.py
```

Opens on <http://127.0.0.1:5000>, with a sidebar holding the filters (source,
hackathon, tech, sort, winners-only) and three panels:

**Overview** — counts by source, most common tech, biggest hackathons, most awarded
prizes, newest submissions.

**Browse ideas** — every scraped project. The search box here is SQLite FTS5: every
word must appear.

**Topic match** — describe a topic in plain language ("reducing food waste for
restaurants") and get ideas ranked by relevance. See below.

**Problem radar** — the `gap` analysis as a panel: every mined pain point ranked by
gap score, filterable by domain and source, each row showing either the projects
already attacking it (with their % overlap) or a **white space** badge. This is the
one screen where both halves of the database are visible at once.

It reads the same SQLite file the scrapers write, so the counters keep updating while
a crawl is still running.

### Topic match

`matcher.py` builds a BM25 index over each project's title, tagline, problem
statement, write-up, tech stack and themes. Unlike the FTS search it uses OR
semantics with ranking, so a project can surface on partial topic overlap; fields are
weighted (a term in the title counts 4×, in the write-up 1×). Every result shows its
relevance score and **which query words matched in which field**, so you can see why
it ranked.

It also mines *related terms* from the top results — ranked by lift (how much more
often a term appears in the results than in the corpus at large, not raw frequency,
which just resurfaces common English). Those become one-click chips to sharpen a vague
topic.

**This is lexical, not semantic.** There are no embeddings: a synonym the corpus never
uses alongside your words ("agritech" vs "farming") will not match on its own. The
related-term chips exist precisely to bridge that gap. The index rebuilds when the
database grows, rate-limited to once every 30s so queries during a live crawl stay
fast (~8ms after the first).

## Layout

```
run.py              CLI
dashboard.py        Flask dashboard (templates/dashboard.html)
scrapers/
  models.py         Project / Prize / PainPoint dataclasses + row schemas
  base.py           HTTP client: rate limit, retry w/ backoff, disk cache
  devfolio.py       api.devfolio.co search API
  devpost.py        devpost.com JSON directory + gallery/detail HTML
  ethglobal.py      RSC flight-payload parser
  social/
    base.py         SocialPost + the SocialScraper interface
    signals.py      weighted pain-phrase filter -> PainPoint
    hn.py           Hacker News via the Algolia API
    reddit.py       Reddit via the OAuth API (app-only token)
    stackexchange.py  Stack Exchange network search
    discourse.py    any Discourse forum's search.json
    lemmy.py        Lemmy instance search
    appstore.py     iTunes search + review RSS
gap.py              problem x project cross-match (coverage test)
extract.py          LLM pass: rewrite / judge / rate a mined pain point
cluster.py          TF-IDF + union-find merge of duplicate complaints
store/db.py         SQLite + FTS5 + CSV/JSONL export (projects *and* problems)
data/               database, exports, response cache
```

## The unified schema

Every source is normalised onto the same `Project` record:

`source, source_id, url, title, tagline, description, problem_solved, challenges,
themes, tracks, tech_stack, platforms, is_winner, prizes[], hackathon_name/slug/url/
start/end, team, demo_url, repo_url, video_url, likes, views, submitted_at, raw`

`prizes` is a list of `{name, rank, sponsor, track, description}` — `rank` is what you
want for ranking ("1st place", "Grand Prize: Build & Grow Award", "Finalist").
`raw` keeps the untouched source payload so you can re-derive fields later without
re-crawling.

Rows are keyed by a stable `sha1(source:source_id)`, so re-running a scrape updates in
place rather than duplicating.

## Known limits

- **Devfolio**'s ranking is not a total order across its Elasticsearch shards, so
  `from`/`size` windows silently repeat and skip documents — and *which* ones drift
  between calls. A single linear sweep of onchain-summer returns 575 hits containing
  only 553–574 distinct projects. The scraper therefore sweeps repeatedly with
  alternating page sizes (200/50/100/25), de-duplicating on uuid, until it has the
  full reported count; onchain-summer reaches all 575 on the third sweep. It also
  caps at Elasticsearch's 10,000-document window (no real hackathon comes close).
- **Devfolio "winners" ≠ prize winners.** Its `filter=winners` includes projects whose
  only award is a `Participation Reward`. For onchain-summer that is 464 of 575. The
  scraper strips those prizes by default (`exclude_participation=True`), so the row is
  still stored but `is_winner=0` — leaving **111** projects that took a real prize.
  Pass `exclude_participation=False` to the scraper if you want the platform's
  definition instead.
- **Devpost** requires a browser `User-Agent` — the edge returns 403 otherwise. It has
  no per-project JSON, so the long write-up costs one HTML fetch per project; use
  `--no-details` when you only need titles and taglines.
- **Devpost galleries are ordered winners-first**, which the scraper relies on to stay
  tractable: measured on `revenuecat-shipaton-2025`, page 1 is 24/24 winners, page 2 is
  6/24, and pages 3-34 are 0/24. When `winners_only` is set it stops after two
  consecutive winner-free pages — 3 requests instead of 34 there, and 1 instead of
  hundreds for an event whose winners are not announced yet. Without this a single
  large hackathon took 11 minutes; with it, ~3 seconds. The two-page grace guards
  against a gallery that does not use that ordering.
- **ETHGlobal** ships prize objects on the showcase list page with their text fields
  hoisted out by RSC de-duplication, so prize *names* only exist on the detail page —
  the scraper always fetches it. Its `tech_stack` is keyword-extracted from the
  write-up (ETHGlobal has no structured tech field), so treat it as approximate.
- Devpost's `--max-hackathons N` walks the *most recently ended* hackathons first, and
  those often have no winners announced yet — go deeper (`--max-hackathons 50`) or name
  a finished event directly (`--hackathon revenuecat-shipaton-2025`) to get results.
- Devpost's `is_winner` comes from the ribbon on the gallery card; the exact prize name
  is only recovered during the detail fetch.

## Etiquette

All of this is public, unauthenticated data. The default is 1 request/second with
jitter, four retries with exponential backoff, and a 7-day on-disk cache so re-runs
cost nothing. Leave those on. Check each platform's terms of use before running a large
crawl or redistributing what you collect.
