"""Content peeks (used by post.py; stdlib only).

Picks representative files and reads at most `max_bytes` from the start of each,
the way a person would open one file of each kind to see what it is:
  * one file (the largest) per (directory, type key); the key is the rules.toml
    ftype, or 'unknown:<ext>' for unmatched extensions;
  * the budget is split: about UNKNOWN_SHARE for unknown keys (resolving them is
    what peeks are for), the rest for known keys with raw, then processed, types
    first (checking that "raw" really is raw); a share either side can't use
    goes to the other; within each side, keys by total bytes, round-robin, so
    every type is sampled before any type gets a second directory; at most
    `max_peeks` files in total, unknown ones listed first.
Sensitive files are never opened. Target files are opened only via common.open_ro.

Writes <run>/peeks.jsonl and a `peeks` table. Raw bytes are never stored: only
the first 16 bytes as hex, the compression, a detected format, a few parsed
features, and (text only) up to MAX_LINES lines of at most LINE_CHARS chars.
Peeked text is untrusted data; `injection_suspect` flags text that looks like
instructions aimed at an AI so the agent can report it, never follow it.
"""
from __future__ import annotations

import bz2
import json
import lzma
import os
import re
import sqlite3
import stat
import zlib
from collections import defaultdict

import common as C

MAX_LINES = 8
LINE_CHARS = 160
UNKNOWN_SHARE = 0.6
_CAT_ORDER = {"raw": 0, "processed": 1}

