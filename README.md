# data-concierge-file-audit

A Claude Code skill that audits your lab's FASRC storage **read-only** and writes a report you can review and share with the FAS Informatics [Data Concierge](https://informatics.fas.harvard.edu/services/concierge/) team. It answers questions like these:
- Where does our raw data live?
- What is copied, and how much space do the copies take?
- Who owns what?
- How much hasn't been touched in years?

> **Status: under development.** The skill isn't ready for use yet. See [docs/plan.md](docs/plan.md).

## Two kinds of audit

| | **Survey** | **Deep audit** |
|---|---|---|
| **Covers** | a whole lab | one project or member folder |
| **Answers** | how data moves through the lab, and where to look closer | how one area is laid out, and who owns what |
| **Reads** | file metadata only (names, sizes, dates, owners) | metadata, plus small samples to recognize file types (at most 64 KiB from each of at most 300 files) |
| **Duplicates** | files of 256 MiB or more | files of 1 MiB or more, plus identical folder trees |
| **Ends with** | ranked recommendations for deep audits | a description of the area; it doesn't recommend changes |

Both write `audits/<run>/report.html` and `report.md`. `report.html` is a single self-contained page that works offline, and `report.md` has the same content as plain text.

## Survey: a map of your whole lab

- **At a glance**: headline tiles covering:
  - total size scanned
  - likely duplicate copies
  - data unchanged for 3+ years
  - former members' data
  - active and former members
  - folders the audit couldn't read
- **Folder sunburst**: an interactive chart of folder sizes, coloured by each folder's main owner.
- **Key findings**: 3 to 5 one-line takeaways, each with a number.
- **Data life cycle**:
  - *Raw data access*: is raw data kept in one central place, or copied or linked into members' folders?
  - *Shared resources*: reference genomes, containers and software environments that several folders each keep their own copy of.
  - *Old data and former members*: where data that hasn't changed in years sits, and data owned by people no longer in the lab's group.
- **Data types**:
  - which kinds of files take the most space
  - where the raw data is
  - file types the audit didn't recognize
  - file names that look like credentials (counted by name, never opened)
- **Areas**: for each top-level folder:
  - what it holds (raw, processed, reference, software)
  - folders with many small files
  - pipeline work folders
- **Users**: each person's footprint, and whether they're still in the lab's group.
- **Big duplicates**: the largest likely duplicate files.
- **Recommended deep audits**: ranked, with size and estimated scan time.

<details>
<summary><b>Preview: at a glance and key findings</b> (made-up example data)</summary>

- **48.2 TiB** scanned (21,904,117 files, 1,203,566 folders)
- **3.1 TiB** likely duplicate copies (6.4% of the total)
- **41.1%** unchanged 3+ years (19.8 TiB)
- **2.6 TiB** former members' data (5.4% of the total)
- **8 / 2** active / former members (11 file owners in all)
- **37** folders not scanned (unreadable, or skipped as credential stores)

**Key findings**
- **Raw data is kept per member:** 6 of 8 member folders hold their own sequencing runs; 120.4 GiB of raw files are copied between members.
- **One reference, many copies:** reference genomes and indexes are copied across 6 folders (310.5 GiB of copies), a candidate for one shared lab copy.
- **41.1% (19.8 TiB) hasn't changed in 3+ years**, most of it in `projects/bob`.
- **2.6 TiB belongs to former members**, in `projects/erin` and `scratch-old`.
- **Top deep audit:** `projects/bob` (14.0 TiB of alignments and variant calls).

</details>

<details>
<summary><b>Preview: a Data life cycle section</b> (made-up example data)</summary>

**Shared resources**

- **Kept per area:** reference data in 6 areas, software environments in 5; copies are compared only for files ≥ 256 MiB, so smaller indexes and environments aren't counted as copies.
- **Patterns:**
  - Each member keeps their own copy of the same mouse reference and its aligner indexes; one shared copy would serve all of them (recommendation).
  - Containers are mostly the same few pipeline images, pulled separately by each person.

| Resource | Areas holding it | Size | Copies across areas | Largest in |
|---|---|---|---|---|
| Reference genomes, indexes, annotation | 6 | 1.2 TiB | 310.5 GiB in 18 groups | `alice`, `bob`, `carol` |
| Container images | 4 | 40.1 GiB | 12.0 GiB in 3 groups | `carol`, `dave` |
| Software environments (64) | 5 | 52.3 GiB | not compared | `alice`, `dave` |

</details>

<details>
<summary><b>Preview: recommended deep audits</b> (made-up example data)</summary>

| Scope (`--only`) | Size | Files | Newest file | Why | Est. scan time | Est. tokens |
|---|---|---|---|---|---|---|
| `projects/bob` | 14.0 TiB | 2,811,204 | 2026-09-30 | Largest area; old alignments next to new runs | ~5 min | ~100K (provisional) |
| `projects/erin` | 2.4 TiB | 96,310 | 2022-03-14 | Former member; nothing new in 4 years | ~1 min | ~100K (provisional) |
| `shared/references` | 1.1 TiB | 40,877 | 2025-11-02 | Copied into 6 member folders | ~1 min | ~100K (provisional) |

