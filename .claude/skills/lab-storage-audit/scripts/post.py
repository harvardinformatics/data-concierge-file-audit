#!/usr/bin/env python3
"""Post-process a finished scan into <run>/audit.sqlite.

Stages (default for the run's mode, in this order):
  merge     shards/*.tsv.gz -> tables dirs, files, users   (fails if any shard lacks .done)
  dupes     file-level duplicates, hardlinks, parent-dir overlap   (reads file slices)
  rollup    recursive dir totals + newest mtime + Merkle signature; exact dir duplicates
  classify  rules.toml -> type/ext stats, raw-data locations, dir markers
  peek      <= --max-peeks representative files, <= --peek-bytes each -> <run>/peeks.jsonl
  summarize capped aggregates for the report -> <run>/summary.json

Defaults come from the run's mode (plan.py --mode; MODE_DEFAULTS below):
  deep    all stages; dupes from 1 MiB
  survey  no peek; dupes only for files >= 256 MiB (provisional threshold,
          to calibrate from summary.size_histogram); no duplicate dir trees
Explicit options override the mode's defaults.

Writes <run>/post.DONE (stats JSON) or <run>/post.FAILED.
--workdir builds the database on node-local disk and copies it back at the end
(SQLite on Lustre is slow); default is to work in the run directory.
--show prints a short human-readable digest of the database (dev aid).
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify  # noqa: E402
import common as C  # noqa: E402
import dupes  # noqa: E402
import peek  # noqa: E402
import summary  # noqa: E402
from scan import DIR_COLS, FILE_COLS, expand_shards  # noqa: E402

STAGES = ("merge", "dupes", "rollup", "classify", "peek", "summarize")
BATCH = 50_000
MODE_DEFAULTS = {
    "deep": {"stages": STAGES, "min_size": 1 << 20, "max_pair_dirs": 50, "tree_dups": True},
    # survey hashes only big files, so small-file trees would match on names and sizes alone
    "survey": {"stages": ("merge", "dupes", "rollup", "classify", "summarize"),
               "min_size": 256 << 20, "max_pair_dirs": 20, "tree_dups": False},
}
DB_PER_GZ = 24          # audit.sqlite measured at 16-21x the gzipped shard TSVs; margin on top


def _db_text(s: str):
    """(text, raw_bytes_or_None) for a possibly non-UTF-8 filename."""
    try:
        s.encode("utf-8")
        return s, None
    except UnicodeEncodeError:
        return C.fs_str(s), os.fsencode(s)


def _int(v: str):
    return int(v) if v not in ("", None) else None


def _split(rel: str) -> tuple[str, str]:
    d, _, n = rel.rpartition("/")
    return (d or "."), n


def _depth(rel: str) -> int:
    return 0 if rel == "." else rel.count("/") + 1


# --------------------------------------------------------------------- merge

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE dirs(
  path TEXT PRIMARY KEY, path_raw BLOB, parent TEXT, name TEXT, depth INT, shard TEXT,
  status TEXT, uid INT, gid INT, mode TEXT, mtime INT,
  n_entries INT, n_files INT, n_subdirs INT, bytes_direct INT,
  bytes_total INT, files_total INT, dirs_total INT, newest_mtime INT, activity_mtime INT,
  problem_dirs_below INT, complete INT, sig TEXT);
CREATE TABLE files(
  id INTEGER PRIMARY KEY, dir TEXT, dir_raw BLOB, name TEXT, name_raw BLOB, ext TEXT,
  kind TEXT, size INT, blocks INT, mtime INT, uid INT, gid INT, dev INT, ino INT,
  nlink INT, mode TEXT, sensitive INT, link TEXT);
CREATE TABLE users(uid INTEGER PRIMARY KEY, name TEXT, in_target_group INT);
"""


def unfinished_shards(run_dir: str) -> list[str]:
    return [sid for sid, _, _ in expand_shards(run_dir)
            if not os.path.exists(os.path.join(run_dir, "shards", f"{sid}.done"))]


