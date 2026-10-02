# Cluster reference for developing this skill

These sections were copied and adapted on 2026-09-30 from the FASRC `agent-guards` repo (`global/CLAUDE.md` and `global/skills/cannoncluster/`), which is gitignored here as reference-only. Keep only what matters for this skill.

**Policy is binding, and this file is not the authority.** Before asserting a quota, purge, retention, or data-classification rule, check the linked `docs.rc.fas.harvard.edu` page. A copied rule can go stale.

## Login vs compute nodes

- Run `hostname` first. A name matching `*login*` (`bosloginXX`/`holyloginXX`) with no `$SLURM_JOB_ID` set means you are on a **login node**.
- A login node is fine for editing, git, `module avail`, and reading small files such as `summary.json`. It is never fine for walks over the target, hashing, or anything multi-core or multi-minute. Use `sbatch` for those, or `salloc` for interactive dev.
- The login node shares storage mounts with compute nodes, so don't do bulk file operations there either (tar, rsync, full scans).
- Anything that must survive a disconnect goes through `sbatch`. Never background work under `salloc` with `nohup`, `setsid`, or `&`, because an idle `salloc` dies after about an hour.
- Source: `cannoncluster/SKILL.md` ("Where am I?"). Responsibilities/AUP: https://docs.rc.fas.harvard.edu/kb/responsibilities/

## Partitions for this skill

