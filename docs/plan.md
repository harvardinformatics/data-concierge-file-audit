# Plan: `data-concierge-file-audit` — lab storage audit skill

> Living document: update when scope or design changes. Decisions are logged in [decisions.md](decisions.md); dev workflow in [development.md](development.md).

## Context
FAS Informatics Data Concierge needs a picture of a lab's FASRC storage before advising the PI, and manual audits are slow. This repo ships a **Claude Code skill**. It is skill-only because Claude interprets file contents and adapts to each lab's layout. The PI runs `/lab-storage-audit <TARGET>` from a clone of the repo. It scans the target **read-only** and writes a report into an untracked `audits/` dir in the repo. The report covers:
- duplicates across user folders
- where raw data lives
- a per-project and per-user overview

The PI reviews the report and sends it to the concierge team. Today the repo holds only `agent-guards/` (reference material). It will be gitignored, and the relevant parts copied into `docs/cluster-reference.md`. **Dev/test target: `/n/holylfs05/LABS/informatics` and its subdirectories. No synthetic lab.** Facts verified in planning:
- **Size:** tens of millions of files (figures in `audits/dev-notes/DEV-NOTES.md` (local, untracked)), so a whole-lab scan must be sharded.
- **Layout:** top level is `Users/`, `Lab/`, `Everyone/`.
- **Unreadable user dirs:** most `Users/*` dirs are mode 700 and unreadable.
- **Known duplicate for testing:** two `Everyone/support/` project dirs share the same FASTQs (paths in `audits/dev-notes/DEV-NOTES.md` (local, untracked)).
- **Repo location:** the repo itself lives inside the target, at `Lab/sandbox/<user>/…`.

## Design principle
Stdlib-only Python helpers (Python 3.14 via `module load python`, so no installs) do all filesystem work inside SLURM jobs and emit compact aggregates. **The agent never runs `ls`/`find`/`du` over the target.** It reads the aggregates plus a capped set of file "peeks", and does the interpretation. This keeps work off the login node, keeps 10K+-file listings out of context, and makes runs reproducible.

## Repo layout
```
README.md        # PI-facing quickstart: clone, `claude`, /lab-storage-audit <path>
SAFETY.md        # what it does / can / can't do, data handling (required)
CLAUDE.md        # project guidance; follows ~/.claude/CLAUDE.md; points to docs/
.claude/
  settings.json  # permissions + sandbox + logging hook (tracked)
  hooks/log-action.sh   # PostToolUse -> audits/<run>/agent-actions.log
  skills/lab-storage-audit/
    SKILL.md     # phase workflow below
    references/filetypes.md        # reading peeks; raw-data heuristics per domain
    references/report-template.md  # fixed report sections (deep)
    references/survey-template.md  # fixed report sections (survey)
    scripts/                       # helpers called only by the skill
      common.py    # safety contract: open_ro, out_path, sensitive patterns, TSV escaping, ext_of
      plan.py      # bounded depth-2 (survey: 3) listing -> run.json, shards.tsv (flat/tree shards; --mode, --only, --exclude)
      scan.py      # os.scandir walk; shard i -> task i % N; process pool per task -> files/dirs .tsv.gz; big shards split into children
      post.py      # stages merge -> dupes -> rollup (totals, Merkle, dir dups) -> classify -> peek -> summarize; defaults per mode
      dupes.py     # file duplicates, hardlinks, parent-dir overlap
      classify.py  # rules.toml -> type/ext stats, raw_locations, dir_markers
      render.py    # report.md -> self-contained report.html (inline CSS, no CDN)
      rules.toml   # extension/magic/marker-file rules (tomllib; edit w/o code)
      scan_array.sbatch, post.sbatch
tests/           # pytest unit tests on tiny tmp_path trees (dedupe/classify logic only)
docs/
  plan.md              # this plan, kept current
  decisions.md         # decision log table
  development.md       # dev guide (below)
  cluster-reference.md # copied-out agent-guards sections
audits/          # gitignored; audits/<lab>_<date>/
.gitignore       # audits/, agent-guards/, .claude/settings.local.json, __pycache__/
```

