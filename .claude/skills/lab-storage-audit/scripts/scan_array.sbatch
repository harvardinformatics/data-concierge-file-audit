#!/bin/bash
# lab-storage-audit: metadata scan. Array task i scans shards with id % N == i
# (N = array size), using one worker process per allocated CPU.
#
# Submit FROM THE REPO ROOT. Partition/resources/logs are passed by the caller
# (limits per https://docs.rc.fas.harvard.edu/kb/running-jobs/#Slurm_Partitions):
#   sbatch --parsable -p test -t 0-01:00 -c 4 --mem=4G --array=0-0 \
#     -o audits/<run>/slurm/%x-%A_%a.out -e audits/<run>/slurm/%x-%A_%a.err \
#     .claude/skills/lab-storage-audit/scripts/scan_array.sbatch audits/<run>
# Dev rule: <=3 jobs on `test` at once, array tasks included (use %N throttle).
#SBATCH -J lsa-scan
#SBATCH --open-mode=append
set -euo pipefail

RUN="$(realpath "${1:?usage: scan_array.sbatch <run_dir>}")"
SCRIPTS="${SLURM_SUBMIT_DIR:?}/.claude/skills/lab-storage-audit/scripts"
TASK="${SLURM_ARRAY_TASK_ID:-0}"
NTASKS="${SLURM_ARRAY_TASK_COUNT:-1}"
[ -f "$SCRIPTS/scan.py" ] || { echo "scan.py not found under $SCRIPTS (submit from repo root)" >&2; exit 2; }

fail() {   # keep scan.py's more detailed sentinel if it wrote one
    rc=$?
    [ -e "$RUN/scan.task$TASK.FAILED" ] || printf '{"job": "%s", "task": %s, "exit": %s, "host": "%s"}\n' \
        "${SLURM_JOB_ID:-}" "$TASK" "$rc" "$(hostname)" > "$RUN/scan.task$TASK.FAILED"
}
trap fail ERR

echo "lsa-scan task $TASK/$NTASKS on $(hostname) at $(date -Is); run=$RUN"
set +u; module load python; set -u
python3 "$SCRIPTS/scan.py" --run "$RUN" --task-index "$TASK" --num-tasks "$NTASKS" \
    --workers "${SLURM_CPUS_PER_TASK:-1}"
echo "lsa-scan task $TASK done at $(date -Is)"
