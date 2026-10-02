"""Rule-based classification (used by post.py; stdlib only).

Builds, from rules.toml:
  ext_class(ext, ftype, category)        every extension present in `files`
  name_class(id, ftype, category)        per-file overrides from `names` globs
  file_class (VIEW)                      id -> ftype, category ('unknown' if unmatched)
  type_stats                             per (ftype, category): files, bytes, dirs, dates
  ext_stats                              per (extension, class), incl. unknown ones (peek targets)
  raw_locations                          per (dir, ftype) holding raw-category files
  dir_markers(path, marker, category)    e.g. Illumina run folders, conda envs, git repos
No file contents are read here.
"""
from __future__ import annotations

import sqlite3
import tomllib

import common as C


def load_rules(path: str) -> dict:
    with open(path, "rb") as fh:
        rules = tomllib.load(fh)
    ext_map: dict[str, tuple[str, str]] = {}
    for ft in rules.get("filetype", []):
        for e in ft.get("ext", []):
            e = e.lower()
            if e in ext_map:
                C.log(f"rules: ext {e!r} in both {ext_map[e][0]!r} and {ft['name']!r}; first wins")
                continue
            if C.ext_of("x." + e) != e:
                C.log(f"rules: ext {e!r} ({ft['name']}) can never match ext_of(); ignored")
                continue
            ext_map[e] = (ft["name"], ft["category"])
    rules["_ext_map"] = ext_map
    return rules


def run(db: sqlite3.Connection, rules: dict) -> dict:
    cur = db.cursor()
    cur.executescript("""
        DROP VIEW IF EXISTS file_class;
        DROP TABLE IF EXISTS ext_class;  DROP TABLE IF EXISTS name_class;
        DROP TABLE IF EXISTS type_stats; DROP TABLE IF EXISTS ext_stats;
        DROP TABLE IF EXISTS raw_locations; DROP TABLE IF EXISTS dir_markers;
        CREATE TABLE ext_class(ext TEXT PRIMARY KEY, ftype TEXT, category TEXT);
        CREATE TABLE name_class(id INTEGER PRIMARY KEY, ftype TEXT, category TEXT);
        CREATE TABLE dir_markers(path TEXT, marker TEXT, category TEXT,
                                 PRIMARY KEY(path, marker));
    """)
    ext_map = rules["_ext_map"]
    present = [r[0] for r in cur.execute("SELECT DISTINCT ext FROM files WHERE kind='f'")]
    cur.executemany("INSERT INTO ext_class VALUES (?,?,?)",
                    [(e, *ext_map[e]) for e in present if e in ext_map])

    for ft in rules.get("filetype", []):
        for g in ft.get("names", []):
            cur.execute("""INSERT OR IGNORE INTO name_class
                           SELECT id, ?, ? FROM files WHERE kind='f' AND name GLOB ?""",
                        (ft["name"], ft["category"], g))

    cur.executescript("""
        CREATE VIEW file_class AS
          SELECT f.id AS id,
                 COALESCE(n.ftype, e.ftype, 'unknown') AS ftype,
                 COALESCE(n.category, e.category, 'unknown') AS category
          FROM files f
          LEFT JOIN name_class n ON n.id = f.id
          LEFT JOIN ext_class e ON e.ext = f.ext
          WHERE f.kind = 'f';
        CREATE TABLE type_stats AS
          SELECT c.ftype, c.category, COUNT(*) AS n_files, SUM(f.size) AS bytes,
                 COUNT(DISTINCT f.dir) AS n_dirs, MIN(f.mtime) AS oldest, MAX(f.mtime) AS newest
          FROM files f JOIN file_class c ON c.id = f.id
          GROUP BY c.ftype, c.category ORDER BY bytes DESC;
        CREATE TABLE ext_stats AS          -- per (ext, class), so `names` overrides count
          SELECT f.ext, c.ftype, c.category,
                 COUNT(*) AS n_files, SUM(f.size) AS bytes, COUNT(DISTINCT f.dir) AS n_dirs,
                 MIN(f.mtime) AS oldest, MAX(f.mtime) AS newest
          FROM files f JOIN file_class c ON c.id = f.id
          GROUP BY f.ext, c.ftype, c.category ORDER BY bytes DESC;
        CREATE TABLE raw_locations AS
          SELECT f.dir AS dir, c.ftype AS ftype, COUNT(*) AS n_files, SUM(f.size) AS bytes,
                 MIN(f.mtime) AS oldest, MAX(f.mtime) AS newest
          FROM files f JOIN file_class c ON c.id = f.id
          WHERE c.category = 'raw' GROUP BY f.dir, c.ftype ORDER BY bytes DESC;
    """)

    for dm in rules.get("dirmarker", []):
        for g in dm.get("files", []):
            cur.execute("""INSERT OR IGNORE INTO dir_markers
                           SELECT DISTINCT dir, ?, ? FROM files WHERE name GLOB ?""",
                        (dm["name"], dm["category"], g))
        for g in dm.get("dirs", []):
            cur.execute("""INSERT OR IGNORE INTO dir_markers
                           SELECT DISTINCT parent, ?, ? FROM dirs
                           WHERE name GLOB ? AND parent IS NOT NULL""",
                        (dm["name"], dm["category"], g))
    db.commit()

    n_unknown = cur.execute(
        "SELECT COALESCE(SUM(n_files),0), COALESCE(SUM(bytes),0) FROM ext_stats "
        "WHERE category='unknown'").fetchone()
    return {
        "types": cur.execute("SELECT COUNT(*) FROM type_stats").fetchone()[0],
        "unknown_files": n_unknown[0], "unknown_bytes": n_unknown[1],
        "raw_dirs": cur.execute("SELECT COUNT(DISTINCT dir) FROM raw_locations").fetchone()[0],
        "dir_markers": cur.execute("SELECT COUNT(*) FROM dir_markers").fetchone()[0],
    }