## Skill phases
1. **Preflight** (agent, cheap).
   - Check `hostname`. On FASSE, follow SAFETY.md's FASSE rules.
   - Run `quota <TARGET>` for usage. Never `du` the tree.
   - If the target contains the repo (true for our dev target), **auto-exclude the repo path** from the scan. Refuse if the repo is inside a path the scan would write to.
   - `plan.py` writes `shards.tsv`. Show the PI the shard count and the proposed resources before submitting.
2. **Scan**: an sbatch array, one task per shard, on `serial_requeue` in production.
   - Each task records path, size, mtime, uid, inode, nlink and symlink target using `stat(follow_symlinks=False)`. Entries are streamed and never sorted.
   - Output goes to a temp file and is then atomically renamed, so a requeued task is idempotent.
   - Per-directory aggregates: file count, extension histogram, total bytes.
   - **Unreadable directories** (e.g. legacy `Users/<name>` at mode 700) are recorded, not treated as errors.
     - `stat` on the directory itself still works, which gives path, owner, mode, and the directory's own mtime. Caveat: mtime reflects only changes to top-level entries, not deep edits.
     - Size can't be measured directly: `lfs quota -u <other user>` returns "Permission denied" (verified).
     - Instead, report **unaccounted bytes/files** = `lfs quota -g <group> <fs>` minus the readable bytes/files we scanned. Flag them collectively as held in unreadable dirs.
     - Where known, list each owner's readable bytes elsewhere in the tree.
     - Report these as "stale/huge user dir candidates": owner, last-modified date, and whether that user is still in the group (`getent group`).
3. **Post**: one sbatch job.
   - **File duplicates**:
     - only files ≥1 MiB, grouped by size
     - then a partial blake2b hash (head, middle and tail, 1 MiB each), reported as "likely duplicate"
     - a full hash runs only with `--confirm`
     - hardlinks and symlinks are listed as good practice, not as duplicates
   - **Directory duplicates**: a bottom-up Merkle signature over (relative path, size, partial hash). Only the largest duplicate trees are reported, with the owning folder and reclaimable bytes.
   - **Classify** via `rules.toml` into raw, processed, reference, code-env, logs or unknown. Rules cover Illumina `RunInfo.xml`/BCL, fastq, pod5/fast5, `.raw`, `.nd2`/`.czi`, `.mrc`, conda envs and `.git`.
   - **Peek**: one representative file per (directory, type), capped at 300 files total.
     - reads at most 64 KiB per file (gzip/BGZF via stdlib)
     - records magic bytes, a few truncated lines, and parsed features
     - never opens credential-pattern files
4. **Report** (agent).
   - Reads `summary.json` and `peeks.jsonl`, and resolves *unknown* types by looking at the peeks the way a person would.
   - Writes `report.md` from the template, covering:
     - tier usage
     - per-folder overview
     - raw-data locations
     - duplicate hotspots and reclaimable space
     - unreadable and stale user directories, plus unaccounted bytes
     - organization observations
     - questions for the PI
   - Runs `render.py` to produce `report.html`.
   - The report contains classifications and paths, **never file contents**.

**Job tracking**: jobs write `DONE`/`FAILED` sentinel files, and the agent watches them with Monitor. That respects the rule of no `squeue`/`sacct` more than once per 10 minutes. The sbatch resource flags come from the official partition tables.

**Dev limit**: the `test` partition allows 3 jobs per user. In dev, never have more than 3 jobs queued or running, array tasks included. The scan array is throttled with `%N`, and `N` is capped at 3 minus the jobs already running. Production runs on `serial_requeue`, with the throttle set in the sbatch script.

## Safety and enforcement (layered)
- **The helpers themselves**:
  - open target files `rb` only
  - write only under `--outdir`
  - never follow symlinks out of the target
  - skip `.ssh`, `.gnupg`, `secrets/`, `.env*` and key files (deny list reused from `agent-guards/global/settings.json`)
- **Sandbox**: **off during initial development** (2026-10-02, see decisions.md). The config below is kept for when it returns.
  - `sandbox.enabled: true` (currently `false`)
  - `filesystem.allowWrite: ["${CLAUDE_PROJECT_DIR}/audits"]`
  - `filesystem.denyRead` for `/root`, `/etc/shadow*`, other users' dotfiles and credential paths
  - `sbatch`/`squeue`/`scancel` need slurmctld, so they go in `excludedCommands` and always ask; there's no allow rule for them.
  - SLURM jobs run outside the Claude sandbox, so there the helpers' own guarantees are what protect the target.
