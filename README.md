# data-concierge-file-audit

This is a Claude Code skill for PIs working with the FAS Informatics [Data Concierge](https://informatics.fas.harvard.edu/services/concierge/) service. It audits your lab's FASRC storage **read-only** and produces a report for the concierge team. The report covers:

- **Duplicates**: files and directories copied across user folders
- **Raw data**: where it lives (FASTQ, BCL run folders, pod5, microscopy, mass spec, and so on)
- **Folder overview**: size, age, and file types for each user or project folder
- **Hidden usage**: unreadable or stale user directories, and how much space they may hold

> **Status: under development.** The skill isn't ready for use yet. See [docs/plan.md](docs/plan.md).

## Before you run it

Read [SAFETY.md](SAFETY.md). It explains exactly what the audit reads, what it can't do (it never modifies, moves, or deletes anything), and how that is enforced.

## Quickstart (once released)

```bash
git clone <repo-url> data-concierge-file-audit
cd data-concierge-file-audit
module load python
claude
```

Then, inside Claude Code:

```
/lab-storage-audit /n/holylabs/<your_lab>
```

The agent checks your quota and proposes a scan plan. It asks your approval before submitting any SLURM jobs, then writes `audits/<run>/report.html` and `report.md`. Review the report, then send it to the concierge team.

## For developers

- [docs/development.md](docs/development.md): the dev loop and verification steps
- [docs/decisions.md](docs/decisions.md): the decision log
- [docs/cluster-reference.md](docs/cluster-reference.md): the cluster rules this skill respects
