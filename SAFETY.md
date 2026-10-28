# SAFETY: what this audit does, and what it can't do

This repo contains a Claude Code skill, `/lab-storage-audit`, that the FAS Informatics [Data Concierge](https://informatics.fas.harvard.edu/services/concierge/) team asks PIs to run on their lab's FASRC storage. It produces a summary of what data you have and how it is organized, so we can give data-management advice. This page states what the audit does, what it deliberately cannot do, and how each promise is enforced.

> **Status:** under development. Enforcement items marked *(to verify)* are designed but not yet tested. See `docs/development.md`.

## What it does

1. **Reads file metadata** under the directory you point it at (the *target*): names, sizes, modification dates, owners, and link counts. Directories you can't read are recorded by name, owner, and last-modified date only.
2. **Reads small samples of file contents** (deep audits only) so it can recognize file types, the way a person would open one file of each kind to see what it is.
   - It reads at most **64 KiB** from each of at most **300** representative files.
   - It samples one file per (directory, file type), never every file.
3. **Finds likely duplicates.** It compares file sizes, then hashes three 1 MiB slices of each candidate (start, middle, end). A full-file hash runs only if you ask for one (`--confirm`).
4. **Writes a report** into `audits/<run>/` inside this repo: `report.md`, `report.html`, `summary.json`, and `agent-actions.log`, a log of every command the agent ran and every file it read. It also keeps its working database, `audit.sqlite` (the metadata above, about 0.25–0.4 GB per million files), there.

### Survey and deep audits

- A **survey** maps a whole lab quickly. It reads metadata (1) and checks duplicates (3) only among files of **256 MiB or more**. It reads **no content samples** (2).
- A **deep** audit covers one project or user directory and does all of the above.
- Both are equally read-only. A survey's database can be large for a big lab (about 0.25–0.4 GB per million files, so tens of GB for tens of millions of files), and it lives in `audits/` like everything else; delete the run folder when you're done with it.

## What it cannot do

- **It cannot modify, move, or delete anything in your storage.** It never runs `rm`, `mv`, `chmod`, or `touch` against your files, and never changes timestamps or permissions. All clean-up is a *recommendation* in the report for you to act on, or not.
- **It cannot read secrets.** Credential-like files are never opened. They are at most counted in metadata. These include:
  - `.ssh/`, `.gnupg/`, `.aws/`, `.env*`, `secrets/`
  - `*.pem`, `*.key`, `.netrc`, `.git-credentials`
  - `*token*`, `*secret*`, and `*credentials*` files
  - the full list is in `docs/cluster-reference.md`
- **It cannot get past Unix permissions.** It runs as *you*, so it sees only what you can already see. Directories other lab members have made private stay private: the report lists that they exist, not what they contain.
- **It sends nothing anywhere.** The report stays in `audits/` until *you* review it and choose to send it to us. The agent has no reason to send anything, and commands that copy files off the cluster (`ssh`, `scp`, `rsync`, Globus, `rclone`) always ask you first.
- **It puts no file contents in the report.** Sampled bytes are used to classify files ("paired-end FASTQ, 150 bp") and are kept in `audits/<run>/peeks.jsonl` on the cluster. The report contains classifications, sizes, dates, and paths.

## How this is enforced (defence in depth)

| Layer | What it enforces |
|---|---|
| **Helper scripts** (`.claude/skills/lab-storage-audit/scripts/`) | Open target files read-only (`rb`). Write only under `audits/<run>/`. Refuse to run if the output dir is inside the scanned tree (this repo is auto-excluded from scans). Never follow symlinks outside the target. Skip credential patterns. |
| **Claude Code sandbox** (`.claude/settings.json`) | **Currently off** (2026-10-02, during initial development): it hung on login nodes and broke group lookups and Slurm access on compute nodes. When it's on, it confines every shell command at the OS level (`bubblewrap`): writes only inside this repo, no reads of `/root` or credential dirs. It may return before release. Until then, the other layers carry the guarantees. |
| **Permission rules** (`.claude/settings.json`) | The agent may edit files only under `audits/` and `docs/`. Deleting, moving, `chmod`, `rsync`/`scp`, **every SLURM job submission**, and similar commands always ask you first. Reading credential files is denied outright. |
| **SLURM jobs** | The heavy scanning runs as batch jobs, not on the shared login node. Batch jobs run outside any Claude Code sandbox, so the helper-script guarantees above are what protect your data there. The code is short, stdlib-only Python that you or FASRC can review. |
| **Action log** | `audits/<run>/agent-actions.log` records every command the agent ran and every file it read, and is included with the report. |

The agent also treats everything it reads as data, never as instructions, including file names and sampled contents. If a file seems to contain instructions aimed at an AI, it flags that in the report and does not follow them.

## Cluster resources

- Scans run as SLURM jobs on `serial_requeue`, which uses idle capacity. Your lab's fairshare is charged for the core-hours used. Before submitting, the agent shows you the number of jobs and the requested resources, and waits for your OK.
- Job status is tracked with small marker files rather than frequent queue queries, following FASRC's guidance on not overloading the scheduler.
- It never uses `du` over your whole tree. Tier-level usage comes from FASRC's `quota` tool.

## Sensitive data / FASSE (Level 3)

- If your lab has Level-3 data on **FASSE**, run the audit **on FASSE**. The report itself contains paths and metadata, so it **stays on FASSE**. Do not copy it to Cannon or email it until you've confirmed with FASRC that this is allowed. See https://docs.rc.fas.harvard.edu/kb/fasse/.
- Level-3 data must never move between FASSE and Cannon, and this tool never moves data at all.
- If you know a Cannon directory holds sensitive data that should not be sampled, tell the agent to exclude it. The directory will be counted by metadata only.

## What we (Data Concierge) receive

Only what you choose to send us, normally `report.html` or `report.md` and optionally `summary.json`. Please skim the report before sending. It lists paths and user names from your lab directory.

## Questions

Contact FAS Informatics: https://informatics.fas.harvard.edu/services/concierge/
