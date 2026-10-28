#!/usr/bin/env python3
"""Walk the shards assigned to this task and record metadata only.

Shard i belongs to task (i % num_tasks). Inside a task, shards are processed
by a pool of worker processes. Each shard writes
  shards/<id>.files.tsv.gz   one row per non-directory entry
  shards/<id>.dirs.tsv.gz    one row per directory the shard owns
  shards/<id>.done           JSON stats; written last (atomic rename)
Shards with an existing .done are skipped, so a requeued task resumes cleanly.
Writes <run>/scan.task<I>.DONE, or scan.task<I>.FAILED if any shard failed.

Splitting on the fly: a tree shard that has walked --split-files entries stops
and hands the subdirs it hasn't entered yet to up to SPLIT_CHILDREN child
shards <id>.0, <id>.1, ... (listed under "children" in its .done). Children run
in the same task as soon as their parent finishes, and may split again. They
exist only once the parent's .done does, so a parent that crashed is re-walked
from scratch and requeue stays idempotent.

Entries are streamed with os.scandir + lstat; nothing is sorted, no file
contents are read, and symlinks are recorded, never followed.
"""
from __future__ import annotations

import argparse
import errno
import json
import multiprocessing as mp
import os
import stat
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

FILE_COLS = ["path", "kind", "size", "blocks", "mtime", "uid", "gid", "dev",
             "ino", "nlink", "mode", "sensitive", "link"]
DIR_COLS = ["path", "status", "uid", "gid", "mode", "mtime",
            "n_entries", "n_files", "n_subdirs", "bytes_direct"]
SPLIT_FILES = 250_000       # entries walked before a tree shard splits (~50 s)
SPLIT_CHILDREN = 16         # at most this many children per split


def load_shards(run_dir: str) -> list[tuple[int, str, str]]:
    out = []
    with open(os.path.join(run_dir, "shards.tsv"), encoding="utf-8",
              errors="surrogateescape", newline="\n") as fh:
        next(fh)
        for line in fh:
            sid, mode, path = line.rstrip("\n").split("\t")
            out.append((int(sid), mode, C.unesc(path)))
    return out


def expand_shards(run_dir: str, top=None) -> list[tuple[str, str, list[str]]]:
    """Planned shards plus every child listed in a finished shard's .done, recursively.

    Returns (shard_id, mode, absolute roots). `top` (planned shards) defaults to
    shards.tsv. A shard's children appear only once the shard itself is done.
    """
    target = C.load_run(run_dir)["target"]
    todo = [(str(sid), mode, [root]) for sid, mode, root in
            (load_shards(run_dir) if top is None else top)]
    out = []
    while todo:
        sid, mode, roots = todo.pop(0)
        out.append((sid, mode, roots))
        done = os.path.join(run_dir, "shards", f"{sid}.done")
        if os.path.exists(done):
            with open(done) as fh:
                for ch in json.load(fh).get("children", []):
                    todo.append((ch["id"], "tree",
                                 [os.path.join(target, r) for r in ch["roots"]]))
    return out


def _errname(ex: OSError) -> str:
    return errno.errorcode.get(ex.errno, str(ex.errno)) if ex.errno else type(ex).__name__


