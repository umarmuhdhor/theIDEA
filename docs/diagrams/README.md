# Diagrams

Two views of the same pipeline:

| File | What it shows |
|---|---|
| [`pipeline_stepflow.svg`](pipeline_stepflow.svg) | the stage order: setup → scrape → mine → extract → cluster → gap → dashboard |
| [`pipeline_flowchart.svg`](pipeline_flowchart.svg) | `run.sh all` decision logic: which stages are skipped, and what `--force` overrides |

Each diagram is three files:

- `*.py` — the source the diagram is generated from
- `*.drawio` — the generated diagram, editable at [app.diagrams.net](https://app.diagrams.net)
- `*.svg` — the rendered preview, which is what the README links to

## Regenerating

The `.py` scripts import `flow_drawio` / `step_drawio`, which are **not part of
this project** — they ship with the `drawing-flowcharts` and `drawing-step-flows`
Claude Code skills and live under `~/.claude/skills/`. Without those on
`PYTHONPATH` the scripts raise `ModuleNotFoundError`, so a fresh clone can read
and edit the diagrams but cannot rebuild them from the Python source.

Edit the `.drawio` files directly if you do not have the skills installed.
