# Plan: two audit modes (quick `survey` of a whole lab, then `deep` per scope)

> **Status: implemented, cluster verification pending** (2026-10-05). Code and unit tests are done (Verification step 1). Steps 2–6 need compute nodes; all partitions were down for maintenance. Implementation choices are in `docs/decisions.md` (2026-10-05). Linked from `docs/plan.md` Open items.

## Context
- **The current pipeline suits project and user dirs.** On a large project dir (multi-TiB, millions of files) it ran end to end in under half an hour, but the report phase used most of the agent tokens.
- **A whole lab is an order of magnitude bigger** (tens of millions of files) and would break it in three places:
  1. **Scan imbalance.** Shards are cut blind at depth 2, so one giant subtree runs alone (in the trial, most of the scan's wall time). At lab scale `Lab/projects` would be a single multi-hour shard.
  2. **Token cost.** `summary.json` was too large to read whole, even for one project.
  3. **Post at lab scale.** A multi-GB SQLite database, plus in-memory sorts and lists.
- Measured rates and sizes are in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
- **The user wants a quick triage mode** that maps the whole lab and recommends what to audit in depth, then the existing deep audit run on the chosen scopes.

**Decisions (user, 2026-10-02):**
- Survey reads metadata plus big-file duplicates (≥ 256 MiB). That threshold is arbitrary; flag it for follow-up and calibrate it from data.
- A deep audit re-scans its scope; it does not reuse the survey database.
- Split shards on the fly.
- Cap total concurrent walkers at 16, and give FASRC a heads-up before the first whole-lab run.

## Design
- **`survey` mode is the same pipeline with different settings.**
  - `plan.py --mode survey` (depth 3).
  - The scan is unchanged apart from splitting.
  - Post runs `merge,dupes,rollup,classify,summarize`: no peek, dupes `--min-size 256MiB`.
  - A compact summary goes to a new short survey report that ends in **recommended deep audits** (scope, size, files, newest, estimated time and tokens).
- **`deep` mode is today's pipeline on one scope** (`--only`), and benefits from splitting and the v2 summary.
- **Splitting on the fly (scan.py).** A tree shard that has walked ≥ `--split-files` files (default 250K, ~50 s) stops. It hands its not-yet-entered subdirs back as child shards `<sid>.<i>` and lists them in its `.done`. The task's pool submits children as parents finish. A crashed parent re-walks from scratch, and children exist only once the parent's `.done` exists, so requeue stays idempotent.

## Changes (all under `.claude/skills/lab-storage-audit/` unless noted)
- **`scripts/scan.py`**
  - `scan_shard(...)`: string shard id, a list of roots, and the split check at each stack pop.
  - New `expand_shards(run_dir)`: walks `.done` → `children`.
  - `run_task`: `wait(FIRST_COMPLETED)` loop that submits children; children stay in their parent's task, routed by the top-level id `% num_tasks`.
  - New `--split-files` flag.
- **`scripts/plan.py`**
  - `--mode survey|deep` (default deep), stored in `run.json` as `mode` and read with `.get`, so no schema bump.
  - Survey sets depth 3.
  - Print a one-line summary at the end; the survey JSON goes to `run.json` only.
- **`scripts/post.py`**
  - `MODE_DEFAULTS` per `run["mode"]`; explicit flags override.
  - `merge` and `unfinished_shards` use `expand_shards`; `dirs.shard` becomes TEXT.
  - Pragmas `temp_store=FILE`, larger `cache_size`.
  - Check free space on the workdir before merge (database ≈ 16 × gz shard size).
- **`scripts/post.sbatch`**: `export SQLITE_TMPDIR=$WORK`. Usage lines show `<partition>` instead of `test`.
- **`scripts/dupes.py`**
  - Stream the candidates instead of `fetchall`.
  - Bounded `ex.map(..., buffersize=…)` in `_pool_map`; batch the hash inserts.
  - Smaller `MAX_PAIR_DIRS` in survey.
- **`scripts/summary.py`: `SUMMARY_VERSION = 2`, compact, target ≤ ~15K tokens.**
  - `{cols, rows}` tables, no indent (needs a `write_json_atomic` option in `common.py`), dates as `YYYY-MM-DD`.
  - `problem_dirs` grouped by (parent, status): count plus 2 examples, ≤ 30 groups.
  - Folders as one row each, up to a `folder_depth` (survey: target + 3), chosen by bytes **or** files ≥ 0.1%; top-3 categories and top owner per row.
  - Markers grouped by type: count plus top 5.
  - Raw locations rolled up, top 25; `top_groups` and `dir_overlap` 10 each; `unknown_exts` 25.
  - Dup-group outside-pipeline-work numbers computed in SQL.
  - Sensitive count split into `{in_code_env, elsewhere, top_dirs_elsewhere}`. The scan's sensitive flag itself is unchanged.
  - **File-size histogram** (bytes and files per size decade) as data for calibrating the 256 MiB threshold.
  - In survey: omit `dir_trees`, because files under the threshold aren't hashed, so trees could match on names and sizes alone.
  - Log the output size; warn above 50 KB.
- **`scripts/peek.py` `select`**: split the budget, about 60% unknown and 40% known types with raw/processed first, so raw files get sampled.
- **`SKILL.md`**
  - `/lab-storage-audit [survey|deep] TARGET`. Survey is the default for a lab root, or when `quota` shows more than ~5M files.
  - A sizing table from measured rates (walk, post and database size per million files; see the local dev notes).
  - Cap tasks × workers at ≤ 16.
  - Drop the redundant `lfs quota` step.
- **`references/survey-template.md`** (new, short): findings, scope, storage vs quota and unaccounted bytes, areas, users, age, big duplicates, grouped unreadable dirs, recommended deep audits, questions. Update `report-template.md` to summary v2.
- **Repo docs**
  - `SAFETY.md`: what survey mode reads (metadata plus 3 MiB slices of files ≥ 256 MiB, no peeks), and that the survey database (~15 GB) is stored in `audits/`.
  - `docs/spec.md`: modes and child shards.
  - `docs/decisions.md`: rows for the 4 decisions above.
  - `docs/plan.md`: Status and Open items, including "calibrate 256 MiB".
- **`tests/`**
  - Splitting with `split_files=2` on the `lab` fixture: child shards appear; totals and dir paths match an unsplit run; a resume after a simulated crash gives no duplicate rows.
  - Survey defaults are applied.
  - Summary v2 shape and size budget.
  - Peek budget split; sensitive breakdown.

## Verification
1. `pytest tests/` passes.
2. **Deep regression on `Lab/projects/<project-C>`** (`holy-smokes`, 8 walkers):
   - scan wall time much closer to the sum of shard times divided by workers
   - same entry count, with sorted dir paths identical to the step 4 trial run
   - `summary.json` ≤ 50 KB
   - report subagent tokens roughly halved versus the step 4 trial
3. **Whole-lab survey** of `/n/holylfs05/LABS/informatics` (≤ 16 walkers, after the FASRC heads-up):
   - record per-shard and child counts, scan and post time, post peak memory and `/scratch` high-water mark
   - scanned bytes plus unaccounted bytes reconcile with `quota`
   - the 5 unreadable `Users/*` dirs appear as one group
   - the report lists recommended deep audits with estimates
4. **Requeue test:** requeue the scan mid-run. Expect identical totals and no duplicate rows.
5. **Deep audit of the top recommended scope:** compare actual time and tokens with the survey's estimate.
6. **Read-only check:** rescan-and-diff one survey shard (`docs/development.md`).

## Follow-ups (not in this change)
- Calibrate the big-file threshold from the survey's size histogram.
- Reuse the survey database for deep audits, only if re-scans prove costly.
- An `lfs find --lazy` walker, if the stat rate becomes the bottleneck.
