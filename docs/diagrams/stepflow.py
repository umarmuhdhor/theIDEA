from pathlib import Path

from step_drawio import Steps, L, R, T, B

s = Steps("Hackathon Idea Pipeline", "run.sh stages: ordered sequence")

setup = s.node("Setup", ".venv + requirements + data dir", col=0, row=0)
scrape = s.node("Scrape", "winning projects → DB (projects)", col=1, row=0)
mine = s.node("Mine", "pain points → DB (problems)", col=2, row=0)
extract = s.node("Extract", "LLM judge (optional, costs $)", col=3, row=0)
cluster = s.node("Cluster", "dedupe complaints → cluster_id", col=4, row=0)
gap = s.node("Gap", "pains with no coverage → top N", col=5, row=0)
dash = s.node("Dashboard", "Flask UI port 5050", col=6, row=0)

s.step(setup, scrape, exit=R(), entry=L(), direct=True)
s.step(scrape, mine, exit=R(), entry=L(), direct=True)
s.step(mine, extract, exit=R(), entry=L(), direct=True)
s.step(extract, cluster, exit=R(), entry=L(), direct=True)
s.step(cluster, gap, exit=R(), entry=L(), direct=True)
s.step(gap, dash, exit=R(), entry=L(), direct=True)

s.note("Pipeline: scrape → mine → cluster → gap (no LLM)", col=2.5, row=2)
s.note("run.sh all skips stages with DB output already present", col=2.5, row=2.5)

s.write(str(Path(__file__).with_name("pipeline_stepflow.drawio")))
