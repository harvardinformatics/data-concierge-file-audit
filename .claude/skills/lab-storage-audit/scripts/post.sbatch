#!/bin/bash
# lab-storage-audit: post.py stages (per the run's mode) into <run>/audit.sqlite.
# Builds the database on node-local /scratch, then copies it back to the run dir.
# Limits per https://docs.rc.fas.harvard.edu/kb/running-jobs/#Slurm_Partitions (or spart).
#
# Submit FROM THE REPO ROOT, after the scan; use afterany (not afterok) so a
# failed scan doesn't leave this job pending forever — post.py itself refuses
# to run if any shard is unfinished:
#   sbatch --parsable -p <partition> -t <time> -c <cpus> --mem=<mem> --dependency=afterany:<scan_jobid> \
#     -o audits/<run>/slurm/%x-%j.out -e audits/<run>/slurm/%x-%j.err \
#     .claude/skills/lab-storage-audit/scripts/post.sbatch audits/<run> [post.py options]
#SBATCH -J lsa-post
#SBATCH --open-mode=append
set -euo pipefail

RUN="$(realpath "${1:?usage: post.sbatch <run_dir> [post.py options]}")"
shift
SCRIPTS="${SLURM_SUBMIT_DIR:?}/.claude/skills/lab-storage-audit/scripts"
[ -f "$SCRIPTS/post.py" ] || { echo "post.py not found under $SCRIPTS (submit from repo root)" >&2; exit 2; }

WORK="/scratch/${USER}/${SLURM_JOB_ID:-manual}-lsa"
cleanup() { [ -n "${WORK:-}" ] && [ -d "$WORK" ] && rm -rf -- "$WORK"; }
fail() {   # keep post.py's more detailed sentinel if it wrote one
    rc=$?
    [ -e "$RUN/post.FAILED" ] || printf '{"job": "%s", "exit": %s, "host": "%s"}\n' \
        "${SLURM_JOB_ID:-}" "$rc" "$(hostname)" > "$RUN/post.FAILED"
}
trap cleanup EXIT
trap fail ERR
mkdir -p "$WORK"
export SQLITE_TMPDIR="$WORK"     # SQLite sort/index temp files go to node-local disk too

echo "lsa-post on $(hostname) at $(date -Is); run=$RUN work=$WORK"
set +u; module load python; set -u
# post.py writes post.DONE / post.FAILED itself.
python3 "$SCRIPTS/post.py" --run "$RUN" --workers "${SLURM_CPUS_PER_TASK:-1}" \
    --workdir "$WORK" "$@"
echo "lsa-post done at $(date -Is)"
