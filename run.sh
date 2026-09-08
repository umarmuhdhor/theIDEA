#!/usr/bin/env bash
#
# run.sh — one entry point for the hackathon-idea pipeline.
#
# The project is the `ideas` package: a CLI (ideas/cli.py) plus a Flask
# dashboard (ideas/web/app.py), both driven from a local virtualenv in .venv.
# This script owns the venv, loads .env, and exposes the stages as commands.
#
#   ./run.sh                       ALL of it: setup -> scrape -> mine -> cluster
#                                  -> gap -> dashboard. Any stage whose output is
#                                  already in the database is skipped.
#   ./run.sh all [--force]         same thing, explicitly (--force redoes stages)
#   ./run.sh setup                 create .venv and install requirements.txt
#   ./run.sh doctor                report venv / deps / env keys / database state
#   ./run.sh scrape [args...]      collect winning projects  (ideas scrape)
#   ./run.sh mine   [args...]      collect pain points       (ideas mine)
#   ./run.sh extract [args...]     LLM judge pass            (ideas extract)
#   ./run.sh cluster [args...]     merge duplicate pains     (ideas cluster)
#   ./run.sh gap [args...]         pains no project attacks  (ideas gap)
#   ./run.sh problems|search|export|stats|list-hackathons [args...]
#   ./run.sh dashboard [args...]   Flask UI (port 5050; PORT=n to change)
#   ./run.sh pipeline [args...]    scrape -> mine -> cluster -> gap (no LLM)
#   ./run.sh demo                  small end-to-end run, safe defaults
#   ./run.sh py [args...]          raw `python -m ideas` passthrough
#   ./run.sh python [args...]      raw venv python
#   ./run.sh clean [--all]         drop HTTP cache (--all also drops the db)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
DB="${DB:-data/hackathon_ideas.db}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ---------------------------------------------------------------- output ----
if [ -t 1 ]; then
  B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
else
  B=""; R=""; G=""; Y=""; C=""; N=""
fi
info() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
ok()   { printf '%s  ok%s  %s\n' "$G" "$N" "$*"; }
warn() { printf '%swarn%s  %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%serr%s   %s\n' "$R" "$N" "$*" >&2; exit 1; }

# ------------------------------------------------------------------- env ----
# .env is optional. Only ANTHROPIC_API_KEY (extract) and the REDDIT_* trio
# (mine --source reddit) are ever read; every other source is keyless.
load_env() {
  [ -f "$ROOT/.env" ] || return 0
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
}

# ----------------------------------------------------------------- venv -----
have_venv() { [ -x "$PY" ]; }

ensure_venv() {
  if ! have_venv; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$PYTHON_BIN not found; set PYTHON_BIN=/path/to/python3"
    info "creating virtualenv at .venv ($($PYTHON_BIN --version 2>&1))"
    "$PYTHON_BIN" -m venv "$VENV"
    "$PIP" install --quiet --upgrade pip
    info "installing requirements.txt"
    "$PIP" install --quiet -r requirements.txt
    ok "virtualenv ready"
    return
  fi
  # venv exists — make sure the imports the CLI needs are actually present.
  if ! "$PY" - <<'PYEOF' >/dev/null 2>&1
import bs4, dateutil, flask, lxml, requests, tenacity, tqdm  # noqa: F401
PYEOF
  then
    info "installing missing requirements into existing .venv"
    "$PIP" install --quiet -r requirements.txt
  fi
}

cmd_setup() {
  ensure_venv
  "$PIP" install --quiet -r requirements.txt
  mkdir -p data
  ok "setup complete — try: ./run.sh demo"
}