_SIGS = [  # (offset, bytes, format), checked on decompressed bytes
    (0, b"\x89HDF\r\n\x1a\n", "hdf5"), (0, b"%PDF", "pdf"), (0, b"\x89PNG", "png"),
    (0, b"\xff\xd8\xff", "jpeg"), (0, b"GIF8", "gif"), (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"), (0, b"PK\x03\x04", "zip"), (0, b"SQLite format 3\x00", "sqlite"),
    (0, b"BAM\x01", "bam"), (0, b"CRAM", "cram"), (0, b"BCF", "bcf"), (0, b"\x7fELF", "elf"),
    (0, b"\x93NUMPY", "npy"), (0, b"RDX2\n", "rdata"), (0, b"RDX3\n", "rdata"),
    (0, b"X\n", "rds"), (0, b"\x8bPOD\r\n\x1a\n", "pod5"), (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (257, b"ustar", "tar"),
]
_INJECT = re.compile(
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+"
    r"(instructions|prompts?|messages)"
    r"|\byou\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|claude|chatgpt|llm|agent)\b"
    r"|\bsystem\s+prompt\b|<\s*/?\s*(system|instructions?)\s*>", re.I)
_ILLUMINA = re.compile(r"^@\S+:\d+:\S+:\d+:\d+:\d+:\d+(\s+[12]:[YN]:\d+:\S*)?")


def _compression(raw: bytes) -> str | None:
    if raw[:2] == b"\x1f\x8b":
        return "bgzf" if len(raw) > 13 and raw[3] & 4 and raw[12:14] == b"BC" else "gzip"
    if raw[:3] == b"BZh":
        return "bz2"
    if raw[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    return None


def _decompress(raw: bytes, comp: str, limit: int) -> bytes:
    """Decompress a *prefix* of a file; truncated input is expected."""
    if comp in ("gzip", "bgzf"):
        out, data = bytearray(), raw
        while data and len(out) < limit:          # BGZF / concatenated members
            d = zlib.decompressobj(31)
            out += d.decompress(data, limit - len(out))
            if not d.eof:
                break
            data = d.unused_data
        return bytes(out)
    if comp == "bz2":
        return bz2.BZ2Decompressor().decompress(raw, limit)
    if comp == "xz":
        return lzma.LZMADecompressor().decompress(raw, limit)
    from compression import zstd                  # Python >= 3.14
    return zstd.ZstdDecompressor().decompress(raw, limit)


def _text_lines(data: bytes) -> list[str] | None:
    """Lines if `data` looks like text, else None. The last line may be cut off."""
    if not data or b"\x00" in data[:8192]:
        return None
    s = data[:8192].decode("utf-8", "replace")
    bad = sum(1 for ch in s if ch == "�" or (ord(ch) < 32 and ch not in "\t\r\n"))
    if bad > len(s) * 0.02:
        return None
    return data.decode("utf-8", "replace").splitlines()


def _features(lines: list[str]) -> dict:
    """Cheap structure hints; the agent does the interpretation."""
    full = lines[:-1] if len(lines) > 1 else lines   # drop a possibly cut-off line
    if len(full) >= 4 and full[0].startswith("@") and full[2].startswith("+"):
        lens = sorted({len(full[i + 1]) for i in range(0, len(full) - 3, 4)
                       if full[i].startswith("@")})
        return {"kind": "fastq", "read_lengths": lens[:5] + (lens[-1:] if len(lens) > 5 else []),
                "illumina_header": bool(_ILLUMINA.match(full[0]))}
    if full and full[0].startswith(">"):
        seq = "".join(x for x in full[1:] if not x.startswith(">")).upper()
        nt = bool(seq) and set(seq) <= set("ACGTUNRYKMSWBDHV-.*")
        return {"kind": "fasta", "records_in_sample": sum(x.startswith(">") for x in full),
                "alphabet": "nucleotide" if nt else "protein/other"}
    body = [x for x in full if x and not x.startswith("#")][:50]
    for delim, name in (("\t", "tab"), (",", "comma")):
        counts = {x.count(delim) for x in body}
        if len(body) >= 2 and len(counts) == 1 and next(iter(counts)) > 0:
            return {"kind": "delimited", "delimiter": name, "columns": next(iter(counts)) + 1}
    return {"kind": "text"}


def peek_file(path: bytes, max_bytes: int) -> dict:
    out: dict = {}
    try:
        fd = C.open_ro(path)
    except OSError as ex:
        return {"error": f"open:{ex.strerror}"}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {"error": "not a regular file"}
        raw = os.read(fd, max_bytes)
    except OSError as ex:
        return {"error": f"read:{ex.strerror}"}
    finally:
        os.close(fd)
    out["magic"] = raw[:16].hex()
    comp = _compression(raw)
    out["compression"] = comp
    data = raw
    if comp:
        try:
            data = _decompress(raw, comp, max_bytes)
        except Exception as ex:                    # corrupt, or not really compressed
            out["error"] = f"{comp}:{type(ex).__name__}"
            data = b""
    fmt = next((f for off, sig, f in _SIGS if data[off:off + len(sig)] == sig), None)
    lines = None if fmt else _text_lines(data)
    if lines is not None:
        out["format"] = "text"
        out["features"] = _features(lines)
        out["lines"] = [x[:LINE_CHARS] for x in lines[:MAX_LINES]]
        out["injection_suspect"] = bool(_INJECT.search(data.decode("utf-8", "replace")))
    else:
        out["format"] = fmt or ("binary" if data else "empty")
    return out


def _round_robin(by_key: dict, keys: list[str]) -> list[tuple[str, int]]:
    """(key, file id) in round-robin order over `keys`, each key's dirs by bytes."""
    lists = [(k, sorted(by_key[k], reverse=True)) for k in keys]
    out, rnd = [], 0
    while any(rnd < len(v) for _k, v in lists):
        out += [(k, v[rnd][1]) for k, v in lists if rnd < len(v)]
        rnd += 1
    return out


def select(db: sqlite3.Connection, max_peeks: int) -> list[tuple]:
    rows = db.execute("""
        SELECT CASE WHEN c.category = 'unknown' THEN 'unknown:' || f.ext ELSE c.ftype END AS k,
               c.category, f.dir, f.id, MAX(f.size), SUM(f.size) AS b
        FROM files f JOIN file_class c ON c.id = f.id
        WHERE f.kind = 'f' AND f.sensitive = 0 AND f.size > 0
        GROUP BY k, f.dir""").fetchall()
    by_key: dict[str, list] = defaultdict(list)
    cat: dict[str, str] = {}
    for k, c, _d, fid, _mx, b in rows:
        by_key[k].append((b, fid))
        cat[k] = c
    total = {k: sum(b for b, _ in v) for k, v in by_key.items()}
    unknown = sorted((k for k in by_key if k.startswith("unknown:")), key=lambda k: -total[k])
    known = sorted((k for k in by_key if not k.startswith("unknown:")),
                   key=lambda k: (_CAT_ORDER.get(cat[k], 2), -total[k]))
    u, kn = _round_robin(by_key, unknown), _round_robin(by_key, known)
    n_u = min(len(u), max(round(max_peeks * UNKNOWN_SHARE), max_peeks - len(kn)))
    return u[:n_u] + kn[:max_peeks - n_u]


def run(db: sqlite3.Connection, run_dir: str, target: str, max_peeks: int,
        max_bytes: int) -> dict:
    from dupes import _abs_path
    cur = db.cursor()
    cur.executescript("""DROP TABLE IF EXISTS peeks;
        CREATE TABLE peeks(id INTEGER PRIMARY KEY, path TEXT, type_key TEXT, format TEXT,
                           injection_suspect INT, json TEXT);""")
    out = C.out_path(run_dir, "peeks.jsonl")
    n = n_err = n_inj = 0
    with open(out + ".tmp", "w", encoding="utf-8") as fh:
        for key, fid in select(db, max_peeks):
            d, draw, name, nraw, size, ftype, cat = cur.execute(
                """SELECT f.dir, f.dir_raw, f.name, f.name_raw, f.size, c.ftype, c.category
                   FROM files f JOIN file_class c ON c.id = f.id WHERE f.id = ?""",
                (fid,)).fetchone()
            if C.is_sensitive_file(name, d):        # belt and braces; scan already flags these
                continue
            rel = name if d == "." else f"{d}/{name}"
            rec = {"path": rel, "size": size, "ftype": ftype, "category": cat,
                   "type_key": key, **peek_file(_abs_path(target, d, draw, name, nraw), max_bytes)}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cur.execute("INSERT INTO peeks VALUES (?,?,?,?,?,?)",
                        (fid, rel, key, rec.get("format"), int(rec.get("injection_suspect", False)),
                         json.dumps(rec, ensure_ascii=False)))
            n += 1
            n_err += "error" in rec
            n_inj += bool(rec.get("injection_suspect"))
    os.replace(out + ".tmp", out)
    db.commit()
    return {"peeked": n, "errors": n_err, "injection_suspects": n_inj,
            "max_peeks": max_peeks, "max_bytes": max_bytes}