def scan_shard(run_dir: str, run: dict, sid: str, mode: str, roots: list[str],
               split_files: int = SPLIT_FILES) -> dict:
    """Scan one shard; returns its stats dict (also written to <id>.done).
    split_files <= 0 never splits."""
    t0 = time.monotonic()
    target, excludes = run["target"], run["excludes"]
    one_fs, target_dev = run["one_fs"], run["target_dev"]
    fpath = C.out_path(run_dir, "shards", f"{sid}.files.tsv.gz")
    dpath = C.out_path(run_dir, "shards", f"{sid}.dirs.tsv.gz")
    root_set = set(roots)
    first = C.fs_str(C.rel_to(roots[0], target))
    stats = {"shard": sid, "mode": mode,
             "root": first + (f" (+{len(roots) - 1} more)" if len(roots) > 1 else ""),
             "files": 0, "bytes": 0, "dirs": 0, "status": {}, "entry_errors": 0}
    walked = 0

    with C.open_tsv_gz_write(fpath + ".tmp") as fo, C.open_tsv_gz_write(dpath + ".tmp") as do:
        fo.write("\t".join(FILE_COLS) + "\n")
        do.write("\t".join(DIR_COLS) + "\n")

        def dir_row(p, status, st, n_entries=-1, n_files=0, n_subdirs=0, nbytes=0):
            nonlocal walked
            walked += 1
            stats["dirs"] += 1
            stats["status"][status] = stats["status"].get(status, 0) + 1
            if st is None:
                meta = ["", "", "", ""]
            else:
                meta = [st.st_uid, st.st_gid, oct(stat.S_IMODE(st.st_mode)), int(st.st_mtime)]
            do.write("\t".join(map(str, [C.esc(C.rel_to(p, target)), status, *meta,
                                         n_entries, n_files, n_subdirs, nbytes])) + "\n")

        stack = list(reversed(roots))
        while stack:
            if mode == "tree" and 0 < split_files <= walked:
                break                                   # rest goes to child shards
            d = stack.pop()
            try:
                st = os.lstat(d)
            except FileNotFoundError:
                if d in root_set:
                    dir_row(d, "missing", None)
                continue
            except OSError as ex:
                dir_row(d, "error:" + _errname(ex), None)
                continue

            if any(C.is_within(d, e) for e in excludes):
                dir_row(d, "excluded", st)
                continue
            if d != target and C.is_sensitive_dir(d):
                dir_row(d, "sensitive", st)
                continue
            if one_fs and st.st_dev != target_dev:
                dir_row(d, "other-fs", st)
                continue

            status = "ok"
            n_entries = n_files = n_subdirs = nbytes = 0
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n_entries += 1
                        try:
                            est = e.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue            # vanished mid-scan
                        except OSError:
                            stats["entry_errors"] += 1
                            fo.write("\t".join([C.esc(C.rel_to(e.path, target)), "?"]
                                               + [""] * (len(FILE_COLS) - 2)) + "\n")
                            continue
                        m = est.st_mode
                        if stat.S_ISDIR(m):
                            n_subdirs += 1
                            if mode == "tree":
                                stack.append(e.path)
                            continue            # flat: child dirs are their own shards
                        if stat.S_ISREG(m):
                            kind, link = "f", ""
                            n_files += 1
                            nbytes += est.st_size
                        elif stat.S_ISLNK(m):
                            kind = "l"
                            try:
                                link = C.esc(os.readlink(e.path))
                            except OSError:
                                link = ""
                        else:
                            kind, link = "o", ""
                        sens = 1 if C.is_sensitive_file(e.name, d) else 0
                        walked += 1
                        fo.write("\t".join(map(str, [
                            C.esc(C.rel_to(e.path, target)), kind, est.st_size,
                            est.st_blocks, int(est.st_mtime), est.st_uid, est.st_gid,
                            est.st_dev, est.st_ino, est.st_nlink,
                            oct(stat.S_IMODE(m)), sens, link])) + "\n")
                        if kind == "f":
                            stats["files"] += 1
                            stats["bytes"] += est.st_size
            except PermissionError:
                status = "unreadable"
                n_entries = -1 if n_entries == 0 else n_entries
            except OSError as ex:
                status = "error:" + _errname(ex)
            dir_row(d, status, st, n_entries, n_files, n_subdirs, nbytes)

    if stack:       # split: deal the unentered dirs round-robin, so big siblings spread out
        n = min(len(stack), SPLIT_CHILDREN)
        stats["children"] = [{"id": f"{sid}.{i}",
                              "roots": [C.rel_to(p, target) for p in stack[::-1][i::n]]}
                             for i in range(n)]
    os.replace(fpath + ".tmp", fpath)
    os.replace(dpath + ".tmp", dpath)
    stats["elapsed_s"] = round(time.monotonic() - t0, 3)
    C.write_json_atomic(C.out_path(run_dir, "shards", f"{sid}.done"), stats)
    return stats