# --------------------------------------------------------------- doctor -----
cmd_doctor() {
  load_env
  printf '%sproject%s   %s\n' "$B" "$N" "$ROOT"

  if have_venv; then ok "venv        $("$PY" --version 2>&1)"
  else warn "venv        missing — run ./run.sh setup"; fi

  if have_venv; then
    "$PY" - <<'PYEOF'
import importlib.util as u
mods = [("requests","requests"),("bs4","beautifulsoup4"),("lxml","lxml"),
        ("tenacity","tenacity"),("dateutil","python-dateutil"),("tqdm","tqdm"),
        ("flask","flask"),("anthropic","anthropic")]
for mod, pkg in mods:
    mark = "  ok " if u.find_spec(mod) else "MISS "
    print(f"{mark} dep         {pkg}")
PYEOF
  fi

  if [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    ok "ANTHROPIC   key set (extract enabled)"
  else
    warn "ANTHROPIC   no key — 'extract' needs ANTHROPIC_API_KEY (every other command works without it)"
  fi
  if [ -n "${REDDIT_CLIENT_ID:-}" ] && [ -n "${REDDIT_CLIENT_SECRET:-}" ]; then
    ok "REDDIT      credentials set"
  else
    warn "REDDIT      no credentials — 'mine --source reddit' unavailable; hn/stackexchange/discourse/lemmy/appstore are keyless"
  fi

  if [ -f "$DB" ]; then
    have_venv && "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def n(t):
    return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in have else "-"
print(f"  ok  database    {db}: {n('projects')} projects, {n('problems')} problems")
PYEOF
  else
    warn "database    $DB not created yet — any scrape/mine creates it"
  fi
}

# ------------------------------------------------------------- cli stages ---
# Every pipeline stage is the same shape: ensure the venv, then hand the
# arguments straight to the CLI so its own --help and flags stay authoritative.
run_py() {
  ensure_venv
  load_env
  exec "$PY" -m ideas "$@"
}

# --db is a *global* flag on the CLI, so it must precede the subcommand.
run_stage() {
  local stage="$1"; shift
  ensure_venv
  load_env
  "$PY" -m ideas --db "$DB" "$stage" "$@"
}

# Port 5000 is not usable by default on macOS: AirPlay Receiver (ControlCenter)
# listens on *:5000, including ::1. Flask binds 127.0.0.1 only, so `curl
# 127.0.0.1:5000` reaches the dashboard while a browser — which resolves
# `localhost` to ::1 first — reaches AirPlay and shows "HTTP ERROR 403".
# Default to 5050 instead, and step past anything already taken.
free_port() {
  "$PY" - "$1" <<'FREEPORT'
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 50):
    # Probe both families: a port is only free if nothing answers on either,
    # otherwise the browser and curl can disagree about who owns it.
    taken = False
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            if s.connect_ex((host, port)) == 0:
                taken = True
        finally:
            s.close()
        if taken:
            break
    if not taken:
        print(port)
        break
else:
    print(start)
FREEPORT
}

cmd_dashboard() {
  ensure_venv
  load_env
  [ -f "$DB" ] || die "no database at $DB — run ./run.sh (or ./run.sh scrape ...) first"

  # An explicit --port/--host from the caller wins; only pick one otherwise.
  local has_port=0
  for a in "$@"; do case "$a" in --port|--port=*) has_port=1 ;; esac; done

  if [ "$has_port" = 1 ]; then
    exec "$PY" -m ideas.web.app --db "$DB" "$@"
  fi

  local want="${PORT:-5050}" port
  port="$(free_port "$want")"
  [ "$port" = "$want" ] || warn "port $want busy — using $port"
  info "dashboard on http://localhost:$port  (ctrl-c to stop)"
  exec "$PY" -m ideas.web.app --db "$DB" --port "$port" "$@"
}

