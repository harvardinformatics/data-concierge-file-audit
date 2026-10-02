import os
import random
import shutil
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "lab-storage-audit" / "scripts"
sys.path.insert(0, str(SCRIPTS))

KB = 1024


def _blob(seed: int, n: int = 64 * KB) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture
def lab(tmp_path):
    """A tiny synthetic tree with known duplicates, links and permission cases.

    target/
      top.txt
      userA/proj/data/{a.fq.gz, b.fq.gz, notes.txt}
      userA/proj/hl.fq.gz          hardlink of data/a.fq.gz (not a duplicate)
      userA/link -> proj           symlink (never followed)
      userB/copy/{a.fq.gz, b.fq.gz, notes.txt}   exact copy of userA/proj/data
      userB/partial/a.fq.gz        copy of a only
      userB/other/c.bin            same size as a, different content
      userC/private/               mode 000 (unreadable)
      userC/.ssh/id_rsa            sensitive dir: never listed
      userC/my_secret.bin          sensitive name: metadata only, never hashed
      excluded/big.bin             passed via --exclude
    """
    t = tmp_path / "target"
    A, B = _blob(1), _blob(2)
    files = {
        "top.txt": b"hello\n",
        "userA/proj/data/a.fq.gz": A,
        "userA/proj/data/b.fq.gz": B,
        "userA/proj/data/notes.txt": b"same notes\n",
        "userB/copy/a.fq.gz": A,
        "userB/copy/b.fq.gz": B,
        "userB/copy/notes.txt": b"same notes\n",
        "userB/partial/a.fq.gz": A,
        "userB/other/c.bin": _blob(3),
        "userC/private/hidden.fq.gz": A,
        "userC/.ssh/id_rsa": A,
        "userC/my_secret.bin": A,
        "excluded/big.bin": A,
    }
    for rel, data in files.items():
        p = t / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    os.link(t / "userA/proj/data/a.fq.gz", t / "userA/proj/hl.fq.gz")
    os.symlink("proj", t / "userA/link")
    private = t / "userC/private"
    private.chmod(0)
    try:
        yield t
    finally:
        private.chmod(stat.S_IRWXU)
        shutil.rmtree(tmp_path, ignore_errors=True)


def snapshot(root: Path) -> dict:
    """path -> (size, mtime_ns, mode) for everything readable under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return out