def check_space(run_dir: str, work: str) -> dict:
    """Refuse to merge when the DB (about DB_PER_GZ x the shard TSVs) won't fit."""
    sdir = os.path.join(run_dir, "shards")
    gz = sum(e.stat().st_size for e in os.scandir(sdir) if e.name.endswith(".tsv.gz"))
    need = gz * DB_PER_GZ
    out = {"shard_gz_bytes": gz, "db_estimate_bytes": need}
    for where in {os.path.dirname(work), run_dir}:
        free = shutil.disk_usage(where).free
        if free < need:
            raise RuntimeError(f"not enough space in {where}: ~{_h(need)} needed for "
                               f"audit.sqlite, {_h(free)} free")
    return out


def merge(db: sqlite3.Connection, run_dir: str, run: dict) -> dict:
    shards = expand_shards(run_dir)
    cur = db.cursor()
    cur.executescript(SCHEMA)
    cur.executemany("INSERT INTO meta VALUES (?,?)",
                    [(k, json.dumps(v)) for k, v in run.items()])
    n_files = n_dirs = 0
    for sid, _mode, _roots in shards:
        with C.open_tsv_gz_read(os.path.join(run_dir, "shards", f"{sid}.dirs.tsv.gz")) as fh:
            assert next(fh).rstrip("\n").split("\t") == DIR_COLS
            rows = []
            for line in fh:
                v = line.rstrip("\n").split("\t")
                rel = C.unesc(v[0])
                path, raw = _db_text(rel)
                parent = None if rel == "." else _db_text(_split(rel)[0])[0]
                name = _db_text(_split(rel)[1])[0] if rel != "." else "."
                rows.append((path, raw, parent, name, _depth(rel), sid, v[1],
                             _int(v[2]), _int(v[3]), v[4], _int(v[5]),
                             _int(v[6]), _int(v[7]), _int(v[8]), _int(v[9])))
            cur.executemany("""INSERT OR REPLACE INTO dirs(path, path_raw, parent, name, depth,
                shard, status, uid, gid, mode, mtime, n_entries, n_files, n_subdirs,
                bytes_direct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            n_dirs += len(rows)
        with C.open_tsv_gz_read(os.path.join(run_dir, "shards", f"{sid}.files.tsv.gz")) as fh:
            assert next(fh).rstrip("\n").split("\t") == FILE_COLS
            rows = []
            for line in fh:
                v = line.rstrip("\n").split("\t")
                d, n = _split(C.unesc(v[0]))
                dt, draw = _db_text(d)
                nt, nraw = _db_text(n)
                link = C.fs_str(C.unesc(v[12])) if len(v) > 12 and v[12] else None
                rows.append((dt, draw, nt, nraw, C.ext_of(nt), v[1],
                             *map(_int, v[2:10]), v[10] or None, _int(v[11]) or 0, link))
                if len(rows) >= BATCH:
                    cur.executemany(_FILE_INSERT, rows)
                    n_files += len(rows)
                    rows = []
            cur.executemany(_FILE_INSERT, rows)
            n_files += len(rows)
    cur.executescript("""
        CREATE INDEX files_dir ON files(dir);
        CREATE INDEX files_di ON files(dev, ino);
        CREATE INDEX dirs_parent ON dirs(parent);
    """)

    # uid -> user name; membership in the target's group
    tgid = os.lstat(run["target"]).st_gid
    try:
        g = grp.getgrgid(tgid)
        members, gname = set(g.gr_mem), g.gr_name
    except KeyError:
        members, gname = set(), str(tgid)
    uids = {r[0] for r in cur.execute(
        "SELECT DISTINCT uid FROM files WHERE uid IS NOT NULL "
        "UNION SELECT DISTINCT uid FROM dirs WHERE uid IS NOT NULL")}
    urows = []
    for uid in sorted(uids):
        try:
            pw = pwd.getpwuid(uid)
            urows.append((uid, pw.pw_name, int(pw.pw_name in members or pw.pw_gid == tgid)))
        except KeyError:
            urows.append((uid, None, 0))            # deleted account: orphaned data
    cur.executemany("INSERT INTO users VALUES (?,?,?)", urows)
    cur.execute("INSERT INTO meta VALUES ('target_group', ?)", (json.dumps(gname),))
    db.commit()
    return {"dirs": n_dirs, "files": n_files, "users": len(urows),
            "unknown_uids": sum(1 for u in urows if u[1] is None)}


_FILE_INSERT = """INSERT INTO files(dir, dir_raw, name, name_raw, ext, kind, size, blocks,
    mtime, uid, gid, dev, ino, nlink, mode, sensitive, link)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


# -------------------------------------------------------------------- rollup

def rollup(db: sqlite3.Connection, min_dir_bytes: int, tree_dups: bool = True) -> dict:
    """Bottom-up totals and Merkle signatures; exact duplicate directory trees
    (only with tree_dups).

    A dir's signature covers its entries' names, kinds and sizes, the partial
    hash of each file that was hashed by the dupes stage, symlink targets, and
    its child dirs' signatures. Dirs with any unreadable/error/excluded part
    are 'incomplete' and never match.
    """
    cur = db.cursor()
    has_hashes = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE name='hashes'").fetchone() is not None
    q = ("SELECT f.dir, f.name, f.kind, f.size, f.mtime, f.link, "
         + ("h.phash " if has_hashes else "NULL ")
         + "FROM files f "
         + ("LEFT JOIN hashes h ON h.dev = f.dev AND h.ino = f.ino " if has_hashes else "")
         + "ORDER BY f.dir")

    own: dict[str, tuple[bytes, int, int, int]] = {}   # dir -> (digest, files, bytes, newest)

    def close(d, entries, nf, nb, newest):
        h = hashlib.blake2b(digest_size=16)
        for e in sorted(entries):
            h.update(repr(e).encode("utf-8", "surrogateescape"))
            h.update(b"\0")
        own[d] = (h.digest(), nf, nb, newest)

    cur_dir, entries, nf, nb, newest = None, [], 0, 0, 0
    for d, name, kind, size, mtime, link, phash in cur.execute(q):
        if d != cur_dir:
            if cur_dir is not None:
                close(cur_dir, entries, nf, nb, newest)
            cur_dir, entries, nf, nb, newest = d, [], 0, 0, 0
        if kind == "f":
            entries.append(("f", name, size, phash or ""))
            nf += 1
            nb += size or 0
            newest = max(newest, mtime or 0)
        elif kind == "l":
            entries.append(("l", name, link or ""))
        else:
            entries.append((kind or "?", name))
    if cur_dir is not None:
        close(cur_dir, entries, nf, nb, newest)

    # newest_mtime is the newest *file* below (when the data last changed). activity_mtime
    # also counts dir mtimes, which move whenever an entry is added, renamed or removed
    # (when anyone last touched the tree). A reorganisation moves only the second.
    if "activity_mtime" not in {r[1] for r in cur.execute("PRAGMA table_info(dirs)")}:
        cur.execute("ALTER TABLE dirs ADD COLUMN activity_mtime INT")   # DBs from before 10-09
    dirs = cur.execute("SELECT path, parent, name, depth, status, mtime FROM dirs "
                       "ORDER BY depth DESC").fetchall()
    kids: dict[str, list] = {}
    updates = []
    empty = hashlib.blake2b(b"", digest_size=16).digest()
    for path, parent, name, _depth_, status, mtime in dirs:
        dig, nf, nb, newest = own.get(path, (empty, 0, 0, 0))
        active = max(newest, mtime or 0)
        ok = status == "ok"
        problems = 0 if ok or status == "excluded" else 1
        n_sub = 0
        child_parts = []
        complete = ok
        for cname, csig, cf, cb, cn, ca, cnd, cprob, ccomplete in kids.pop(path, []):
            nf += cf
            nb += cb
            newest = max(newest, cn)
            active = max(active, ca)
            n_sub += 1 + cnd
            problems += cprob
            complete = complete and ccomplete
            child_parts.append((cname, csig))
        sig = None
        if complete:
            h = hashlib.blake2b(dig, digest_size=16)
            for cname, csig in sorted(child_parts):
                h.update(repr(("d", cname)).encode("utf-8", "surrogateescape"))
                h.update(csig)
            sig = h.digest()
        updates.append((nb, nf, n_sub, newest, active, problems, int(complete),
                        sig.hex() if sig else None, path))
        if parent is not None:
            kids.setdefault(parent, []).append(
                (name, sig or b"", nf, nb, newest, active, n_sub, problems, complete))
    cur.executemany("""UPDATE dirs SET bytes_total=?, files_total=?, dirs_total=?,
                       newest_mtime=?, activity_mtime=?, problem_dirs_below=?, complete=?,
                       sig=? WHERE path=?""",
                    updates)
    cur.execute("CREATE INDEX IF NOT EXISTS dirs_sig ON dirs(sig)")

    # exact duplicate trees; keep only maximal ones (parent not itself duplicated)
    cur.executescript("DROP TABLE IF EXISTS dir_dup_groups; DROP TABLE IF EXISTS dir_dup_members;")
    if not tree_dups:
        db.commit()
        return {"dirs": len(dirs), "tree_dups": False, "used_hashes": has_hashes}
    cur.execute("""CREATE TEMP TABLE dupsig AS
        SELECT sig, COUNT(*) AS n, MAX(bytes_total) AS bytes, MAX(files_total) AS files
        FROM dirs WHERE sig IS NOT NULL AND files_total > 0 AND bytes_total >= ?
        GROUP BY sig HAVING COUNT(*) > 1""", (min_dir_bytes,))
    cur.execute("""CREATE TABLE dir_dup_groups AS
        SELECT ROW_NUMBER() OVER (ORDER BY s.bytes * (s.n - 1) DESC) AS gid,
               s.sig, s.n AS n_dirs, s.bytes AS bytes_each, s.files AS files_each,
               s.bytes * (s.n - 1) AS reclaimable
        FROM dupsig s
        WHERE EXISTS (SELECT 1 FROM dirs d LEFT JOIN dirs p ON p.path = d.parent
                      WHERE d.sig = s.sig
                        AND (p.sig IS NULL OR p.sig NOT IN (SELECT sig FROM dupsig)))""")
    cur.execute("""CREATE TABLE dir_dup_members AS
        SELECT g.gid, d.path FROM dir_dup_groups g JOIN dirs d ON d.sig = g.sig""")
    db.commit()
    r = cur.execute("SELECT COUNT(*), COALESCE(SUM(reclaimable),0) FROM dir_dup_groups").fetchone()
    return {"dirs": len(dirs), "dir_dup_groups": r[0], "dir_dup_reclaimable": r[1],
            "min_dir_bytes": min_dir_bytes, "used_hashes": has_hashes}


# ---------------------------------------------------------------------- show

_h = C.human_bytes


def show(db: sqlite3.Connection, limit: int = 10) -> None:
    cur = db.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master")}
    tot = cur.execute("SELECT COUNT(*), SUM(size) FROM files WHERE kind='f'").fetchone()
    print(f"files: {tot[0]}  bytes: {_h(tot[1])}")
    print("dir status:", dict(cur.execute("SELECT status, COUNT(*) FROM dirs GROUP BY status")))
    print("\nnon-ok dirs (owner, dir mtime):")
    for path, status, user, uid, mtime in cur.execute(
            """SELECT d.path, d.status, u.name, d.uid, d.mtime FROM dirs d
               LEFT JOIN users u ON u.uid = d.uid
               WHERE d.status NOT IN ('ok') ORDER BY d.path LIMIT ?""", (limit * 3,)):
        print(f"  {status:12} {user or uid!s:12} {time.strftime('%Y-%m-%d', time.localtime(mtime or 0))}  {path}")
    if "dup_groups" in tables:
        g = cur.execute("SELECT COUNT(*), SUM(reclaimable) FROM dup_groups").fetchone()
        print(f"\nfile dup groups: {g[0]}  reclaimable: {_h(g[1])}")
        for gid, size, n, conf in cur.execute(
                "SELECT gid, size, n_phys, confirmed FROM dup_groups ORDER BY gid LIMIT ?", (limit,)):
            paths = [f"{d}/{n_}" for d, n_ in cur.execute(
                """SELECT f.dir, f.name FROM dup_members m JOIN files f
                   ON f.dev=m.dev AND f.ino=m.ino WHERE m.gid=? LIMIT 4""", (gid,)).fetchall()]
            print(f"  #{gid} {_h(size)} x{n} {'confirmed' if conf else 'likely'}: {paths}")
        print("\ntop dir overlaps (shared dup bytes):")
        for a, b, sb, sf, ba, bb in cur.execute(
                """SELECT o.dir_a, o.dir_b, o.shared_bytes, o.shared_files,
                          da.bytes_direct, db.bytes_direct FROM dir_overlap o
                   LEFT JOIN dirs da ON da.path=o.dir_a LEFT JOIN dirs db ON db.path=o.dir_b
                   ORDER BY o.shared_bytes DESC LIMIT ?""", (limit,)):
            fa = sb / ba if ba else 0
            fb = sb / bb if bb else 0
            print(f"  {_h(sb)} in {sf} files ({fa:.0%} of A, {fb:.0%} of B)\n    A {a}\n    B {b}")
        hl = cur.execute("SELECT COUNT(*), SUM(saved) FROM hardlink_groups WHERE n_paths > 1").fetchone()
        print(f"\nhardlink groups within target: {hl[0]}  space saved: {_h(hl[1])}")
    if "dir_dup_groups" in tables:
        print("\nexact duplicate dir trees:")
        for gid, n, be, fe in cur.execute(
                "SELECT gid, n_dirs, bytes_each, files_each FROM dir_dup_groups ORDER BY gid LIMIT ?",
                (limit,)):
            mem = [r[0] for r in cur.execute(
                "SELECT path FROM dir_dup_members WHERE gid=? LIMIT 5", (gid,))]
            print(f"  #{gid} {n} copies of {_h(be)} / {fe} files: {mem}")
    if "type_stats" in tables:
        print("\ntypes by bytes:")
        for ft, cat, n, b, nd in cur.execute(
                "SELECT ftype, category, n_files, bytes, n_dirs FROM type_stats LIMIT ?", (limit,)):
            print(f"  {_h(b):>10}  {n:>8} files  {nd:>5} dirs  [{cat}] {ft}")
        print("\nraw-data locations:")
        for d, ft, n, b in cur.execute(
                "SELECT dir, ftype, n_files, bytes FROM raw_locations LIMIT ?", (limit,)):
            print(f"  {_h(b):>10}  {n:>6} {ft}: {d}")
        print("\ndir markers:")
        for p, mk in cur.execute("SELECT path, marker FROM dir_markers LIMIT ?", (limit,)):
            print(f"  {mk}: {p}")


# ---------------------------------------------------------------------- main

def run_post(run_dir: str, stages=None, min_size=None, chunk=1 << 20,
             confirm=False, workers=1, workdir=None, min_dir_bytes=1 << 20,
             rules_path=None, max_peeks=300, peek_bytes=64 << 10) -> dict:
    """stages=None / min_size=None take the run mode's defaults (MODE_DEFAULTS)."""
    run_dir = os.path.realpath(run_dir)
    run = C.load_run(run_dir)
    C.check_run_dir_safe(run_dir, run["target"], run["excludes"])
    mode = run.get("mode", "deep")
    defaults = MODE_DEFAULTS[mode]
    stages = defaults["stages"] if stages is None else stages
    min_size = defaults["min_size"] if min_size is None else min_size
    C.clear_sentinel(run_dir, "post.FAILED")
    C.clear_sentinel(run_dir, "post.DONE")
    final = C.out_path(run_dir, "audit.sqlite")
    if "merge" in stages:
        missing = unfinished_shards(run_dir)
        if missing:
            raise RuntimeError(f"{len(missing)} shard(s) not done: {missing[:20]}")
        for p in (final, final + ".tmp"):
            if os.path.exists(p):
                os.remove(p)
    elif not os.path.exists(final):
        raise RuntimeError("no audit.sqlite; run the merge stage first")

    work = final
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        work = os.path.join(workdir, "audit.sqlite")
        if os.path.exists(work):
            os.remove(work)
        if "merge" not in stages:
            shutil.copyfile(final, work)

    t0 = time.monotonic()
    stats: dict = {"mode": mode, "stages": list(stages)}
    if "merge" in stages:
        stats["space"] = check_space(run_dir, work)
    db = sqlite3.connect(work)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=FILE")         # sorts spill to SQLITE_TMPDIR, not RAM
    db.execute("PRAGMA cache_size=-262144")      # 256 MiB page cache
    if "merge" not in stages:                    # rerun of later stages: keep earlier stats
        prev = db.execute("SELECT value FROM meta WHERE key='post_stats'").fetchone()
        old = json.loads(prev[0]) if prev else {}
        stats.update({k: v for k, v in old.items()
                      if k == "space" or (k in STAGES and k not in stages)})
    for st in STAGES:
        if st not in stages:
            continue
        t = time.monotonic()
        C.log(f"stage {st} ...")
        if st == "merge":
            stats[st] = merge(db, run_dir, run)
        elif st == "dupes":
            stats[st] = dupes.run(db, run["target"], min_size, chunk, confirm, workers,
                                  defaults["max_pair_dirs"])
        elif st == "rollup":
            stats[st] = rollup(db, min_dir_bytes, defaults["tree_dups"])
        elif st == "classify":
            rules = classify.load_rules(rules_path or str(C.SCRIPTS_DIR / "rules.toml"))
            stats[st] = classify.run(db, rules)
        elif st == "peek":
            stats[st] = peek.run(db, run_dir, run["target"], max_peeks, peek_bytes)
        elif st == "summarize":
            stats[st] = summary.run(db, run_dir, run, stats)
        stats[st]["elapsed_s"] = round(time.monotonic() - t, 3)
        C.log(f"stage {st}: {stats[st]}")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('post_stats', ?)", (json.dumps(stats),))
    db.commit()
    db.close()
    if work != final:
        shutil.copyfile(work, final + ".tmp")
        os.replace(final + ".tmp", final)
        os.remove(work)
    stats["elapsed_s"] = round(time.monotonic() - t0, 3)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--stages", help=f"comma list from {','.join(STAGES)} "
                    "(default: the run mode's stages)")
    ap.add_argument("--min-size", help="smallest file considered for dupes "
                    "(default: 1MiB deep, 256MiB survey)")
    ap.add_argument("--chunk", default="1MiB", help="partial-hash slice size (x3)")
    ap.add_argument("--min-dir-bytes", default="1MiB", help="smallest dir tree reported as dup")
    ap.add_argument("--confirm", action="store_true", help="full-hash duplicate candidates")
    ap.add_argument("--workers", type=int, default=0, help="default: allocated CPUs")
    ap.add_argument("--workdir", help="build the DB here (node-local disk), copy back")
    ap.add_argument("--rules", help="rules.toml path (default: next to this script)")
    ap.add_argument("--max-peeks", type=int, default=300, help="files sampled in total")
    ap.add_argument("--peek-bytes", default="64KiB", help="bytes read per sampled file")
    ap.add_argument("--show", action="store_true", help="print a digest of audit.sqlite and exit")
    args = ap.parse_args(argv)

    run_dir = os.path.realpath(args.run)
    if args.show:
        db = sqlite3.connect(f"file:{C.out_path(run_dir, 'audit.sqlite')}?mode=ro", uri=True)
        show(db)
        return 0
    stages = None
    if args.stages:
        stages = [s.strip() for s in args.stages.split(",") if s.strip()]
        bad = [s for s in stages if s not in STAGES]
        if bad:
            C.die(f"unknown stage(s): {bad}")
    min_size = C.parse_size(args.min_size) if args.min_size else None
    try:
        stats = run_post(run_dir, stages, min_size, C.parse_size(args.chunk),
                         args.confirm, args.workers or C.default_workers(), args.workdir,
                         C.parse_size(args.min_dir_bytes), args.rules, args.max_peeks,
                         C.parse_size(args.peek_bytes))
    except Exception as ex:
        C.write_sentinel(run_dir, "post.FAILED", {"error": repr(ex),
                                                  "traceback": traceback.format_exc()})
        C.log(traceback.format_exc())
        return 1
    C.write_sentinel(run_dir, "post.DONE", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