# -------------------------------------------------------------- pipeline ----
# scrape (answers) -> mine (questions) -> cluster (dedupe) -> gap (white space).
# `extract` is deliberately NOT in here: it is the only stage that costs money.
cmd_pipeline() {
  ensure_venv
  load_env
  local hack_src="${SCRAPE_SOURCE:-devfolio}"
  local mine_src="${MINE_SOURCE:-hn}"
  local max_hacks="${MAX_HACKATHONS:-10}"
  local since="${SINCE:-365d}"
  local limit="${MINE_LIMIT:-500}"

  info "1/4 scrape  — winning projects from $hack_src"
  "$PY" -m ideas --db "$DB" scrape --source $hack_src --max-hackathons "$max_hacks"

  info "2/4 mine    — pain points from $mine_src (since $since, limit $limit)"
  "$PY" -m ideas --db "$DB" mine --source $mine_src --since "$since" --limit "$limit"

  info "3/4 cluster — merge duplicate complaints"
  "$PY" -m ideas --db "$DB" cluster

  info "4/4 gap     — pains no project attacks"
  "$PY" -m ideas --db "$DB" gap --top "${GAP_TOP:-15}"

  if [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    info "optional: ./run.sh extract --limit 100   (LLM pass, then re-run cluster)"
  fi
  ok "pipeline done — ./run.sh dashboard to browse it"
}

# Small, bounded version of the pipeline: good for a first run and for checking
# the install works end to end without a long crawl.
cmd_demo() {
  ensure_venv
  load_env
  info "demo 1/4 scrape  — devfolio onchain-summer winners"
  "$PY" -m ideas --db "$DB" scrape --source devfolio --hackathon onchain-summer --max-projects 50

  info "demo 2/4 mine    — Hacker News, last 180 days, 200 posts"
  "$PY" -m ideas --db "$DB" mine --source hn --since 180d --limit 200

  info "demo 3/4 cluster"
  "$PY" -m ideas --db "$DB" cluster

  info "demo 4/4 stats"
  "$PY" -m ideas --db "$DB" stats
  ok "demo done — ./run.sh gap --top 10   or   ./run.sh dashboard"
}

# ------------------------------------------------------------------- all ----
# `./run.sh` with no arguments. Runs the whole thing, start to finish, and
# skips any stage whose output is already in the database — so it is safe to
# re-run, and a second run goes straight to the dashboard.

# Count rows matching a WHERE clause; 0 when the database or table is absent.
# Written in Python because the venv is guaranteed and sqlite3(1) is not.
db_count() {
  [ -f "$DB" ] || { echo 0; return; }
  "$PY" - "$DB" "$1" "$2" <<'DBCOUNT' 2>/dev/null || echo 0
import sqlite3, sys
db, table, where = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone():
        print(0)
    else:
        print(c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])
except Exception:
    print(0)
DBCOUNT
}

skip() { printf '%sskip%s  %s\n' "$Y" "$N" "$*"; }

cmd_all() {
  local force=0 want_dash=1
  for a in "$@"; do
    case "$a" in
      -f|--force)     force=1 ;;
      --no-dashboard) want_dash=0 ;;
      *) die "all: unknown option $a (use --force / --no-dashboard)" ;;
    esac
  done
  [ "${FORCE:-0}" = "1" ] && force=1

  local hack_src="${SCRAPE_SOURCE:-devfolio}"
  local mine_src="${MINE_SOURCE:-hn}"
  local max_hacks="${MAX_HACKATHONS:-10}"
  local since="${SINCE:-365d}"
  local limit="${MINE_LIMIT:-500}"

  # --- 1. setup -------------------------------------------------------------
  if [ "$force" = 0 ] && have_venv && "$PY" - <<'DEPCHECK' >/dev/null 2>&1
