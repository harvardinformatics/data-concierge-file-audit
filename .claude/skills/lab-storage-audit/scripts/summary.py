"""summary.json: the compact, versioned aggregate the agent reports from (used by post.py).

Every list is capped, so the file stays small enough to read whole at any lab
size. Paths are target-relative. No file contents: peek details stay in
peeks.jsonl. Bump SUMMARY_VERSION on any structural change, and keep
references/report-template.md in sync.

"Pipeline work" means workflow scratch: a Nextflow `work/` next to a `.nextflow` dir,
any work/<xx>/<hash> task dir, or `.snakemake/`. Copies there are pipeline
intermediates, not raw data or real duplicates: a file group counts as a duplicate
only with >= 2 physical copies outside pipeline work, and work dirs are listed
separately in `pipeline_workdirs`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict

import common as C

SUMMARY_VERSION = 1
_WORK = re.compile(r"(^|/)(work/[0-9a-f]{2}/[0-9a-f]{30}|\.snakemake)(/|$)")


def pipeline_work(path: str) -> bool:
    return bool(_WORK.search(path))


def _rows(cur, sql, args=()) -> list[dict]:
    """Materialized, so callers can run other queries on `cur` while iterating."""
    rows = cur.execute(sql, args).fetchall()
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in rows]


def _first(rows: list[dict]) -> dict:
    return rows[0]


def _ancestors(path: str):
    """'a/b/c' -> a/b/c, a/b, a, . (the target root is '.')."""
    while path != ".":
        yield path
        path = path.rsplit("/", 1)[0] if "/" in path else "."
    yield "."


def build(db: sqlite3.Connection, run: dict, max_folders: int = 300) -> dict:
    cur = db.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master")}
    uname = {uid: (n, g) for uid, n, g in cur.execute("SELECT uid, name, in_target_group FROM users")}
    meta = dict(cur.execute("SELECT key, value FROM meta"))

    tot = _first(_rows(cur, """SELECT
        SUM(kind='f') AS files, SUM(kind='l') AS symlinks, SUM(kind NOT IN ('f','l')) AS other,
        COALESCE(SUM(CASE WHEN kind='f' THEN size END),0) AS bytes,
        COALESCE(SUM(blocks),0) * 512 AS allocated_bytes, SUM(sensitive) AS sensitive_files,
        MIN(CASE WHEN kind='f' THEN mtime END) AS oldest_mtime,
        MAX(CASE WHEN kind='f' THEN mtime END) AS newest_mtime FROM files"""))
    tot["dirs"] = cur.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]

    def owner(uid):
        n, g = uname.get(uid, (None, 0))
        return {"uid": uid, "user": n, "in_group": bool(g)}

    problem = [{**r, **owner(r.pop("uid"))} for r in _rows(cur, """
        SELECT path, status, uid, mode, mtime FROM dirs WHERE status != 'ok'
        ORDER BY status, path LIMIT 200""")]

    users = [{**owner(r.pop("uid")), **r} for r in _rows(cur, """
        SELECT uid, COUNT(*) AS files, SUM(size) AS bytes, MAX(mtime) AS newest_mtime
        FROM files WHERE kind='f' GROUP BY uid ORDER BY bytes DESC""")]

    workdirs = [r for r in _rows(cur, """
        SELECT d.path, m.marker, d.bytes_total AS bytes, d.files_total AS files, d.newest_mtime
        FROM dir_markers m JOIN dirs d
          ON d.path = (CASE WHEN m.path = '.' THEN '' ELSE m.path || '/' END)
                      || (CASE m.marker WHEN 'Nextflow launch dir' THEN 'work' ELSE '.snakemake' END)
        WHERE m.marker IN ('Nextflow launch dir', 'Snakemake working dir')
        ORDER BY d.bytes_total DESC LIMIT 100""")] if "dir_markers" in tables else []
    roots = [w["path"] for w in workdirs]

    def in_work(p: str) -> bool:
        """Inside workflow scratch: a work/ or .snakemake/ root, or a task dir below one."""
        return pipeline_work(p) or any(p == w or p.startswith(w + "/") for w in roots)
    db.create_function("pipeline_work", 1, lambda p: int(in_work(p or "")), deterministic=True)

    # folders: dirs within 2 levels of each root holding >= 0.1% of the bytes
    root_depth = {r: (0 if r == "." else r.count("/") + 1) for r in run["roots"]}
    floor = (tot["bytes"] or 0) // 1000
    folders = {}
    for r in _rows(cur, """SELECT path, depth, files_total AS files, bytes_total AS bytes,
                                  dirs_total AS dirs, newest_mtime, status, problem_dirs_below
                           FROM dirs WHERE bytes_total >= ? ORDER BY bytes_total DESC""", (floor,)):
        if any((p == "." or r["path"] == p or r["path"].startswith(p + "/"))
               and r["depth"] <= d + 2 for p, d in root_depth.items()):
            folders[r["path"]] = {**r, "by_category": defaultdict(int), "by_owner": defaultdict(int),
                                  "pipeline_work": in_work(r["path"])}
            if len(folders) >= max_folders:
                break
    if "file_class" in tables:
        for d, cat, uid, b in cur.execute("""SELECT f.dir, c.category, f.uid, SUM(f.size)
                FROM files f JOIN file_class c ON c.id = f.id GROUP BY f.dir, c.category, f.uid"""):
            for a in _ancestors(d):
                if a in folders:
                    folders[a]["by_category"][cat] += b
                    folders[a]["by_owner"][uid] += b
    for f in folders.values():          # lists, not dicts: summary.json is written sort_keys
        f["by_category"] = sorted(([c, b] for c, b in f["by_category"].items()),
                                  key=lambda x: -x[1])
        f["top_owners"] = [{**owner(u), "bytes": b} for u, b in
                           sorted(f.pop("by_owner").items(), key=lambda x: -x[1])[:3]]

    out = {
        "schema_version": SUMMARY_VERSION,
        "generated": C.now_iso(),
        "run": {k: run.get(k) for k in ("target", "roots", "excludes", "created", "created_by",
                                         "host", "one_fs")} | {
                "target_group": json.loads(meta.get("target_group", "null"))},
        "totals": tot,
        "dir_status": dict(cur.execute("SELECT status, COUNT(*) FROM dirs GROUP BY status")),
        "problem_dirs": problem,
        "users": users,
        "folders": list(folders.values()),
        "pipeline_workdirs": workdirs,
        "age_by_year": list(_rows(cur, """SELECT strftime('%Y', mtime, 'unixepoch') AS year,
            COUNT(*) AS files, SUM(size) AS bytes FROM files WHERE kind='f'
            GROUP BY year ORDER BY year""")),
    }
    if "type_stats" in tables:
        out["by_category"] = list(_rows(cur, """SELECT category, SUM(n_files) AS files,
            SUM(bytes) AS bytes FROM type_stats GROUP BY category ORDER BY bytes DESC"""))
        out["types"] = list(_rows(cur, "SELECT * FROM type_stats"))
        out["unknown_exts"] = list(_rows(cur, """SELECT ext, n_files, bytes, n_dirs, oldest, newest
            FROM ext_stats WHERE category='unknown' ORDER BY bytes DESC LIMIT 40"""))
        out["raw_locations"] = _rows(cur, """SELECT * FROM raw_locations
            WHERE NOT pipeline_work(dir) ORDER BY bytes DESC LIMIT 50""")
        out["raw_locations_outside_pipeline_work"] = _first(_rows(cur, """SELECT COUNT(*) AS dirs,
            COALESCE(SUM(bytes),0) AS bytes FROM raw_locations WHERE NOT pipeline_work(dir)"""))
        out["markers"] = list(_rows(cur, "SELECT * FROM dir_markers ORDER BY path"))
    if "dup_groups" in tables:
        # A group is a real duplicate only if >= 2 physical copies sit outside pipeline
        # work dirs; a raw file plus its work/ staging copy is a pipeline intermediate.
        groups = []
        for g in _rows(cur, """SELECT gid, size, n_phys AS copies, n_paths, reclaimable, confirmed
                              FROM dup_groups ORDER BY gid"""):
            members = cur.execute(
                """SELECT CASE WHEN f.dir='.' THEN f.name ELSE f.dir || '/' || f.name END,
                          f.dev, f.ino
                   FROM dup_members m JOIN files f ON f.dev=m.dev AND f.ino=m.ino AND f.kind='f'
                   WHERE m.gid=? ORDER BY f.dir, f.name""", (g["gid"],)).fetchall()
            outside = [(p, di) for p, *di in members if not in_work(p)]
            n_out = len({tuple(di) for _p, di in outside})
            g["copies_outside_pipeline_work"] = n_out
            g["reclaimable_outside_pipeline_work"] = g["size"] * max(0, n_out - 1)
            g["paths"] = [p for p, _di in outside][:3]
            groups.append(g)
        real = sorted((g for g in groups if g["copies_outside_pipeline_work"] >= 2),
                      key=lambda g: -g["reclaimable_outside_pipeline_work"])
        out["duplicates"] = {
            "file_groups": len(groups),
            "reclaimable_bytes": sum(g["reclaimable"] for g in groups),
            "file_groups_outside_pipeline_work": len(real),
            "reclaimable_bytes_outside_pipeline_work": sum(
                g["reclaimable_outside_pipeline_work"] for g in real),
            "confirmed": bool(groups) and all(g["confirmed"] for g in groups),
            "top_groups": real[:25],
            "dir_overlap": list(_rows(cur, """
                SELECT o.dir_a, o.dir_b, o.shared_bytes, o.shared_files,
                       ROUND(1.0 * o.shared_bytes / NULLIF(da.bytes_direct, 0), 3) AS frac_a,
                       ROUND(1.0 * o.shared_bytes / NULLIF(db.bytes_direct, 0), 3) AS frac_b
                FROM dir_overlap o LEFT JOIN dirs da ON da.path=o.dir_a
                LEFT JOIN dirs db ON db.path=o.dir_b
                WHERE NOT (pipeline_work(o.dir_a) OR pipeline_work(o.dir_b))
                ORDER BY o.shared_bytes DESC LIMIT 20""")),
            "hardlinks": _first(_rows(cur, """SELECT COUNT(*) AS groups, COALESCE(SUM(saved),0)
                AS saved_bytes FROM hardlink_groups WHERE n_paths > 1""")),
        }
    if "dir_dup_groups" in tables:
        trees = []
        for g in _rows(cur, """SELECT gid, n_dirs AS copies, bytes_each, files_each, reclaimable
                              FROM dir_dup_groups ORDER BY gid"""):
            g["paths"] = [r[0] for r in cur.execute(
                "SELECT path FROM dir_dup_members WHERE gid=? ORDER BY path", (g["gid"],))
                if not in_work(r[0])]
            g["copies"] = len(g["paths"])
            g["reclaimable"] = g["bytes_each"] * max(0, g["copies"] - 1)
            if g["copies"] >= 2:
                trees.append(g)
        trees.sort(key=lambda t: -t["reclaimable"])
        out.setdefault("duplicates", {})["dir_trees"] = trees[:20]
    if "peeks" in tables:
        out["peeks"] = {
            "file": "peeks.jsonl",
            "n": cur.execute("SELECT COUNT(*) FROM peeks").fetchone()[0],
            "by_format": dict(cur.execute(
                "SELECT format, COUNT(*) FROM peeks GROUP BY format ORDER BY 2 DESC")),
            "injection_suspects": [r[0] for r in cur.execute(
                "SELECT path FROM peeks WHERE injection_suspect ORDER BY path")],
        }
    return out


def run(db: sqlite3.Connection, run_dir: str, run: dict, post_stats: dict) -> dict:
    s = build(db, run)
    s["post_stats"] = post_stats
    C.write_json_atomic(C.out_path(run_dir, "summary.json"), s)
    d = s.get("duplicates", {})
    return {"folders": len(s["folders"]), "problem_dirs": len(s["problem_dirs"]),
            "pipeline_workdirs": len(s["pipeline_workdirs"]),
            "reclaimable_outside_pipeline_work": d.get("reclaimable_bytes_outside_pipeline_work")}