| Use | Partition | Notes |
|---|---|---|
| Dev / debug | `test` | Official table (fetched 2026-09-30): 12 h max, **Max Jobs 5**, Max Cores 112, 990 GB/node, not charged fairshare, multi-partition submits forbidden. **Project rule: at most 3 jobs queued or running, array tasks included** (the user's cap, stricter than the official 5). Throttle arrays with `--array=…%N`. |
| Production scan | `serial_requeue` | Official table: 3 days max, no job or core cap listed, memory "varies". For "jobs that require up to 8 cores … or job arrays where each job instance uses less than 8 cores". Default partition when `-p` is omitted. Jobs **can be preempted and requeued**, so helpers must be idempotent and use `--open-mode=append`. |

Cluster-wide limits (same page): 10,100 jobs per user, max array size 10,000, and each array index counts as a job. Defaults: 1 core, 100 MB memory, no default time.

- **Never guess resource limits** (`-t`, `--mem`, `-c`). Set them per partition from the official table: https://docs.rc.fas.harvard.edu/kb/running-jobs/#Slurm_Partitions. On FASSE, use https://docs.rc.fas.harvard.edu/kb/fasse/#SLURM_and_Partitions. Never paste a partition maximum in as a default.
- Cannon defaults are minimal. The default memory is **100 MB**, and `-t` is **mandatory**.
- Fairshare is lab-scoped: hours burned on `serial_requeue` count against the whole group. Mention the cost if a planned audit is large.
- `#SBATCH` lines must come before any code. SLURM silently stops parsing directives after the first non-directive line.
- Source: `cannoncluster/references/partitions.md`, `references/gotchas.md`.

## Slurm query cadence (hard rule)

- **Never query SlurmDB or the controller** (`sacct`, `sreport`, `sacctmgr`, `squeue`, `sinfo`, `scontrol`, `showq`) **more than once per 10 minutes**. That rules out polling loops and `watch`.
- This skill tracks jobs with **sentinel files** (`DONE`/`FAILED` written by the job into the run directory) and watches them with a filesystem check. At most one `squeue` call is allowed per 10 minutes, and only when a sentinel is overdue.
- Source: `global/CLAUDE.md` hard rules and `references/gotchas.md`.

## Lustre / large-filesystem gotchas

- **`du -sh` is slow over huge or deep trees** on any FASRC tier. A full `du` over millions of files can take hours. Use `quota <PATH>` for tier-level usage, and reserve `du` for a specific subdirectory known to be small.
- **Don't `ls` huge directories.** Plain `ls` sorts its output, and `ls -l`/`--color` stat every entry. For a count, use `ls -f | wc -l`, which is unsorted and has no stat per entry. The helpers use streaming `os.scandir`.
- **Many small files perform poorly.** A directory with more than 1K files is a finding worth reporting: suggest sharding into subdirectories or aggregating into tar, HDF5, or Parquet.
- The dev target (`/n/holylfs05`) is Lustre 2.15.8. `lfs find` is an alternative walker; compare its timings against `os.scandir`.
- Source: `cannoncluster/references/filesystems.md` ("Large-filesystem gotchas").

## Quota

- `quota <PATH>` is FASRC's documented wrapper and covers home, `/n/holylabs`, `/n/netscratch`, and paid tiers: https://docs.rc.fas.harvard.edu/kb/checking-quota-and-usage/
- On Lustre, `lfs quota -h -g <group> <mount>` gives group bytes and files.
- `lfs quota -u <other user>` returns **Permission denied** for an unprivileged account (verified). An unprivileged account cannot see another user's usage, so the size of an unreadable directory can only be inferred, as group total minus the scanned bytes.

## Storage tiers (for interpreting what a lab has)

These are the complimentary tiers. Source: https://docs.rc.fas.harvard.edu/kb/data-storage-workflow-rdm/

| Path | Quota | Backup | Retention |
|---|---|---|---|
| `~/` (`/n/home??/<user>`) | 100 GiB | snapshots | permanent |
| `/n/holylabs/<lab>/` | 4 TiB | none | permanent |
| `/n/netscratch/<lab>/` | 50 TiB | none | **90-day purge** |

- **Paid tiers** (current naming): Compute Storage `/n/compute_storage/pi_lab`, Lab Storage `/n/lab_storage/pi_lab`, Long-term Storage `/n/long_term/pi_lab`, Tape (NESE, via Globus), and FASSE `/n/fasse/...`.
- **Storage Modernization Initiative** (in progress): legacy backends are being consolidated into Compute Storage and Lab Storage. The legacy backends are `holylfs05`/`holylfs06`, `holystore01`, `bos-isilon`/`holy-isilon`, `boslfs02`, and the NFS mounts. **The dev target itself (`/n/holylfs05/LABS/...`) is a legacy path**, so reports should flag hardcoded legacy paths. netscratch, holylabs, and home are unaffected. Sources: https://www.rc.fas.harvard.edu/data-storage/ and https://docs.rc.fas.harvard.edu/kb/storage-modernization-initiative-faqs/
- **netscratch purge**: never `touch` files to dodge the purge. FASRC calls that filesystem abuse, and it can trigger administrative action (https://docs.rc.fas.harvard.edu/kb/policy-scratch/). The right recommendation is to *migrate* data to a permanent tier. This skill is read-only, so it never touches timestamps anyway.
  - The policy page (fetched 2026-09-30) says "files older than 90 days will be deleted" but never names the timestamp. Its tar tip (`-m`/`--touch` "to update modified time") points to **mtime**. So the reads we do for hashing and peeking, which can only change atime, should not affect the purge. We haven't confirmed this with FASRC.

## Permissions and unreadable directories

- **Many user directories are mode 700.** A scan must distinguish *absent* from *unreadable*. On the dev target, 5 of 7 `Users/*` directories are unreadable to the auditor.
- `stat` on an unreadable directory still works (the parent is readable), which gives owner, mode, and the directory's own mtime. That mtime changes only when top-level entries change, not on deep edits.
- Group-readable does not always mean readable by the PI. Joint appointments and secondary groups are common, so check `id <user>` before assuming access.
- Source: `cannoncluster/references/gotchas.md` ("Permissions and groups").

## Sandbox behavior inside a Claude Code session

*The project sandbox is off during initial development (2026-10-02). This section applies only when it's re-enabled.*

- An unreadable or unwritable path inside a session is **usually the sandbox boundary, not a cluster fault**. Don't retry with variations, don't hunt for alternative paths, and don't report it as an outage.
- `mount`, `findmnt`, and `/proc/mounts` show the *namespace's* view, so they can't tell the sandbox apart from the node.
- To act outside the namespace, hand the command to the user prefixed with `!`.
- The sandbox bind-mounts some repo paths (`.env*`, `.git/config`, shell rc files) to `/dev/null`. They show up as stray untracked entries in `git status` and need no cleanup.
- SSH-based git push usually can't work from inside the sandbox. Use HTTPS with a PAT, or have the human push.
- Source: `cannoncluster/references/gotchas.md` ("A constrained environment is not a broken one").

## FASSE separation (hard rule)

- FASSE is a *physically separate* Level-3 cluster for sensitive data (https://docs.rc.fas.harvard.edu/kb/fasse/). **Level-3 data must never move between FASSE and Cannon.**
- If `hostname` indicates a FASSE node, don't assume Cannon defaults such as partitions or internet egress. An audit of FASSE storage must run on FASSE, and its report (paths and metadata) stays on FASSE unless the PI and FASRC confirm it may leave.
- FASSE lab directories: https://docs.rc.fas.harvard.edu/kb/fasse/#Your_lab_directory

## Credential and secret paths (never open)

This list was copied from `agent-guards/global/settings.json` `deny`, and the helpers use the same patterns. File contents matching these patterns are never read or peeked. Such files are counted in metadata only.

- **Environment and secrets dirs:** `.env`, `.env.*`, `secrets/**`
- **SSH, GPG, cloud and container config:** `.ssh/**`, `.gnupg/**`, `.aws/**`, `.kube/**`, `.config/gcloud/**`, `.azure/**`, `.docker/config.json`
- **Credential and config dotfiles:** `.git-credentials`, `.netrc`, `.pgpass`, `.my.cnf`, `.pypirc`, `.npmrc`, `.Renviron`
- **Data-transfer tool credentials:** `.irods/**`, `.globus*/**`, `.ncbi/**`, `*.ngc`
- **Key files:** `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.pem`, `*.key`
- **Name patterns:** `*credentials*`, `*secret*`, `*_token`, `*.token`
- **Shell history:** `.bash_history`, `.python_history`

## Python environment

- Use `module load python` (Python 3.14 as of 2026-09-30) before running helpers. Helpers are stdlib-only, so nothing needs installing.
- Keep `conda init` and `source activate` out of `~/.bashrc`, because they break batch jobs.