import bs4, dateutil, flask, lxml, requests, tenacity, tqdm  # noqa: F401
DEPCHECK
  then
    skip "1/6 setup    — .venv already has every requirement"
  else
    info "1/6 setup    — virtualenv + requirements.txt"
    cmd_setup
  fi
  ensure_venv
  load_env
  mkdir -p data

  # --- 2. scrape ------------------------------------------------------------
  local n_projects; n_projects="$(db_count projects 1)"
  if [ "$force" = 0 ] && [ "$n_projects" -gt 0 ]; then
    skip "2/6 scrape   — $n_projects projects already stored"
  else
    info "2/6 scrape   — winning projects from $hack_src"
    "$PY" -m ideas --db "$DB" scrape --source $hack_src --max-hackathons "$max_hacks"
  fi

  # --- 3. mine --------------------------------------------------------------
  local n_problems; n_problems="$(db_count problems 1)"
  if [ "$force" = 0 ] && [ "$n_problems" -gt 0 ]; then
    skip "3/6 mine     — $n_problems pain points already stored"
  else
    info "3/6 mine     — pain points from $mine_src (since $since, limit $limit)"
    "$PY" -m ideas --db "$DB" mine --source $mine_src --since "$since" --limit "$limit"
    n_problems="$(db_count problems 1)"
  fi

  # --- 4. extract (paid, opt-in) -------------------------------------------
  # The only stage that spends money, so it stays off unless asked for.
  local n_judged; n_judged="$(db_count problems "extracted_by IS NOT NULL AND extracted_by != ''")"
  if [ "${EXTRACT:-0}" != "1" ]; then
    skip "4/6 extract  — LLM pass off by default (costs money); enable with EXTRACT=1 ./run.sh"
  elif [ -z "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    warn "4/6 extract  — EXTRACT=1 but no ANTHROPIC_API_KEY; skipping"
  elif [ "$n_problems" -eq 0 ]; then
    skip "4/6 extract  — nothing mined to judge"
  elif [ "$force" = 0 ] && [ "$n_judged" -gt 0 ]; then
    skip "4/6 extract  — $n_judged rows already judged"
  else
    info "4/6 extract  — LLM judge pass"
    "$PY" -m ideas --db "$DB" extract --limit "${EXTRACT_LIMIT:-100}"
  fi

  # --- 5. cluster -----------------------------------------------------------
  local n_clustered; n_clustered="$(db_count problems "cluster_id IS NOT NULL AND cluster_id != ''")"
  if [ "$n_problems" -eq 0 ]; then
    skip "5/6 cluster  — no pain points to cluster"
  elif [ "$force" = 0 ] && [ "$n_clustered" -gt 0 ]; then
    skip "5/6 cluster  — $n_clustered rows already clustered"
  else
    info "5/6 cluster  — merge duplicate complaints"
    "$PY" -m ideas --db "$DB" cluster
  fi

  # --- 6. gap ---------------------------------------------------------------
  # Read-only report, always regenerated: it is cheap and it is the payoff.
  info "6/6 gap      — pains no project attacks"
  "$PY" -m ideas --db "$DB" gap --top "${GAP_TOP:-15}" || true

  # --- dashboard ------------------------------------------------------------
  if [ "$want_dash" = 0 ]; then
    ok "done — ./run.sh dashboard to browse it"
    return 0
  fi
  ok "pipeline complete"
  cmd_dashboard
}

# ----------------------------------------------------------------- clean ----
cmd_clean() {
  rm -rf data/.cache
  ok "removed data/.cache"
  find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
  ok "removed __pycache__"
  if [ "${1:-}" = "--all" ]; then
    rm -f "$DB" data/*.log
    ok "removed $DB and data/*.log"
  fi
}

usage() {
  # Print the header comment block: every line after the shebang up to the
  # first line that is not a comment.
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$ROOT/run.sh"
}

# ------------------------------------------------------------------ main ----
case "${1:-all}" in
  # `shift` fails when there are no positional parameters at all, and `set -e`
  # would kill the script on it — so guard it for the bare `./run.sh` case.
  all|"")                   [ $# -gt 0 ] && shift || true; cmd_all "$@" ;;
  # Bare options with no subcommand mean `all` too: ./run.sh --no-dashboard
  -f|--force|--no-dashboard) cmd_all "$@" ;;
  setup)                    shift; cmd_setup "$@" ;;
  doctor|check)             shift; cmd_doctor "$@" ;;
  scrape)                   shift; run_stage scrape "$@" ;;
  mine)                     shift; run_stage mine "$@" ;;
  extract)                  shift; run_stage extract "$@" ;;
  cluster)                  shift; run_stage cluster "$@" ;;
  gap)                      shift; run_stage gap "$@" ;;
  problems)                 shift; run_stage problems "$@" ;;
  search)                   shift; run_stage search "$@" ;;
  export)                   shift; run_stage export "$@" ;;
  stats)                    shift; run_stage stats "$@" ;;
  list-hackathons)          shift; run_stage list-hackathons "$@" ;;
  dashboard|serve|web)      shift; cmd_dashboard "$@" ;;
  pipeline)                 shift; cmd_pipeline "$@" ;;
  demo)                     shift; cmd_demo "$@" ;;
  py)                       shift; run_py "$@" ;;
  python)                   shift; ensure_venv; load_env; exec "$PY" "$@" ;;
  clean)                    shift; cmd_clean "$@" ;;
  help|-h|--help)           usage ;;
  *) printf '%sunknown command: %s%s\n\n' "$R" "$1" "$N" >&2; usage; exit 2 ;;
esac