</details>

## Deep audit: one project or member folder

A deep audit helps a lab understand one area before reorganizing it. It **describes what is there and who owns it**, and doesn't suggest a reorganization, because that depends on goals only the lab knows.

- **At a glance**: headline tiles covering:
  - size scanned
  - likely duplicate copies
  - raw data
  - owners, and folders with mixed ownership
  - data unchanged for 2+ years
- **Folder sunburst**: coloured by owner, and hatched where no single owner holds 80% of a folder.
- **Key findings** and **organization observations**:
  - Is there a consistent layout (by project, person or data stage)?
  - Are raw and processed data kept apart?
  - Are names consistent?
  - Are inputs copied instead of linked?
  - Is code mixed in with data?
  - Are there folders without READMEs?
- **Ownership**: who owns what, since only a file's owner can move or change it. It includes:
  - owner share bars
  - each owner's main folders
  - folders whose files belong to several people
- **Per-folder overview**:
  - sizes, file counts and newest files
  - folders with many small files
  - old data that someone touched recently
- **Raw data**: where it is and what it is (FASTQ, BCL run folders, pod5, microscopy, mass spec, and so on), checked against small samples of each file type.
- **Duplicates**: likely duplicate files and identical folder trees, with the space a clean-up could free.
- **Background** (collapsed by default):
  - scope and method
  - quota compared with what was scanned
  - unreadable folders and data age
  - pipeline intermediates
  - a file-type appendix

<details>
<summary><b>Preview: the Ownership section</b> (made-up example data)</summary>

- **Patterns:**
  - `dave`'s assembly work sits inside `alice`'s project folders, so moving those folders needs both of them.
  - `erin` is no longer in the lab's group; all of their files are in `archive-2021`, unchanged since 2022.

| Owner | Size | Share | Files | Main folders (share of theirs) | Newest file | Note |
|---|---|---|---|---|---|---|
| alice | 9.8 TiB | 70.0% | 1,402,118 | `rnaseq` 61.2%, `assembly` 30.5%, `shared-inputs` 8.3% | 2026-10-21 | |
| dave | 3.1 TiB | 22.1% | 610,452 | `assembly` 88.0%, `rnaseq` 12.0% | 2026-09-02 | |
| erin | 1.1 TiB | 7.9% | 18,201 | `archive-2021` 100.0% | 2022-01-17 | not in group; no new files in 2+ years |

Folders with mixed ownership:

| Folder | Size | File owners | Folder owner |
|---|---|---|---|
| `assembly/hifi-2026` | 2.9 TiB | dave 72%, alice 28% | alice (differs) |

</details>

## Reading the report

- **Interpretation first.** Each section opens with a short *Patterns* note, then the tables behind it. In `report.html` only the first table in each section is open, and the rest are one click away. Background sections start collapsed, and printing opens everything.
- **Numbers come from the data, not the AI.** A script fills in the tables, headline figures and fixed sentences from the scan's summary, so re-rendering a report always gives the same numbers. Claude writes only the interpretation, and labels guesses "likely".
- **No file contents.** The report holds paths, sizes, dates, owners and file classifications, and never what's inside your files.
- **Everything is logged.** `agent-actions.log` lists every command the agent ran and every file it read, and ships with the report.

## Before you run it

Read [SAFETY.md](SAFETY.md). It explains exactly what the audit reads, what it can't do (it never modifies, moves, or deletes anything), and how that is enforced.

## Quickstart (once released)

Start an interactive job first. The agent waits for its scan jobs, so don't run it on a login node:

```bash
salloc -p test -c 1 --mem=4G -t 0-6:00:00
git clone <repo-url> data-concierge-file-audit
cd data-concierge-file-audit
module load python
claude
```

Then, inside Claude Code, run one of these:

```
/lab-storage-audit survey /n/holylabs/<your_lab>
/lab-storage-audit deep /n/holylabs/<your_lab>/<project>
```

If you leave out the mode, the audit picks one for you: a survey for a lab root or for anything over about 5 million files, and otherwise a deep audit.

What happens next:
1. The agent checks your quota and proposes a scan plan. It asks whether any folders are sensitive and should be skipped.
2. It shows you the exact SLURM jobs and resources, and **asks your approval** before submitting them.
3. It waits for the jobs to finish, then writes `audits/<run>/report.html` and `report.md`.
4. Review the report, then send it to the Data Concierge team. A survey ends by offering the top recommended deep audit.

Job time depends on file count, plus queue time. In our test runs, a survey of tens of millions of files took about an hour or more. A deep audit of a few million files took from about 20 minutes to over an hour, depending on how many same-size files needed comparing.

## For developers

- [docs/spec.md](docs/spec.md): what each script does and how the agent works
- [docs/development.md](docs/development.md): the dev loop and verification steps
- [docs/decisions.md](docs/decisions.md): the decision log
- [docs/cluster-reference.md](docs/cluster-reference.md): the cluster rules this skill respects
