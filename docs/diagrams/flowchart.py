from pathlib import Path

from flow_drawio import Flow, B, L, R, T

COL_PITCH = 520

f = Flow("Run.sh skip / force logic", "cmd_all: skip if DB present, else run; --force redoes")

start = f.start("Run ./run.sh [all]", col=0, row=0)

setup_check = f.decision(".venv + deps OK?", col=0, row=2)
f.branch(setup_check, yes=f.process("Skip setup", col=0, row=4), no=f.process("Run setup", col=1, row=4), yes_label="Yes · skip", no_label="No · build")

scrape_check = f.decision("projects > 0 and !--force?", col=0, row=6)
run_scrape = f.process("Run scrape", col=1, row=6)
skip_scrape = f.process("Skip scrape", col=0, row=8)
f.branch(scrape_check, yes=skip_scrape, no=run_scrape, yes_label="Yes · skip", no_label="No · run")

mine_check = f.decision("problems > 0 and !--force?", col=0, row=9)
run_mine = f.process("Run mine", col=1, row=9)
skip_mine = f.process("Skip mine", col=0, row=11)
f.branch(mine_check, yes=skip_mine, no=run_mine, yes_label="Yes · skip", no_label="No · run")

extract_check = f.decision("EXTRACT=1 + key + problems exist?", col=2, row=6)
run_extract = f.process("Run extract (LLM)", col=3, row=6)
skip_extract = f.process("Skip extract", col=2, row=8)
f.branch(extract_check, yes=run_extract, no=skip_extract, yes_label="Yes · run", no_label="No · skip")

cluster_check = f.decision("problems > 0 + cluster needed?", col=2, row=9)
run_cluster = f.process("Run cluster", col=3, row=9)
skip_cluster = f.process("Skip cluster", col=2, row=11)
f.branch(cluster_check, yes=run_cluster, no=skip_cluster, yes_label="Yes · run", no_label="No · skip")

gap_run = f.process("Always run gap", col=2, row=12)

end_all = f.terminal("Pipeline complete → dashboard (unless --no-dashboard)", col=2, row=14)

f.then(start, setup_check)
f.to(setup_check, skip_scrape, label="skip path", exit=B(), entry=T(), corridor=f.corridor(after_col=0))
f.then(skip_scrape, mine_check)
f.to(run_scrape, mine_check, exit=B(), entry=T(), corridor=f.corridor(after_col=0))

# Returns from no branches
f.to(run_scrape, mine_check, exit=B(), entry=T(), corridor=f.corridor(after_col=0))
f.to(run_mine, cluster_check, exit=B(), entry=T(), corridor=f.corridor(after_col=1))

# Extract branch rejoins to cluster
f.to(run_extract, cluster_check, exit=L(), entry=R(), corridor=f.corridor(after_col=2))
f.to(skip_extract, cluster_check, exit=L(), entry=R(), corridor=f.corridor(after_col=2))

f.to(run_cluster, gap_run, exit=B(), entry=T(), corridor=f.corridor(after_col=2))
f.to(skip_cluster, gap_run, exit=B(), entry=T(), corridor=f.corridor(after_col=2))
f.then(gap_run, end_all)

f.write(str(Path(__file__).with_name("pipeline_flowchart.drawio")))
