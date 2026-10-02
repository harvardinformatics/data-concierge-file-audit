#!/usr/bin/env python3
"""Create an audit run directory and plan scan shards with a bounded survey.

Lists at most the directories above --depth (target itself, then each level
down to depth-1) — never walks the tree. Every directory at level --depth
becomes a 'tree' shard (walked recursively by scan.py); directories above it
become 'flat' shards (their own entries only; child dirs are separate shards).

Writes: <run>/run.json, <run>/shards.tsv, and audits/.current-run.
Prints a JSON summary for the agent on stdout.

Example (dev target 1):
  python3 plan.py /n/holylfs05/LABS/informatics \
      --only Everyone/support/<project-A> \
      --only Everyone/support/<project-B>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def survey(target: str, roots: list[str], depth: int, excludes: list[str],
           max_children: int, one_fs: bool):
    """Return (shards, notes). shards: list of (mode, abspath)."""
    target_dev = os.lstat(target).st_dev
    shards: list[tuple[str, str]] = []
    notes: list[dict] = []

    def expand(path: str, level: int) -> None:
        # Anything that can't or mustn't be listed becomes a tree shard; scan.py
        # records its status (excluded / sensitive / unreadable / other-fs).
        if (level >= depth or any(C.is_within(path, e) for e in excludes)
                or C.is_sensitive_dir(path)):
            shards.append(("tree", path))
            return
        try:
            if one_fs and os.lstat(path).st_dev != target_dev:
                shards.append(("tree", path))
                return
            children = []
            n_entries = 0
            with os.scandir(path) as it:
                for e in it:
                    n_entries += 1
                    if e.is_dir(follow_symlinks=False):
                        children.append(e.path)
        except OSError:
            shards.append(("tree", path))
            return
        if len(children) > max_children:
            notes.append({"path": C.fs_str(C.rel_to(path, target)),
                          "note": f"{len(children)} subdirs > --max-children; one tree shard"})
            shards.append(("tree", path))
            return
        shards.append(("flat", path))
        if level < 2:
            notes.append({"path": C.fs_str(C.rel_to(path, target)), "level": level,
                          "entries": n_entries, "subdirs": len(children)})
        for c in sorted(children):
            expand(c, level + 1)

    for r in roots:
        expand(r, 0)
    return shards, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="directory to audit (read-only)")
    ap.add_argument("--only", action="append", default=[], metavar="REL",
                    help="restrict to these subpaths of the target (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATH",
                    help="absolute or target-relative path to skip (repeatable)")
    ap.add_argument("--depth", type=int, default=2,
                    help="levels listed before switching to recursive shards (default 2)")
    ap.add_argument("--max-children", type=int, default=2000,
                    help="dirs with more subdirs than this become one tree shard")
    ap.add_argument("--cross-fs", action="store_true",
                    help="descend into other filesystems (default: record, don't descend)")
    ap.add_argument("--audits-dir", default=str(C.REPO_ROOT / "audits"))
    ap.add_argument("--name", help="run name (default <target-basename>_<YYYYmmdd-HHMMSS>)")
    args = ap.parse_args(argv)

    target = os.path.realpath(args.target)
    if not os.path.isdir(target):
        C.die(f"target is not a directory: {target}")
    if target == "/":
        C.die("refusing to audit /")
    repo = str(C.REPO_ROOT)
    if C.is_within(target, repo):
        C.die(f"target {target} is inside this repo {repo}")

    excludes = []
    for e in args.exclude:
        p = e if os.path.isabs(e) else os.path.join(target, e)
        excludes.append(os.path.realpath(p))
    if C.is_within(repo, target):
        excludes.append(repo)                       # auto-exclude this repo
    excludes = sorted(set(excludes))

    roots = []
    for r in args.only or ["."]:
        p = os.path.realpath(os.path.join(target, r))
        if not C.is_within(p, target):
            C.die(f"--only {r} resolves outside the target")
        if not os.path.isdir(p):
            C.die(f"--only {r} is not a directory")
        roots.append(p)
    for a in roots:
        if any(a != b and C.is_within(a, b) for b in roots):
            C.die(f"--only paths overlap: {a}")

    name =args.name or f"{os.path.basename(target)}_{dt.datetime.now():%Y%m%d-%H%M%S}"
    if not all(ch.isalnum() or ch in "._-" for ch in name):
        C.die(f"bad run name {name!r}: use letters, digits, . _ -")
    audits = os.path.realpath(args.audits_dir)
    run_dir = os.path.join(audits, name)
    C.check_run_dir_safe(run_dir, target, excludes)
    os.makedirs(os.path.join(run_dir, "shards"), exist_ok=False)
    os.makedirs(os.path.join(run_dir, "slurm"), exist_ok=True)

    shards, notes = survey(target, roots, args.depth, excludes,
                           args.max_children, not args.cross_fs)

    with open(C.out_path(run_dir, "shards.tsv"), "w", encoding="utf-8",
              errors="surrogateescape", newline="\n") as fh:
        fh.write("shard_id\tmode\tpath\n")
        for i, (mode, p) in enumerate(shards):
            fh.write(f"{i}\t{mode}\t{C.esc(p)}\n")

    run = {
        "schema_version": C.SCHEMA_VERSION,
        "created": C.now_iso(),
        "created_by": os.environ.get("USER", ""),
        "host": os.uname().nodename,
        "target": target,
        "target_dev": os.lstat(target).st_dev,
        "roots": [C.rel_to(r, target) for r in roots],
        "excludes": excludes,
        "depth": args.depth,
        "one_fs": not args.cross_fs,
        "n_shards": len(shards),
    }
    C.write_json_atomic(C.out_path(run_dir, "run.json"), run)
    with open(os.path.join(audits, ".current-run.tmp"), "w") as fh:
        fh.write(name + "\n")
    os.replace(os.path.join(audits, ".current-run.tmp"), os.path.join(audits, ".current-run"))

    modes = {m: sum(1 for x, _ in shards if x == m) for m in ("flat", "tree")}
    print(json.dumps({"run_dir": run_dir, "target": target, "excludes": excludes,
                      "shards": len(shards), "by_mode": modes, "survey": notes},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