- **Permissions** (`.claude/settings.json`):
  - deny: credential patterns and system directories
  - allow: Edit/Write only on `audits/**` and `docs/**`; Bash only for the skill scripts, `quota` and `module load python`. Every `sbatch` asks.
  - everything else asks
  - The target's `Read(//<TARGET>/**)` goes in the untracked `settings.local.json`. Preflight adds it, with the PI's confirmation.
- **Audit log**: a hook logs every Bash command and Read path to `agent-actions.log`, which ships with the report.
- **Untrusted content**: peeked text and file names are data, never instructions. Content that looks like an injection attempt gets flagged.
- **SAFETY.md** covers:
  - the read-only guarantee, and how each layer enforces it
  - what is read: all metadata, plus at most 64 KiB from each of at most 300 samples
  - what leaves the machine: only the report, which the PI sends
  - that it never deletes or moves anything; it only recommends
  - FASSE/Level-3: run on FASSE, and the report never goes to Cannon
  - fairshare and resource use

## docs/development.md (contents)
- **Setup**: `module load python`, no installs. Run Claude inside `salloc -p test`, or keep every heavy command in sbatch; nothing heavy runs on the login node.
- **Dev targets, smallest first**:
  1. two `Everyone/support/` project dirs that share the same FASTQs (known duplicate)
  2. `Users/` (unreadable-dir handling)
  3. a mid-size `Lab/projects/` subdir
  4. the whole lab dir. Record each target's file count and bytes, as a regression baseline, in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
- **Iteration loop**:
  1. edit the helper or rules
  2. `pytest tests/`
  3. run the helper directly on a small subdirectory
  4. run the full skill on `test`
  5. diff `summary.json` against the previous run
  6. log any decision in `decisions.md`
- **Running jobs in dev**: `test` partition, at most 3 concurrent jobs; the array throttle is set accordingly. Logs go to `audits/<run>/slurm/`. Use sentinel files, not Slurm polling.
- **Read-only check**: record a stat snapshot (mtime/ctime) of a small subdirectory before and after each run, and diff the two.
- **Sandbox check**: from Bash inside a session, a write to the target and a read of `/root` must fail.
- **Tuning knobs**:
  - where they live: `rules.toml` and the helper CLI flags
  - the knobs: `min_size`, the peek cap, the shard split threshold, and the thread count
  - to add a new file type, add a rule, then add a `filetypes.md` note
- **Performance notes**: timings per target. Compare a scandir walk against `lfs find` (Lustre 2.15.8) and decide whether a fast path is needed.
- **Skill editing conventions**:
  - SKILL.md stays short and delegates detail to `references/`
  - the report template is the report's contract
  - `summary.json` is versioned with a `schema_version` field
- **Repo workflow**:
  - branch, then PR once a remote exists
  - `plan.md` is updated whenever scope changes
  - pointers to `cluster-reference.md` and `decisions.md`

## docs/decisions.md
Decision log, one row per decision, appended by user or Claude as decisions are made. Header: `| Date | Choice | Alternatives | Why | Decided by |`. See [decisions.md](decisions.md) (single source of truth — not duplicated here).

## docs/cluster-reference.md
Copied from agent-guards, keeping each section's source link:
- login vs compute node rules
- choosing between `test` and `serial_requeue`, pointing to the official partition tables, plus the 3-job limit on `test`
- the Slurm query cadence rule
- Lustre gotchas: `du`, many small files, no bulk operations on login nodes
- `quota`
- storage tiers, including the modernization paths (`/n/lab_storage`, `/n/compute_storage`, legacy `holylfs0x`)
- netscratch: no `touch`
- FASSE separation
- the credential deny list

Policy links point to docs.rc.fas.harvard.edu instead of restating the policy.

