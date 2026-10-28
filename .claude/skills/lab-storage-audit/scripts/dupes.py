"""File-level duplicate detection (used by post.py; stdlib only).

Pipeline over the `files` table:
  1. physical files = one row per (dev, ino) of regular, non-sensitive files
     >= min_size, so hardlinks are never counted as duplicates;
  2. candidates = physical files whose size is shared with another;
  3. partial hash = blake2b(size || head || middle || tail), `chunk` bytes each
     (whole file when size <= 3*chunk, which is then an exact content hash);
  4. groups = same (size, partial hash); with confirm=True every member that
     was only partially hashed gets a full-content hash and groups are rebuilt;
  5. hardlink groups and parent-directory overlap (shared duplicate bytes).

Target files are opened only via common.open_ro (read-only, no symlink follow).
"""
from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations

import common as C

READ_BLOCK = 8 << 20
MAX_PAIR_DIRS = 50      # groups spread over more dirs than this skip pairwise overlap
BATCH = 10_000          # hash rows per INSERT batch


def _read_exact(fd: int, n: int, off: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        b = os.pread(fd, n - len(buf), off + len(buf))
        if not b:
            break
        buf += b
    return bytes(buf)


def hash_file(path: bytes, size: int, ino: int, chunk: int, full: bool = False):
    """Return (digest_hex, covered_whole_file, error_or_None)."""
    try:
        fd = C.open_ro(path)
    except OSError as ex:
        return None, 0, f"open:{ex.strerror}"
    try:
        st = os.fstat(fd)
        if st.st_ino != ino or st.st_size != size:
            return None, 0, "changed"
        h = hashlib.blake2b(digest_size=16)
        h.update(size.to_bytes(8, "little"))
        if full or size <= 3 * chunk:
            off = 0
            while True:
                b = os.pread(fd, READ_BLOCK, off)
                if not b:
                    break
                h.update(b)
                off += len(b)
            return h.hexdigest(), 1, None
        for off in (0, (size - chunk) // 2, size - chunk):
            h.update(_read_exact(fd, chunk, off))
        return h.hexdigest(), 0, None
    except OSError as ex:
        return None, 0, f"read:{ex.strerror}"
    finally:
        os.close(fd)


def _hash_job(args):
    dev, ino, size, path, chunk, full = args
    digest, whole, err = hash_file(path, size, ino, chunk, full)
    return dev, ino, size, digest, whole, err


def _abs_path(target: str, d: str, d_raw, n: str, n_raw) -> bytes:
    dpart = d_raw if d_raw is not None else d.encode()
    npart = n_raw if n_raw is not None else n.encode()
    base = os.fsencode(target)
    return base + b"/" + npart if dpart == b"." else base + b"/" + dpart + b"/" + npart


def _pool_map(fn, jobs, workers):
    """Lazy map over an iterable of jobs; at most a few hundred in flight, so a
    lab-sized candidate list is never held in memory."""
    if workers <= 1:
        yield from map(fn, jobs)
        return
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("fork")) as ex:
        yield from ex.map(fn, jobs, chunksize=16, buffersize=workers * 4)


def _batched_insert(cur, sql, rows) -> int:
    """Insert from an iterator in batches; returns the number of rows with an error (r[5])."""
    n_err, batch = 0, []
    for r in rows:
        n_err += bool(r[5])
        batch.append(r)
        if len(batch) >= BATCH:
            cur.executemany(sql, batch)
            batch = []
    cur.executemany(sql, batch)
    return n_err


def run(db: sqlite3.Connection, target: str, min_size: int, chunk: int,
        confirm: bool, workers: int, max_pair_dirs: int = MAX_PAIR_DIRS) -> dict:
    cur = db.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS phys;   DROP TABLE IF EXISTS hashes;
        DROP TABLE IF EXISTS dup_groups; DROP TABLE IF EXISTS dup_members;
        DROP TABLE IF EXISTS hardlink_groups; DROP TABLE IF EXISTS dir_overlap;
    """)
    cur.execute("""CREATE TABLE phys AS
        SELECT dev, ino, size, MIN(id) AS fid, COUNT(*) AS n_paths FROM files
        WHERE kind='f' AND sensitive=0 AND size >= ? GROUP BY dev, ino""", (min_size,))
    cur.execute("CREATE INDEX phys_size ON phys(size)")
    n_cand = cur.execute("""SELECT COALESCE(SUM(n), 0) FROM
        (SELECT COUNT(*) AS n FROM phys GROUP BY size HAVING COUNT(*) > 1)""").fetchone()[0]
    C.log(f"dupes: {n_cand} candidate files (same size, >= {min_size} B)")

    cur.execute("""CREATE TABLE hashes(dev INT, ino INT, size INT, phash TEXT,
                   whole INT, fullhash TEXT, error TEXT, PRIMARY KEY(dev, ino))""")
    cand = db.cursor().execute("""         -- streamed: a second cursor, read while inserting
        SELECT p.dev, p.ino, p.size, f.dir, f.dir_raw, f.name, f.name_raw
        FROM phys p JOIN files f ON f.id = p.fid
        WHERE p.size IN (SELECT size FROM phys GROUP BY size HAVING COUNT(*) > 1)""")
    jobs = ((dev, ino, size, _abs_path(target, d, dr, n, nr), chunk, False)
            for dev, ino, size, d, dr, n, nr in cand)
    n_err = _batched_insert(
        cur, "INSERT INTO hashes(dev, ino, size, phash, whole, error) VALUES (?,?,?,?,?,?)",
        _pool_map(_hash_job, jobs, workers))

    key = "phash"
    if confirm:
        todo = cur.execute("""
            SELECT h.dev, h.ino, h.size, f.dir, f.dir_raw, f.name, f.name_raw
            FROM hashes h JOIN phys p USING(dev, ino) JOIN files f ON f.id = p.fid
            WHERE h.whole = 0 AND h.phash IS NOT NULL AND (h.size, h.phash) IN
              (SELECT size, phash FROM hashes WHERE phash IS NOT NULL
               GROUP BY size, phash HAVING COUNT(*) > 1)""").fetchall()
        C.log(f"dupes: --confirm full-hashing {len(todo)} files")
        jobs = [(dev, ino, size, _abs_path(target, d, dr, n, nr), chunk, True)
                for dev, ino, size, d, dr, n, nr in todo]
        for dev, ino, _s, digest, _w, err in _pool_map(_hash_job, jobs, workers):
            cur.execute("UPDATE hashes SET fullhash=?, error=COALESCE(?, error) "
                        "WHERE dev=? AND ino=?", (digest, err, dev, ino))
        cur.execute("UPDATE hashes SET fullhash = phash WHERE whole = 1")
        key = "fullhash"

    cur.execute(f"""CREATE TABLE dup_groups AS
        SELECT ROW_NUMBER() OVER (ORDER BY h.size * (COUNT(*) - 1) DESC) AS gid,
               h.size AS size, h.{key} AS hash, COUNT(*) AS n_phys,
               SUM(p.n_paths) AS n_paths, h.size * (COUNT(*) - 1) AS reclaimable,
               MIN(h.whole OR {1 if confirm else 0}) AS confirmed
        FROM hashes h JOIN phys p USING(dev, ino)
        WHERE h.{key} IS NOT NULL
        GROUP BY h.size, h.{key} HAVING COUNT(*) > 1""")
    cur.execute("CREATE INDEX dup_groups_gid ON dup_groups(gid)")
    cur.execute(f"""CREATE TABLE dup_members AS
        SELECT g.gid, h.dev, h.ino FROM dup_groups g
        JOIN hashes h ON h.size = g.size AND h.{key} = g.hash""")
    cur.execute("CREATE INDEX dup_members_gid ON dup_members(gid)")
    cur.execute("CREATE INDEX dup_members_di ON dup_members(dev, ino)")

    cur.execute("""CREATE TABLE hardlink_groups AS
        SELECT dev, ino, size, COUNT(*) AS n_paths, MAX(nlink) AS nlink,
               size * (COUNT(*) - 1) AS saved
        FROM files WHERE kind='f' AND nlink > 1 GROUP BY dev, ino""")

    # parent-directory overlap: bytes of duplicate content shared by two dirs
    overlap: dict[tuple[str, str], list[int]] = {}
    wide = 0

    def flush(dirs, size):
        nonlocal wide
        if len(dirs) > max_pair_dirs:
            wide += 1
            return
        for a, b in combinations(sorted(dirs), 2):
            acc = overlap.setdefault((a, b), [0, 0])
            acc[0] += size
            acc[1] += 1

    cur_gid, cur_size, dirs = None, 0, set()
    for gid, size, d in cur.execute("""
            SELECT m.gid, g.size, f.dir FROM dup_members m
            JOIN dup_groups g USING(gid)
            JOIN files f ON f.dev = m.dev AND f.ino = m.ino AND f.kind='f'
            ORDER BY m.gid"""):
        if gid != cur_gid:
            if cur_gid is not None:
                flush(dirs, cur_size)
            cur_gid, cur_size, dirs = gid, size, set()
        dirs.add(d)
    if cur_gid is not None:
        flush(dirs, cur_size)
    cur.execute("""CREATE TABLE dir_overlap(dir_a TEXT, dir_b TEXT,
                   shared_bytes INT, shared_files INT)""")
    cur.executemany("INSERT INTO dir_overlap VALUES (?,?,?,?)",
                    ((a, b, v[0], v[1]) for (a, b), v in overlap.items()))
    db.commit()

    g = cur.execute("SELECT COUNT(*), COALESCE(SUM(reclaimable),0) FROM dup_groups").fetchone()
    return {"candidates": n_cand, "hash_errors": n_err, "groups": g[0],
            "reclaimable_bytes": g[1], "dir_pairs": len(overlap),
            "groups_too_wide_for_pairs": wide, "confirmed": bool(confirm),
            "min_size": min_size, "chunk": chunk, "max_pair_dirs": max_pair_dirs}
