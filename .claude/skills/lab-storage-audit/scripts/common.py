"""Shared helpers for the lab-storage-audit scripts. Stdlib only.

Safety contract (keep in sync with SAFETY.md):
  * Target files are opened only through open_ro(): O_RDONLY | O_NOFOLLOW | O_NONBLOCK.
  * Outputs are written only inside the run directory (out_path() enforces it),
    and the run directory must not be inside the target unless it sits under an
    excluded path (this repo is auto-excluded when the target contains it).
  * Credential-like paths are never opened; sensitive dirs are never descended.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import gzip
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1

SCRIPTS_DIR = Path(__file__).resolve().parent
# scripts -> lab-storage-audit -> skills -> .claude -> repo root
REPO_ROOT = SCRIPTS_DIR.parents[3]

# --- sensitive paths (mirrors docs/cluster-reference.md credential list) -----

SENSITIVE_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".kube", ".azure", ".irods", ".ncbi",
    "secrets", ".secrets",
})
SENSITIVE_DIR_GLOBS = (".globus*",)
SENSITIVE_FILE_GLOBS = (
    ".env", ".env.*", ".git-credentials", ".netrc", ".pgpass", ".my.cnf",
    ".pypirc", ".npmrc", ".renviron", "*.ngc",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem", "*.key",
    "*credentials*", "*secret*", "*_token", "*.token",
    ".bash_history", ".python_history",
)


def is_sensitive_dir(path: str) -> bool:
    """True for directories whose contents must never be listed or opened."""
    name = os.path.basename(path).lower()
    if name in SENSITIVE_DIR_NAMES:
        return True
    if any(fnmatch.fnmatchcase(name, g) for g in SENSITIVE_DIR_GLOBS):
        return True
    parent = os.path.basename(os.path.dirname(path)).lower()
    return name == "gcloud" and parent == ".config"


def is_sensitive_file(name: str, parent: str = "") -> bool:
    """True for files whose contents must never be opened (metadata only)."""
    n = name.lower()
    if any(fnmatch.fnmatchcase(n, g) for g in SENSITIVE_FILE_GLOBS):
        return True
    return n == "config.json" and os.path.basename(parent).lower() == ".docker"


# --- path containment ----------------------------------------------------------

def is_within(path: str, root: str) -> bool:
    """True if `path` equals `root` or lies below it (both absolute, normalized)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def rel_to(path: str, target: str) -> str:
    """Path relative to the target; the target itself is '.'."""
    if path == target:
        return "."
    return path[len(target) + 1:]


def check_run_dir_safe(run_dir: str, target: str, excludes: list[str]) -> None:
    """Refuse to write inside the target unless under an excluded path."""
    run_dir = os.path.realpath(run_dir)
    if is_within(run_dir, target) and not any(is_within(run_dir, e) for e in excludes):
        die(f"refusing: run dir {run_dir} is inside target {target} and not excluded")


def out_path(run_dir: str | Path, *parts: str) -> str:
    """Join an output path and assert it stays inside the run directory."""
    run_dir = os.path.realpath(run_dir)
    p = os.path.realpath(os.path.join(run_dir, *parts))
    if not is_within(p, run_dir):
        die(f"refusing to write outside run dir: {p}")
    return p


def open_ro(path: str) -> int:
    """Open a target file read-only without following a final symlink. Returns fd.
    O_NONBLOCK: a FIFO that replaced a scanned file must not hang the job
    (no effect on regular files)."""
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)


# --- TSV encoding (paths may contain tabs/newlines/backslashes/non-UTF-8) ------

_ESC = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESC = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}
_ESC_RE = re.compile(r"[\\\t\n\r]")
_UNESC_RE = re.compile(r"\\(.)")


def esc(s: str) -> str:
    return _ESC_RE.sub(lambda m: _ESC[m.group(0)], s)


def unesc(s: str) -> str:
    return _UNESC_RE.sub(lambda m: _UNESC.get(m.group(1), m.group(1)), s)


def open_tsv_gz_write(path: str):
    return gzip.open(path, "wt", encoding="utf-8", errors="surrogateescape",
                     newline="\n", compresslevel=6)


def open_tsv_gz_read(path: str):
    return gzip.open(path, "rt", encoding="utf-8", errors="surrogateescape",
                     newline="\n")


def fs_str(s: str) -> str:
    """Make a surrogateescape'd filename safe for JSON/sqlite text (lossy only
    for non-UTF-8 names, which get U+FFFD)."""
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# --- extensions ----------------------------------------------------------------

COMPRESSION_EXTS = frozenset({"gz", "bgz", "bgzf", "bz2", "xz", "zst", "zstd", "lz4",
                              "ora", "dsrc"})


def ext_of(name: str) -> str:
    """Lower-case extension, compound for compressed files: 'x.fastq.gz' ->
    'fastq.gz'. Rotated logs 'x.log.1' -> 'log'. Dotfiles and junk -> ''."""
    parts = name.lower().split(".")
    if parts and parts[0] == "":          # dotfile: '.bashrc', '.env.local'
        parts = parts[1:]
    if len(parts) < 2:
        return ""
    exts = parts[1:]
    while len(exts) > 1 and exts[-1].isdigit():   # rotated: x.log.1 -> log
        exts = exts[:-1]
    e = exts[-1]
    if e in COMPRESSION_EXTS and len(exts) >= 2:
        e = exts[-2] + "." + e
    if e.isdigit() or len(e) > 16 or not re.fullmatch(r"[a-z0-9_+.-]+", e):
        return ""
    return e


# --- run metadata / sentinels / logging ------------------------------------------

def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_run(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "run.json")) as fh:
        run = json.load(fh)
    if run.get("schema_version") != SCHEMA_VERSION:
        die(f"run.json schema_version {run.get('schema_version')} != {SCHEMA_VERSION}")
    return run


def human_bytes(n) -> str:
    """Binary units, one decimal: 6.2 GiB."""
    n = float(n or 0)
    for u in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or u == "PiB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def write_json_atomic(path: str, obj, dumps=None) -> int:
    """Write JSON atomically, indented unless a dumps(obj) -> str is given.
    Returns bytes written."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(dumps(obj) if dumps else json.dumps(obj, indent=2, sort_keys=True))
        fh.write("\n")
        n = fh.tell()
    os.replace(tmp, path)
    return n


def write_sentinel(run_dir: str, name: str, info: dict) -> None:
    """name like 'scan.task0.DONE' or 'post.FAILED'."""
    info = {"time": now_iso(), "host": os.uname().nodename,
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"), **info}
    write_json_atomic(out_path(run_dir, name), info)


def clear_sentinel(run_dir: str, name: str) -> None:
    try:
        os.remove(out_path(run_dir, name))
    except FileNotFoundError:
        pass


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 2):
    log(f"ERROR: {msg}")
    raise SystemExit(code)


def default_workers() -> int:
    env = os.environ.get("SLURM_CPUS_PER_TASK")
    if env and env.isdigit():
        return max(1, int(env))
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def parse_size(s: str) -> int:
    """'1MiB', '512K', '10G', '4096' -> bytes (binary units)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*", s.lower())
    if not m:
        raise ValueError(f"bad size: {s!r}")
    mult = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40, "p": 1 << 50}
    return int(float(m.group(1)) * mult[m.group(2)])