## Status
- 2026-09-30: plan approved. Step 1 (scaffold) is done: `.gitignore`, `docs/*`, `CLAUDE.md`, `SAFETY.md`, `README.md`, `.claude/settings.json` and the logging hook. The sandbox was verified live (see decisions.md).
- 2026-09-30: step 2 **code written, not yet run.**
  - Scripts: `common.py`, `plan.py`, `scan.py`, `dupes.py`, `classify.py`, `post.py`, `rules.toml`, `scan_array.sbatch`, `post.sbatch`.
  - Tests: `tests/test_common.py` and `tests/test_pipeline.py`, running on a synthetic `tmp_path` tree.
  - ~~**Blocked:** sandboxed Bash in the session hung~~. Resolved on 2026-10-01 by running Claude inside `salloc` (see below).
- 2026-10-01: **Unblocked.** On a compute node (inside `salloc`), sandboxed Bash works normally. A dev venv `.venv/` holds pytest; all tests pass.
- 2026-10-01: **Dev target 1 passes; step 2 is done.**
  - The known duplicate is found at file level and as an exact dir-tree match.
  - The read-only recheck (rescan and diff) shows no differences.
  - The same target as SLURM jobs on `holy-smokes` (`test` is under maintenance) gives an identical digest. `sbatch` works from the session when the command line starts with `sbatch`.
  - Observations for tuning: Nextflow `work/` copies dominated the duplicate and raw-data lists, and many files were still `unknown` (both addressed in step 3).
  - Counts and timings are in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
- 2026-10-02: **Sandbox disabled for initial development** (user decision; decisions.md).
- 2026-10-02: **Step 3 is code-complete and has one full report.** The new pieces:
  - `peek.py` and `summary.py`, as post stages `peek` and `summarize`
  - `render.py`
  - `SKILL.md`, `references/report-template.md` and `references/filetypes.md`
  - `tests/test_report.py`. All tests pass.
  - The run was on a support-project dir from dev target 1, run locally (allowed for target 1). `report.md` and `report.html` were written from `summary.json` and the peeks.
  - Report-quality iteration led to:
    - pipeline-scratch-aware duplicate and raw-data accounting
    - tighter summary caps
    - an `ext_stats` bug fix
    - three `rules.toml` rules
  - Open for tuning:
    - Peek budget goes to many one-file Nextflow cache keys (`index.<run-name>`); consider skipping keys below a byte floor after round 1.
    - Dir-tree duplicates of files < 1 MiB match on names and sizes only.
    - Digits in extensions (`fasta11.gz`).
- 2026-10-02: **Step 4 trial on a large project dir** (`Lab/projects/<project-C>`; multi-TiB, millions of files).
  - Full SLURM path on `holy-smokes`, end to end in under half an hour. The agent phases ran as two subagents (setup, then report).
  - Findings to act on:
    - `summary.json` is too large to read whole. Make it a compact digest: group problem dirs, format dates, one line per folder.
    - All peeks went to unknown types, so no raw files were sampled.
    - The `sensitive_files` count is noisy (mostly Conda-env libraries).
    - One tree shard dominated the scan wall time.
    - `plan.py` stdout is verbose.
    - SKILL.md's `lfs quota` step is redundant with `quota`.
  - Sizes, timings and token counts are in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
  - Next: do dev target 2 (`Users/`) for unreadable dirs and unaccounted bytes; implement [plans/survey-mode.md](plans/survey-mode.md).
- 2026-10-05: **Survey/deep modes implemented; cluster verification pending** (all partitions down for maintenance). On branch `survey-mode`.
  - `plan.py --mode survey|deep`; `scan.py` splits big tree shards into child shards on the fly (`--split-files`, default 250K entries); `post.py` mode defaults (survey: no peek, dupes ≥ 256 MiB), free-space check, `temp_store=FILE` with `SQLITE_TMPDIR` on node-local disk; `dupes.py` streams candidates; summary v2 (compact tables, grouped problem dirs, sensitive split, size histogram); peek budget split; `SKILL.md` modes and sizing table; `references/survey-template.md`.
  - Tests: `tests/test_survey.py` (split vs unsplit equality, crash resume, survey defaults, v2 shape and size, peek split, sensitive split). All tests pass.
  - Next, when partitions return: regenerate the summary from the step 4 trial's `audit.sqlite` (no target reads) to check the v2 size; then the deep regression, `Users/`, and the whole-lab survey, per the plan's Verification section.
