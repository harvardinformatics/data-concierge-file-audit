"""summary.json: the compact, versioned aggregate the agent reports from (used by post.py).

Every list is capped, so the file stays small enough to read whole at any lab
size (target: under ~50 KB, about 15K tokens; a larger file logs a warning).
Paths are target-relative, dates are YYYY-MM-DD. No file contents: peek details
stay in peeks.jsonl. Bump SUMMARY_VERSION on any structural change, and keep
references/report-template.md and survey-template.md in sync.

Tables are {"cols": [...], "rows": [[...], ...]}: column names are written once,
not once per row. The file has one table row, or one key of an object, per line, so
the agent can read it with the Read tool (in two parts at lab scale). `capped` says,
for every shortened list, how many rows it shows out of how many there are.

"Pipeline work" means workflow scratch: a Nextflow `work/` next to a `.nextflow` dir,
any work/<xx>/<hash> task dir, or `.snakemake/`. Copies there are pipeline
intermediates, not raw data or real duplicates: a file group counts as a duplicate
only with >= 2 physical copies outside pipeline work, and work dirs are listed
separately in `pipeline_workdirs`.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict

import common as C

SUMMARY_VERSION = 4
WARN_BYTES = 50_000
FOLDER_DEPTH = {"deep": 2, "survey": 3}      # folder rows go this many levels below each root
MAX_LEVEL1 = 60                              # level-1 folders always listed (the rest summed)
DETAIL_ROWS = 15                             # largest level-2 folders always listed
OLD_YEARS = {"deep": 2, "survey": 3}         # "old" = not modified in this many years
SPLIT_AREA = 0.5                             # an area holding more than this share splits
SYSTEM_UID = 1000                            # below: system accounts (root), not members
_WORK = re.compile(r"(^|/)(work/[0-9a-f]{2}/[0-9a-f]{30}|\.snakemake)(/|$)")
# Sensitive-name matches here are almost always package files (certifi's *.pem,
# a library's secret*.py), not a lab's credentials.
_ENV_MARKERS = ("Conda environment", "Python virtualenv")
_CODE_SEGMENTS = frozenset({"site-packages", "dist-packages", "node_modules", "pkgs", ".git"})


def pipeline_work(path: str) -> bool:
    return bool(_WORK.search(path))


def _date(ts) -> str | None:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None


def _table(cols: list[str], rows) -> dict:
    return {"cols": cols, "rows": [list(r) for r in rows]}


def _compact(v) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True)


def dumps(obj, ind: str = "") -> str:
    """JSON with one table row, or one key of an object, per line. Lists outside
    tables stay on one line."""
    if isinstance(obj, dict) and set(obj) == {"cols", "rows"}:
        sep = ",\n" + ind + "  "
        rows = sep.join(_compact(r) for r in obj["rows"])
        rows = f"\n{ind}  {rows}\n{ind}" if rows else ""
        return f'{{"cols":{_compact(obj["cols"])},"rows":[{rows}]}}'
    if isinstance(obj, dict) and obj:
        inner = ind + " "
        return "{\n" + ",\n".join(f"{inner}{json.dumps(k)}:{dumps(v, inner)}"
                                   for k, v in sorted(obj.items())) + "\n" + ind + "}"
    return _compact(obj)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def _ancestors(path: str):
    """'a/b/c' -> a/b/c, a/b, a, . (the target root is '.')."""
    while path != ".":
        yield path
        path = path.rsplit("/", 1)[0] if "/" in path else "."
    yield "."


def _rollup_raw(rows: list[tuple]) -> list[list]:
    """Raw-data dirs, one level rolled up: siblings holding the same type merge into
    their parent. rows: (dir, ftype, files, bytes, oldest, newest)."""
    by_parent = defaultdict(list)
    for r in rows:
        parent = r[0].rsplit("/", 1)[0] if "/" in r[0] else "."
        by_parent[(parent, r[1])].append(r)
    out = []
    for (parent, ftype), rs in by_parent.items():
        if len(rs) == 1:
            d, _ft, nf, nb, old, new = rs[0]
            out.append([d, ftype, 1, nf, nb, old, new])
        else:
            out.append([parent, ftype, len(rs), sum(r[2] for r in rs), sum(r[3] for r in rs),
                        min(r[4] for r in rs), max(r[5] for r in rs)])
    out.sort(key=lambda r: -r[4])
    return out


def build(db: sqlite3.Connection, run: dict, max_folders: int = 120) -> dict:
    cur = db.cursor()
    mode = run.get("mode", "deep")
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master")}
    uname = {uid: (n, g) for uid, n, g in cur.execute("SELECT uid, name, in_target_group FROM users")}
    meta = dict(cur.execute("SELECT key, value FROM meta"))

    def who(uid) -> str:
        n = uname.get(uid, (None, 0))[0]
        return n or f"uid:{uid}"

    def in_group(uid) -> bool:
        return bool(uname.get(uid, (None, 0))[1])

    def former(uid) -> bool:
        """A past member: an account not in the lab's group (or deleted), not a system one."""
        return uid is not None and uid >= SYSTEM_UID and not in_group(uid)

    r = cur.execute("""SELECT SUM(kind='f'), SUM(kind='l'), SUM(kind NOT IN ('f','l')),
        COALESCE(SUM(CASE WHEN kind='f' THEN size END),0), COALESCE(SUM(blocks),0) * 512,
        MIN(CASE WHEN kind='f' THEN mtime END), MAX(CASE WHEN kind='f' THEN mtime END)
        FROM files""").fetchone()
    tot = {"files": r[0] or 0, "symlinks": r[1] or 0, "other": r[2] or 0, "bytes": r[3],
           "allocated_bytes": r[4], "oldest": _date(r[5]), "newest": _date(r[6]),
           "dirs": cur.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]}

    # problem dirs, grouped by (parent, status): 5 unreadable home dirs are one row
    groups: dict = {}
    n_problem = 0
    for path, parent, name, status, uid, mtime in cur.execute(
            "SELECT path, parent, name, status, uid, mtime FROM dirs WHERE status != 'ok' "
            "ORDER BY path"):
        n_problem += 1
        g = groups.setdefault((parent or ".", status), {"n": 0, "ex": [], "own": Counter(),
                                                        "out": set(), "new": 0})
        g["n"] += 1
        if len(g["ex"]) < 2:
            g["ex"].append(name if parent is not None else path)
        if uid is not None:
            g["own"][who(uid)] += 1
            if not in_group(uid):
                g["out"].add(uid)
        g["new"] = max(g["new"], mtime or 0)
    ranked = sorted(groups.items(), key=lambda kv: (kv[0][1] == "excluded", -kv[1]["n"], kv[0]))
    problem = {"n": n_problem, "n_groups": len(groups), "groups": _table(
        ["parent", "status", "n", "examples", "owners", "owners_outside_group", "newest"],
        ([p, st, g["n"], g["ex"], [u for u, _ in g["own"].most_common(3)], len(g["out"]),
          _date(g["new"])] for (p, st), g in ranked[:30]))}

    user_rows = cur.execute("""SELECT uid, COUNT(*), SUM(size), MAX(mtime) FROM files
        WHERE kind='f' GROUP BY uid ORDER BY SUM(size) DESC""").fetchall()
    users = {"n": len(user_rows),
             "active": sum(1 for u, *_ in user_rows if in_group(u)),
             "former": sum(1 for u, *_ in user_rows if former(u)),
             "former_bytes": sum(nb or 0 for u, _, nb, _ in user_rows if former(u)),
             "table": _table(
        ["user", "uid", "in_group", "files", "bytes", "newest"],
        ([None if who(u).startswith("uid:") else who(u), u, in_group(u), nf, nb, _date(nw)]
         for u, nf, nb, nw in user_rows[:50]))}

    wd_rows = cur.execute("""
        SELECT d.path, m.marker, d.bytes_total, d.files_total, d.newest_mtime
        FROM dir_markers m JOIN dirs d
          ON d.path = (CASE WHEN m.path = '.' THEN '' ELSE m.path || '/' END)
                      || (CASE m.marker WHEN 'Nextflow launch dir' THEN 'work' ELSE '.snakemake' END)
        WHERE m.marker IN ('Nextflow launch dir', 'Snakemake working dir')
        ORDER BY d.bytes_total DESC""").fetchall() if "dir_markers" in tables else []
    roots = [w[0] for w in wd_rows]

    def in_work(p: str) -> bool:
        """Inside workflow scratch: a work/ or .snakemake/ root, or a task dir below one."""
        return pipeline_work(p) or any(p == w or p.startswith(w + "/") for w in roots)
    db.create_function("pipeline_work", 1, lambda p: int(in_work(p or "")), deterministic=True)
    workdirs = {"n": len(wd_rows), "bytes": sum(w[2] or 0 for w in wd_rows),
                "files": sum(w[3] or 0 for w in wd_rows), "top": _table(
                    ["path", "marker", "bytes", "files", "newest"],
                    ([p, m, b, f, _date(n)] for p, m, b, f, n in wd_rows[:25]))}

    # folders: dirs up to folder_depth below each root. Always every level-1 folder (up to
    # MAX_LEVEL1; the rest are summed in `folders_other`) and the DETAIL_ROWS largest level-2
    # folders, so the report's Areas tables (render.py) are fixed; then dirs holding >= 0.1%
    # of the bytes or of the files, ranked by the larger share, up to max_folders rows.
    folder_depth = FOLDER_DEPTH.get(mode, 2)
    root_depth = {p: (0 if p == "." else p.count("/") + 1) for p in run["roots"]}
    tb, tf = max(tot["bytes"], 1), max(tot["files"], 1)

    def level(path: str, depth: int):
        for p, d in root_depth.items():
            if p == "." or path == p or path.startswith(p + "/"):
                return depth - d
        return None

    cand = []
    for path, depth, status, nf, nb, nd, newest, active, probs, duid in cur.execute(
            """SELECT path, depth, status, files_total, bytes_total, dirs_total, newest_mtime,
                      activity_mtime, problem_dirs_below, uid FROM dirs WHERE depth <= ?""",
            (max(root_depth.values()) + folder_depth,)):
        lv = level(path, depth)
        if lv is None or lv > folder_depth:
            continue
        nb, nf = nb or 0, nf or 0
        if lv <= 2 or nb >= tb // 1000 or nf >= tf // 1000:
            cand.append((max(nb / tb, nf / tf), path, lv, status, nb, nf, nd, newest, active,
                         probs, duid))
    by_bytes = sorted(cand, key=lambda c: (-c[4], c[1]))
    level1 = [c for c in by_bytes if c[2] == 1]
    forced = level1[:MAX_LEVEL1] + [c for c in by_bytes if c[2] == 2][:DETAIL_ROWS]
    rest = level1[MAX_LEVEL1:]
    other = {"n": len(rest), "bytes": sum(c[4] for c in rest),
             "files": sum(c[5] for c in rest)} if rest else None
    names = {c[1] for c in forced}
    shared = sorted((c for c in cand if c[1] not in names and c[2] != 1
                     and (c[4] >= tb // 1000 or c[5] >= tf // 1000)),
                    key=lambda c: (-c[0], c[1]))
    chosen = forced + shared[:max(0, max_folders - len(forced))]
    n_folders = len(forced) + len(shared)
    folders = {c[1]: {"row": c[2:], "cat": defaultdict(int), "own": defaultdict(int)}
               for c in chosen}
    # areas, for the life-cycle view (content mix, old, former members): the level-1 folders,
    # except that one holding more than SPLIT_AREA of the bytes is replaced by its listed
    # subfolders (to folder_depth), so a lab root's `Lab/` becomes its member and project dirs
    kids: dict = defaultdict(list)
    for c in cand:
        kids[_parent(c[1])].append(c)
    area_rows, todo = [], list(level1)
    while todo:
        c = todo.pop()
        if c[4] > SPLIT_AREA * tb and c[2] < folder_depth and kids.get(c[1]):
            todo += kids[c[1]]
        else:
            area_rows.append(c)
    area_rows.sort(key=lambda c: (-c[4], c[1]))
    areas = {c[1]: {"cat": defaultdict(int), "old": 0, "former": 0, "envs": 0, "env_bytes": 0,
                    "raw": 0, "row": c} for c in area_rows[:MAX_LEVEL1]}
    # "old": last changed in (this year - OLD_YEARS) or earlier, as in the report's age facts
    old_cut = time.mktime((time.localtime().tm_year - OLD_YEARS.get(mode, 2) + 1,
                           1, 1, 0, 0, 0, 0, 0, -1))
    if "file_class" in tables and folders:
        for d, cat, uid, old, b in cur.execute("""SELECT f.dir,
                    CASE WHEN c.ftype = 'Container images' THEN 'container' ELSE c.category END,
                    f.uid, f.mtime < ?, SUM(f.size)
                FROM files f JOIN file_class c ON c.id = f.id GROUP BY 1, 2, 3, 4""", (old_cut,)):
            for a in _ancestors(d):
                if a in folders:
                    folders[a]["cat"]["code-env" if cat == "container" else cat] += b
                    folders[a]["own"][uid] += b
                if a in areas:
                    ar = areas[a]
                    ar["cat"][cat] += b
                    ar["old"] += b if old else 0
                    ar["former"] += b if former(uid) else 0
    # owners: who holds the bytes (only a file's owner can move or change it). The top 3
    # file owners with their bytes when there is more than one, plus the rest summed; and
    # dir_owner, the folder's own owner (renaming or moving needs write access to it).
    frows = []
    user_folders: dict = defaultdict(list)          # uid -> [(bytes, path)] at level 1
    for path, f in folders.items():
        lv, status, nb, nf, nd, newest, active, probs, duid = f["row"]
        cats = sorted(f["cat"].items(), key=lambda x: -x[1])[:3]
        own = sorted(f["own"].items(), key=lambda x: (-x[1], x[0]))
        top = own[0][0] if own else None
        if lv == 1:
            for u, b in own:
                user_folders[u].append((b, path))
        frows.append([path, lv, status, nb, nf, nd, _date(newest), _date(active), probs,
                      int(in_work(path)), [[c, b] for c, b in cats],
                      None if top is None else who(top),
                      [[who(u), b] for u, b in own[:3]] if len(own) > 1 else [],
                      sum(b for _, b in own[3:]), None if duid is None else who(duid)])
    frows.sort(key=lambda r: r[0])
    for r in users["table"]["rows"]:                # each user's 3 biggest top-level folders
        r.append([[p, b] for b, p in sorted(user_folders.get(r[1], []),
                                            key=lambda x: (-x[0], x[1]))[:3]])
    users["table"]["cols"].append("top_folders")

    out = {
        "schema_version": SUMMARY_VERSION,
        "generated": C.now_iso(),
        "mode": mode,
        "run": {k: run.get(k) for k in ("target", "roots", "excludes", "created", "created_by",
                                         "host", "one_fs")} | {
                "target_group": json.loads(meta.get("target_group", "null")),
                "folder_depth": folder_depth},
        "totals": tot,
        "dir_status": dict(cur.execute("SELECT status, COUNT(*) FROM dirs GROUP BY status")),
        "problem_dirs": problem,
        "users": users,
        "folders": _table(["path", "level", "status", "bytes", "files", "dirs", "newest",
                           "last_activity", "problem_dirs_below", "pipeline_work",
                           "top_categories", "top_owner", "owners", "other_owners_bytes",
                           "dir_owner"], frows),
        "pipeline_workdirs": workdirs,
        "age_by_year": _table(["year", "files", "bytes"], cur.execute(
            """SELECT strftime('%Y', mtime, 'unixepoch') AS year, COUNT(*), SUM(size)
               FROM files WHERE kind='f' GROUP BY year ORDER BY year""")),
        # decimal decades, for calibrating the survey's big-file duplicate threshold
        "size_histogram": _table(["from_bytes", "files", "bytes"], (
            [0 if dec < 0 else 10 ** dec, n, b] for dec, n, b in cur.execute(
                """SELECT CASE WHEN size = 0 THEN -1 ELSE length(CAST(size AS TEXT)) - 1 END
                          AS dec, COUNT(*), SUM(size)
                   FROM files WHERE kind='f' GROUP BY dec ORDER BY dec"""))),
        "sensitive": _sensitive(cur, tables, in_work),
    }
    out["folder_levels"] = {str(lv): sum(1 for c in cand if c[2] == lv) for lv in (1, 2)}
    if other:                                   # level-1 folders beyond MAX_LEVEL1, summed
        out["folders_other"] = other
    n_raw_rows = 0
    if "type_stats" in tables:
        out["by_category"] = _table(["category", "files", "bytes"], cur.execute(
            """SELECT category, SUM(n_files), SUM(bytes) FROM type_stats
               GROUP BY category ORDER BY SUM(bytes) DESC"""))
        out["types"] = _table(["ftype", "category", "files", "bytes", "dirs", "oldest", "newest"], (
            [ft, c, n, b, nd, _date(o), _date(nw)] for ft, c, n, b, nd, o, nw in cur.execute(
                "SELECT ftype, category, n_files, bytes, n_dirs, oldest, newest FROM type_stats")))
        uex = cur.execute("""SELECT ext, n_files, bytes, n_dirs, oldest, newest FROM ext_stats
                             WHERE category='unknown' ORDER BY bytes DESC""").fetchall()
        out["unknown_exts"] = {"n": len(uex), "top": _table(
            ["ext", "files", "bytes", "dirs", "oldest", "newest"],
            ([e, n, b, nd, _date(o), _date(nw)] for e, n, b, nd, o, nw in uex[:25]))}
        raw = cur.execute("""SELECT dir, ftype, n_files, bytes, oldest, newest FROM raw_locations
                             WHERE NOT pipeline_work(dir)""").fetchall()
        rolled = _rollup_raw(raw)
        n_raw_rows = len(rolled)
        out["raw_locations"] = {"dirs": len(raw), "bytes": sum(r[3] or 0 for r in raw),
                                "top": _table(["path", "ftype", "dirs", "files", "bytes",
                                               "oldest", "newest"],
                                              ([*r[:5], _date(r[5]), _date(r[6])]
                                               for r in rolled[:25]))}
        markers: dict = {}
        for marker, cat, path, nb in cur.execute(
                """SELECT m.marker, m.category, m.path, d.bytes_total FROM dir_markers m
                   LEFT JOIN dirs d ON d.path = m.path
                   ORDER BY m.marker, d.bytes_total DESC, m.path"""):
            mk = markers.setdefault(marker, {"category": cat, "n": 0, "top": []})
            mk["n"] += 1
            if len(mk["top"]) < 5:
                mk["top"].append(path)
        out["markers"] = markers
    if "dup_groups" in tables:
        out["duplicates"] = _duplicates(cur)
    if "dir_dup_groups" in tables and mode != "survey":
        # survey hashes only big files, so small-file trees would match on names and sizes alone
        trees = []
        for gid, be, fe in cur.execute(
                "SELECT gid, bytes_each, files_each FROM dir_dup_groups ORDER BY gid").fetchall():
            paths = [r[0] for r in cur.execute(
                "SELECT path FROM dir_dup_members WHERE gid=? ORDER BY path", (gid,))
                if not in_work(r[0])]
            if len(paths) >= 2:
                trees.append([len(paths), be, fe, be * (len(paths) - 1), paths[:4]])
        trees.sort(key=lambda t: -t[3])
        out.setdefault("duplicates", {})["dir_trees"] = {"n": len(trees), "top": _table(
            ["copies", "bytes_each", "files_each", "reclaimable", "paths"], trees[:10])}
    if areas:
        out["areas"] = _areas(cur, tables, areas, run, raw if "type_stats" in tables else [])
        out["life_cycle"] = _life_cycle(cur, tables, areas, run, tot)
    out["capped"] = _capped(out, n_folders, n_raw_rows)
    if "peeks" in tables:
        out["peeks"] = {
            "file": "peeks.jsonl",
            "n": cur.execute("SELECT COUNT(*) FROM peeks").fetchone()[0],
            "by_format": dict(cur.execute(              # a peek that failed has no format
                """SELECT COALESCE(format, 'unreadable'), COUNT(*) FROM peeks GROUP BY 1
                   ORDER BY 2 DESC""")),
            "by_category": dict(cur.execute(
                """SELECT CASE WHEN type_key LIKE 'unknown:%' THEN 'unknown' ELSE 'known' END,
                          COUNT(*) FROM peeks GROUP BY 1""")),
            "injection_suspects": [r[0] for r in cur.execute(
                "SELECT path FROM peeks WHERE injection_suspect ORDER BY path")],
        }
    return out


def _area_of(path: str, areas: dict) -> str | None:
    for a in _ancestors(path):
        if a in areas:
            return a
    return None


def _areas(cur, tables, areas: dict, run: dict, raw: list) -> dict:
    """One row per level-1 folder: content by category (containers apart from other
    software), bytes not modified in OLD_YEARS, bytes owned by former members, software
    environments, and raw bytes."""
    if "dir_markers" in tables:
        for path, nb in cur.execute("""SELECT m.path, d.bytes_total FROM dir_markers m
                LEFT JOIN dirs d ON d.path = m.path WHERE m.marker IN (?, ?)""", _ENV_MARKERS):
            if (a := _area_of(path, areas)):
                areas[a]["envs"] += 1
                areas[a]["env_bytes"] += nb or 0
    for d, _ft, _nf, nb, _o, _n in raw:
        if (a := _area_of(d, areas)):
            areas[a]["raw"] += nb or 0
    rows = []
    for path, ar in sorted(areas.items(), key=lambda kv: (-kv[1]["row"][4], kv[0])):
        c = ar["row"]
        cats = sorted(((k, b) for k, b in ar["cat"].items() if b), key=lambda x: (-x[1], x[0]))
        rows.append([path, c[4], c[5], [[k, b] for k, b in cats], ar["old"], ar["former"],
                     ar["envs"], ar["env_bytes"], ar["raw"]])
    return {"old_years": OLD_YEARS.get(run.get("mode", "deep"), 2), "table": _table(
        ["path", "bytes", "files", "by_category", "old_bytes", "former_bytes", "envs",
         "env_bytes", "raw_bytes"], rows)}


def _life_cycle(cur, tables, areas: dict, run: dict, tot: dict) -> dict:
    """How data moves between areas: duplicate groups whose copies span two or more areas
    (raw data, references, containers), and symlinks from one area into another or out of
    the target. Duplicates are only as complete as the dupes stage's min size."""
    shared: dict = {}
    if "dup_members" in tables and "file_class" in tables:
        groups: dict = {}
        for gid, size, n_phys, d, cat, ftype in cur.execute("""
                SELECT m.gid, g.size, g.n_phys, f.dir, c.category, c.ftype
                FROM dup_members m JOIN dup_groups g USING (gid)
                JOIN files f ON f.dev = m.dev AND f.ino = m.ino AND f.kind = 'f'
                JOIN file_class c ON c.id = f.id"""):
            g = groups.setdefault(gid, {"size": size, "n": n_phys, "areas": set(),
                                        "kind": "container" if ftype == "Container images"
                                        else cat})
            g["areas"].add(_area_of(d, areas) or ".")
        for g in groups.values():
            if len(g["areas"]) >= 2:
                k = shared.setdefault(g["kind"], {"groups": 0, "bytes": 0, "areas": set()})
                k["groups"] += 1
                k["bytes"] += g["size"] * (g["n"] - 1)
                k["areas"] |= g["areas"]
    across = _table(["kind", "groups", "reclaimable", "areas"], sorted(
        ([k, v["groups"], v["bytes"], len(v["areas"])] for k, v in shared.items()),
        key=lambda r: -r[2]))

    target = run.get("target") or ""
    pairs: Counter = Counter()
    n_links = 0
    for d, link in cur.execute("SELECT dir, link FROM files WHERE kind = 'l'"):
        n_links += 1
        if not link:
            continue
        if link.startswith("/"):
            dst = link[len(target) + 1:] if link.startswith(target + "/") else None
        else:
            dst = os.path.normpath(os.path.join("" if d == "." else d, link))
            dst = None if dst == ".." or dst.startswith("../") else dst
        src_a = _area_of(d, areas) or "."
        dst_a = "(outside)" if dst is None else (_area_of(dst, areas) or ".")
        if dst_a != src_a:
            pairs[(src_a, dst_a)] += 1
    links = {"n": n_links, "cross_area": sum(n for (_, b), n in pairs.items() if b != "(outside)"),
             "outside": sum(n for (_, b), n in pairs.items() if b == "(outside)"),
             "top": _table(["from", "to", "links"], (
                 [a, b, n] for (a, b), n in sorted(pairs.items(),
                                                   key=lambda kv: (-kv[1], kv[0]))[:15]))}
    return {"copied_across_areas": across, "links": links}


def _capped(out: dict, n_folders: int, n_raw_rows: int) -> dict:
    """{list: [shown, total]} for every list that was shortened. Lists inside rows
    (examples, paths) carry their totals in the same row (n, copies)."""
    d = out.get("duplicates", {})
    lists = {
        "folders": (out["folders"], n_folders),
        "users.table": (out["users"]["table"], out["users"]["n"]),
        "problem_dirs.groups": (out["problem_dirs"]["groups"], out["problem_dirs"]["n_groups"]),
        "pipeline_workdirs.top": (out["pipeline_workdirs"]["top"], out["pipeline_workdirs"]["n"]),
        "sensitive.top_dirs_elsewhere": (out["sensitive"]["top_dirs_elsewhere"],
                                         out["sensitive"]["n_dirs_elsewhere"]),
    }
    if "unknown_exts" in out:
        lists["unknown_exts.top"] = (out["unknown_exts"]["top"], out["unknown_exts"]["n"])
        lists["raw_locations.top"] = (out["raw_locations"]["top"], n_raw_rows)
    if "top_groups" in d:
        lists["duplicates.top_groups"] = (d["top_groups"], d["file_groups_outside_pipeline_work"])
        lists["duplicates.dir_overlap"] = (d["dir_overlap"], d["n_dir_overlap"])
    if "dir_trees" in d:
        lists["duplicates.dir_trees.top"] = (d["dir_trees"]["top"], d["dir_trees"]["n"])
    return {k: [len(t["rows"]), n] for k, (t, n) in lists.items() if n > len(t["rows"])}


def _sensitive(cur, tables: set, in_work) -> dict:
    """Credential-like file names, split into those inside software environments
    (usually package files) and those elsewhere (worth a look)."""
    envs = set()
    if "dir_markers" in tables:
        envs = {r[0] for r in cur.execute(
            f"SELECT path FROM dir_markers WHERE marker IN ({','.join('?' * len(_ENV_MARKERS))})",
            _ENV_MARKERS)}
    code = other = 0
    elsewhere = []
    for d, n in cur.execute("SELECT dir, COUNT(*) FROM files WHERE sensitive = 1 GROUP BY dir"):
        if (in_work(d) or _CODE_SEGMENTS.intersection(d.split("/"))
                or any(a in envs for a in _ancestors(d))):
            code += n
        else:
            other += n
            elsewhere.append((d, n))
    elsewhere.sort(key=lambda x: (-x[1], x[0]))
    return {"files": code + other, "in_code_env": code, "elsewhere": other,
            "n_dirs_elsewhere": len(elsewhere),
            "top_dirs_elsewhere": _table(["dir", "files"], elsewhere[:5])}


def _duplicates(cur) -> dict:
    """File duplicates. A group is real only with >= 2 physical copies outside pipeline
    work dirs; a raw file plus its work/ staging copy is a pipeline intermediate."""
    cur.execute("DROP TABLE IF EXISTS temp.dup_out")
    cur.execute("""CREATE TEMP TABLE dup_out AS
        SELECT m.gid, COUNT(DISTINCT f.dev || ':' || f.ino) AS n_out
        FROM dup_members m JOIN files f ON f.dev = m.dev AND f.ino = m.ino AND f.kind = 'f'
        WHERE NOT pipeline_work(f.dir) GROUP BY m.gid""")
    cur.execute("""CREATE TEMP VIEW IF NOT EXISTS dup_real AS
        SELECT g.gid, g.size, g.n_phys, g.confirmed, o.n_out,
               g.size * (o.n_out - 1) AS reclaimable_out
        FROM dup_groups g JOIN dup_out o USING (gid) WHERE o.n_out >= 2""")
    allg = cur.execute("""SELECT COUNT(*), COALESCE(SUM(reclaimable), 0),
                                 COALESCE(MIN(confirmed), 0) FROM dup_groups""").fetchone()
    real = cur.execute("SELECT COUNT(*), COALESCE(SUM(reclaimable_out), 0) FROM dup_real").fetchone()
    top = []
    for gid, size, n_phys, n_out, rec, conf in cur.execute(
            """SELECT gid, size, n_phys, n_out, reclaimable_out, confirmed FROM dup_real
               ORDER BY reclaimable_out DESC, gid LIMIT 10""").fetchall():
        paths = [r[0] for r in cur.execute(
            """SELECT CASE WHEN f.dir = '.' THEN f.name ELSE f.dir || '/' || f.name END
               FROM dup_members m JOIN files f ON f.dev = m.dev AND f.ino = m.ino AND f.kind = 'f'
               WHERE m.gid = ? AND NOT pipeline_work(f.dir) ORDER BY f.dir, f.name LIMIT 3""",
            (gid,))]
        top.append([size, n_phys, n_out, rec, conf, paths])
    return {
        "file_groups": allg[0],
        "reclaimable_bytes": allg[1],
        "file_groups_outside_pipeline_work": real[0],
        "reclaimable_bytes_outside_pipeline_work": real[1],
        "confirmed": bool(allg[0]) and bool(allg[2]),
        "top_groups": _table(["size", "copies", "copies_outside_pipeline_work",
                              "reclaimable_outside_pipeline_work", "confirmed", "paths"], top),
        "dir_overlap": _table(
            ["dir_a", "dir_b", "shared_bytes", "shared_files", "frac_a", "frac_b"],
            cur.execute("""
                SELECT o.dir_a, o.dir_b, o.shared_bytes, o.shared_files,
                       ROUND(1.0 * o.shared_bytes / NULLIF(da.bytes_direct, 0), 3),
                       ROUND(1.0 * o.shared_bytes / NULLIF(db.bytes_direct, 0), 3)
                FROM dir_overlap o LEFT JOIN dirs da ON da.path = o.dir_a
                LEFT JOIN dirs db ON db.path = o.dir_b
                WHERE NOT (pipeline_work(o.dir_a) OR pipeline_work(o.dir_b))
                ORDER BY o.shared_bytes DESC LIMIT 10""")),
        "n_dir_overlap": cur.execute(
            """SELECT COUNT(*) FROM dir_overlap
               WHERE NOT (pipeline_work(dir_a) OR pipeline_work(dir_b))""").fetchone()[0],
        "hardlinks": dict(zip(("groups", "saved_bytes"), cur.execute(
            """SELECT COUNT(*), COALESCE(SUM(saved), 0) FROM hardlink_groups
               WHERE n_paths > 1""").fetchone())),
    }


def run(db: sqlite3.Connection, run_dir: str, run: dict, post_stats: dict) -> dict:
    s = build(db, run)
    s["post_stats"] = post_stats
    n = C.write_json_atomic(C.out_path(run_dir, "summary.json"), s, dumps=dumps)
    if n > WARN_BYTES:
        C.log(f"WARNING: summary.json is {n} bytes (> {WARN_BYTES}); "
              "the agent may need to read it in parts")
    d = s.get("duplicates", {})
    return {"bytes": n, "folders": len(s["folders"]["rows"]),
            "problem_dirs": s["problem_dirs"]["n"],
            "pipeline_workdirs": s["pipeline_workdirs"]["n"],
            "reclaimable_outside_pipeline_work": d.get("reclaimable_bytes_outside_pipeline_work")}