def _worker(args):
    run_dir, run, sid, mode, roots, split_files = args
    try:
        return scan_shard(run_dir, run, sid, mode, roots, split_files)
    except Exception:
        return {"shard": sid, "failed": traceback.format_exc()}


def run_task(run_dir: str, task_index: int = 0, num_tasks: int = 1,
             workers: int = 1, only_shards: list[int] | None = None,
             split_files: int = SPLIT_FILES) -> dict:
    run_dir = os.path.realpath(run_dir)
    run = C.load_run(run_dir)
    C.check_run_dir_safe(run_dir, run["target"], run["excludes"])
    C.clear_sentinel(run_dir, f"scan.task{task_index}.FAILED")

    top = [s for s in load_shards(run_dir) if s[0] % num_tasks == task_index]
    if only_shards is not None:
        top = [s for s in top if s[0] in set(only_shards)]
    known = expand_shards(run_dir, top)        # children of finished shards stay in this task
    todo = [s for s in known
            if not os.path.exists(os.path.join(run_dir, "shards", f"{s[0]}.done"))]
    C.log(f"task {task_index}/{num_tasks}: {len(top)} shards (+{len(known) - len(top)} "
          f"children so far), {len(todo)} to do, {workers} workers, split at {split_files}")

    t0 = time.monotonic()
    results, failed = [], []

    def job(s):
        return (run_dir, run, s[0], s[1], s[2], split_files)

    def children(r):
        return [(c["id"], "tree", [os.path.join(run["target"], p) for p in c["roots"]])
                for c in r.get("children", [])]

    if workers <= 1:
        queue = list(todo)
        while queue:
            r = _worker(job(queue.pop(0)))
            results.append(r)
            queue += children(r)
    elif todo:
        ctx = mp.get_context("fork")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            pending = {ex.submit(_worker, job(s)) for s in todo}
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    r = f.result()
                    results.append(r)
                    pending |= {ex.submit(_worker, job(c)) for c in children(r)}
    for r in results:
        if "failed" in r:
            failed.append(r)
            C.log(f"shard {r['shard']} FAILED:\n{r['failed']}")
        else:
            split = f", split into {len(r['children'])}" if r.get("children") else ""
            C.log(f"shard {r['shard']} ({r['root']}): {r['files']} files, "
                  f"{r['dirs']} dirs, {r['elapsed_s']}s{split}")

    info = {"task": task_index, "num_tasks": num_tasks, "shards": len(top),
            "children": sum(len(r.get("children", [])) for r in results)
            + len(known) - len(top),
            "scanned_now": len(results) - len(failed), "failed": [r["shard"] for r in failed],
            "files": sum(r.get("files", 0) for r in results),
            "elapsed_s": round(time.monotonic() - t0, 3)}
    if failed:
        C.write_sentinel(run_dir, f"scan.task{task_index}.FAILED", info)
    else:
        C.write_sentinel(run_dir, f"scan.task{task_index}.DONE", info)
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory from plan.py")
    ap.add_argument("--task-index", type=int, default=0)
    ap.add_argument("--num-tasks", type=int, default=1)
    ap.add_argument("--workers", type=int, default=0, help="default: allocated CPUs")
    ap.add_argument("--shard", type=int, action="append",
                    help="only these planned shard ids (their children follow)")
    ap.add_argument("--split-files", type=int, default=SPLIT_FILES,
                    help=f"entries a tree shard walks before splitting (default {SPLIT_FILES}; "
                         "0 = never)")
    args = ap.parse_args(argv)
    if not 0 <= args.task_index < args.num_tasks:
        C.die("--task-index must be in [0, --num-tasks)")
    info = run_task(args.run, args.task_index, args.num_tasks,
                    args.workers or C.default_workers(), args.shard, args.split_files)
    return 1 if info["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