- 2026-10-05: **Cluster trials of survey mode** on `<project-C>` and on `<lab-projects>` (one level up, every user's projects, tens of millions of files). Both ran end to end; the summary stayed under ~50 KB and each report took one subagent with no database queries. Figures are in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
- 2026-10-07: **Fixes from the trials' report-agent feedback.** `rules.toml` (PacBio Revio names raw, HAL processed, PacBio run-folder marker, `accession2taxid.gz` by name); folder dates use the newest file; `summary.json` one row per line, read with the Read tool, with a `capped` index; survey skips duplicate-tree detection; templates (subdirectory share instead of unaccounted, inode comparison, TiB, no backup line, survey "Data types" section); SKILL.md sizing table from measured rates; `DB_PER_GZ` 24. See decisions.md 2026-10-07.
- 2026-10-09: **Deterministic report tables.** `render.py` fills `{{table:NAME}}` lines from `summary.json`; templates give fixed bullet slots; `summary.json` always lists every level-1 folder and the 15 largest level-2 folders, with `level`, `status` and a `last_activity` date per folder. `rules.toml` adds GFA/odgi, Jellyfish, MTZ, D4 and RepeatMasker types. See decisions.md 2026-10-09.
- 2026-10-26: **Whole-lab survey of `<lab>`** (tens of millions of files) ran end to end on `serial_requeue`: 2 scan tasks × 8 walkers, then post. Post was preempted at start and requeued cleanly; the survey's `summary.json` fit in one Read; the report recommends deep audits. Post used one core of 4, so post now requests `-c 1` with `--workers 4` (decisions.md 2026-10-27). Figures are in `audits/dev-notes/DEV-NOTES.md` (local, untracked).
- 2026-10-27: **Survey report made reproducible.** `render.py` now also writes the fixed sentences (`{{facts:NAME}}`), adds types, merged duplicates, small-file, recently-touched and credential-name tables, shortens paths, and lints survey prose (word budgets, numbers repeated from tables). The survey template leaves the agent only interpretation slots. Both survey reports were re-rendered in `audits/report-dev/` for comparison (decisions.md 2026-10-27). The deep template is unchanged (open item).
- 2026-10-28: **Sunburst chart at the top of survey reports** (`{{chart:sunburst}}`): `folders` rows as rings, angle = bytes, colour = main owner (7 palette slots + "other users"), grey for bytes not in a listed subfolder; hover tooltips and branch/owner highlighting; inline SVG and script, no external resources. Compared on both survey reports and the older deep report in `audits/report-dev/sunburst-experiment/`. Limits: only as detailed as `folders` (≥ 0.1%, 3 levels); deep reports don't use it yet (decisions.md 2026-10-28).

## Open items (revisit)
- **Survey/deep modes: implemented 2026-10-05, cluster verification pending.** See [plans/survey-mode.md](plans/survey-mode.md) Verification steps 2–6.
- **Deep report template** still has slots that restate its tables ("Biggest items", per-row notes). Move it to facts blocks, the merged duplicates table and the lint once the survey format is settled.
- **Calibrate the survey's 256 MiB duplicate threshold** from the whole-lab survey's `size_histogram` (arbitrary starting point; user, 2026-10-02).
- **Post `/scratch` high-water at lab scale**: not logged by post; the whole-lab survey's database (~0.4 GB per million files) is a lower bound. Log `/scratch` use in post if it matters.
- **Areas detail level for a whole-lab target.** The detail table is fixed at 2 levels below the target. For a lab root, projects sit one level deeper (`Lab/projects/<user>`), so check the first whole-lab survey's table and decide whether the level should be settable per run.
  - 2026-10-26 (user): the first whole-lab survey keeps the fixed level. The level should eventually be **inferred from the data**, not set per run, because the PI won't know a new lab's layout in advance.
- **Per-area duplicate numbers: on hold** (user, 2026-10-07) until the lab group confirms duplicate identification works as intended. Duplicates are matched by size plus a hash of three 1 MiB slices, not by name, and some groups may be deliberate copies. After that, decide: duplicate bytes and bytes older than 2 years per `folders` row (a group spanning two areas counts in both), every top-level folder always listed, and each user's top folders.
- **Survey has no prompt-injection screen of file names** (raised 2026-10-07; revisit in the safety-overview edit round, user). Injection suspects come only from peeks, which surveys skip, so a survey's "Flagged content" covers only the few hundred names that reach `summary.json`. Option: a cheap regex screen over all file and dir names in the summarize step, reporting paths only. Decide alongside the other SAFETY.md promises.
- **2026-10-02: sandbox disabled for initial development (user decision).** The items below are kept for the before-release decision on whether to bring it back.
- **Sandboxing in general (the user asked to revisit).**
  - Is the value to PIs worth the friction? The value is an OS-level read-only guarantee for improvised agent commands, no Bash network, and fewer prompts. The friction:
    - sandboxed Bash hung on every command after the conversation was resumed (2026-09-30), unresolved
    - `.claude/skills` is write-protected from Bash
    - SLURM jobs aren't covered
  - Decide:
    - keep, adjust, or drop the sandbox for the product
    - whether PIs need "start a fresh session, don't resume" guidance
    - whether to revisit in-job `bwrap` and an sbatch-guard hook
- **The sandbox hangs on Cannon login nodes** (2026-09-30): `bwrap` gets stuck in kernel state `D`, and every Bash command, `!` command and Claude-internal git call blocks. The suspected cause is network-namespace creation (`--unshare-net`) on busy shared nodes. To find out:
  - Does it work inside `salloc -p test`?
  - Which of these hangs: plain `bwrap` or `bwrap --unshare-net`?

  If the sandbox only works on compute nodes, SKILL.md and README must tell PIs to start Claude inside `salloc`. That's a reasonable rule anyway, since the agent shouldn't run on login nodes.
  - **2026-10-01 result:** inside `salloc -p test` it **works** (see decisions.md). Still open: whether plain `bwrap` or `--unshare-net` is what hangs on login nodes. Unless that's resolved, SKILL.md and README should require `salloc`.
- **Sandbox network isn't blocked as documented** (2026-10-01). `.claude/settings.json` has `network.allowedDomains: []` and there are no managed settings, yet `pip install pytest` reached PyPI from sandboxed Bash. Find out why: it could be a proxy default or the meaning of an empty list. Then confirm against https://code.claude.com/docs/en/sandboxing, and fix either the settings or the "Bash network access is blocked" claims in CLAUDE.md, SAFETY.md and development.md.
- **Sandbox placeholder files in the repo root.** Inside the sandbox, `.bashrc`, `.gitconfig`, `.idea`, `.vscode`, `.mcp.json` and similar names appear in the repo root as 0-byte character devices (write-protection mounts), and `git status` lists them as untracked. Don't `git add -A` from inside the sandbox; stage explicit paths, or gitignore those names before the first commit.

## Development sequence
1. **Scaffold**:
   - `git init`, `.gitignore`
   - `docs/plan.md` (this file), `docs/decisions.md`, `docs/development.md`
   - CLAUDE.md, SAFETY.md, settings.json. Confirm the exact sandbox keys against https://code.claude.com/docs/en/sandboxing.
   - `docs/cluster-reference.md`
   - save a project memory pointing to the plan and the decision log
2. `plan.py` and `scan.py`, plus the dedupe and classify steps with unit tests. Test on one small informatics subdirectory, first directly with a tiny target, then as a single `test` job.
3. Peek, summarize, SKILL.md, the report template, and `render.py`. Iterate on report quality using a mid-size informatics directory.
4. The full SLURM path on `test`, staying at or under 3 jobs, on the whole informatics lab dir. Take timings and compare against `lfs find`.
5. Tune the rules and caps, then switch the production default to `serial_requeue`.

## Verification
- `pytest` unit tests pass for dedupe, directory-signature and classify logic.
- The known duplicate is detected at both file level and directory level, with correct reclaimable bytes.
- The unreadable `Users/*` dirs are listed with owner and last-modified date, and unaccounted bytes = group quota − scanned bytes.
- The before/after stat snapshot of the test subdirectory is identical.
- The sandbox write and read denials fire, and an Edit outside `audits/` and `docs/` is refused.
- End-to-end: `/lab-storage-audit /n/holylfs05/LABS/informatics/<subdir>` via SLURM, then review `report.md`, `report.html` and `agent-actions.log`.
