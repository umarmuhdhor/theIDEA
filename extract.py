"""LLM pass over mined pain points: rewrite the statement, judge, classify.

The regex pass in `scrapers/social/signals.py` is a *recall* device — it finds
posts that smell like an unmet need and lifts out the sentence carrying the
strongest phrase. That sentence is raw human speech: `"No more scheduling by
hand."` is a real signal but a useless problem statement.

This pass fixes that, and does the one thing regex fundamentally cannot: decide
whether the post is a problem at all. A launch announcement bragging about the
"workaround" it replaces trips every keyword and is not a pain point.

It only ever reads what the regex pass kept, which is why it is affordable:
~1% of the firehose, batched, cached by post key so a re-run costs nothing.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

from scrapers.social.signals import impact

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
BATCH = 20

SYSTEM = """You are a product researcher mining public forum posts for unmet needs.

For each post you receive, decide whether it states a real problem somebody has,
then describe that problem.

Rules:
- Work only from the text. Never invent a detail the post does not contain.
- `is_problem` is false for: product launches and self-promotion, general
  opinion or debate, news commentary, jokes, and questions about how to use a
  tool that already exists. It is true only when somebody is describing
  friction, waste, or a gap in what exists.
- `problem_statement`: one sentence, under 25 words, in the form "<who> cannot
  <do what> because <why>" or "<who> wastes <effort> on <task>". No product
  pitch, no solution.
- `who`: the role that suffers, 1-4 words ("restaurant owner", "solo dev").
  Empty string if the post never says.
- `domain`: one of fintech, health, education, devtools, data-ai, ecommerce,
  logistics, hr-ops, legal, marketing, climate, agritech, real-estate,
  security, productivity, other.
- `severity`: 1 = mild annoyance, 3 = costs real time or money weekly,
  5 = blocks the person's work or is expensive every single day.

Return one object per post, with the same `id` you were given."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "is_problem": {"type": "boolean"},
                    "problem_statement": {"type": "string"},
                    "who": {"type": "string"},
                    "domain": {"type": "string"},
                    "severity": {"type": "integer"},
                },
                "required": ["id", "is_problem", "problem_statement", "who",
                             "domain", "severity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def render_batch(rows: Iterable[Any]) -> str:
    """One prompt block per post. Truncation is per-post and generous."""
    parts = []
    for r in rows:
        body = (r["raw_text"] or r["problem_statement"] or "")[:1500]
        title = (r["title"] or "").strip()
        # `raw_text` is built as "title. text", so repeating the title would
        # spend tokens on saying the same thing twice.
        head = f"{title}\n" if title and not body.startswith(title[:40]) else ""
        parts.append(
            f'<post id="{r["key"]}" channel="{r["channel"]}">\n{head}{body}\n</post>'
        )
    return "\n\n".join(parts)


def _client():
    import anthropic

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # A stored `ant auth login` profile also works, so this is a warning.
        log.warning("no ANTHROPIC_API_KEY set - relying on a stored credential profile")
    return anthropic.Anthropic()


def call_model(client, rows: list, model: str) -> list[dict]:
    """One API call for one batch. Returns the raw item dicts."""
    prompt = render_batch(rows)
    kwargs = dict(
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        resp = client.messages.create(
            **kwargs, output_config={"format": {"type": "json_schema", "schema": SCHEMA}}
        )
    except Exception as e:
        # Older / smaller models may not accept output_config; fall back to
        # asking for bare JSON rather than losing the batch.
        if "output_config" not in str(e) and "format" not in str(e):
            raise
        log.warning("structured output rejected (%s); retrying as plain JSON", e)
        resp = client.messages.create(
            **{**kwargs, "system": SYSTEM + "\n\nReply with JSON only, no prose."}
        )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _parse(text)


def _parse(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):                     # fenced JSON from the fallback path
        text = text.split("```")[1].lstrip("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            log.error("model returned unparseable output: %.200s", text)
            return []
        data = json.loads(text[start:end + 1])
    return data.get("items") or []


def run(
    store,
    limit: int = 100,
    batch_size: int = BATCH,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> dict[str, int]:
    rows = store.unextracted(limit=limit, model=model)
    if not rows:
        return {"seen": 0, "updated": 0, "rejected": 0}

    if dry_run:
        print(f"--- would send {len(rows)} rows to {model} in "
              f"{(len(rows) + batch_size - 1) // batch_size} batch(es)\n")
        print(SYSTEM)
        print("\n--- first batch ---\n")
        print(render_batch(rows[:batch_size]))
        return {"seen": len(rows), "updated": 0, "rejected": 0}

    client = _client()
    by_key = {r["key"]: r for r in rows}
    updated = rejected = 0

    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        try:
            items = call_model(client, chunk, model)
        except Exception as e:
            log.error("batch %d failed: %s", i // batch_size + 1, e)
            continue

        updates = {}
        for it in items:
            key = str(it.get("id", ""))
            row = by_key.get(key)
            if row is None:                      # hallucinated id
                log.warning("model returned unknown id %s", key)
                continue
            sev = max(0, min(5, int(it.get("severity") or 0)))
            ok = bool(it.get("is_problem", True))
            if not ok:
                rejected += 1
            updates[key] = {
                "problem_statement": (it.get("problem_statement") or "").strip(),
                "who": (it.get("who") or "").strip(),
                "domain": (it.get("domain") or "").strip(),
                "severity": sev,
                "is_problem": ok,
                "extracted_by": model,
                "impact": impact(row["score"], 1, sev) if ok else 0.0,
            }
        updated += store.apply_extraction(updates)
        log.info("batch %d: %d/%d judged", i // batch_size + 1, len(updates), len(chunk))

    return {"seen": len(rows), "updated": updated, "rejected": rejected}
