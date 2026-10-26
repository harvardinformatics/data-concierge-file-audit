"""plan -> scan -> post on the synthetic `lab` fixture (see conftest.py)."""
import json
import os
import sqlite3

import pytest

import plan
import post
import scan
from conftest import snapshot

KB = 1024
IS_ROOT = os.geteuid() == 0


def _plan(lab, tmp_path, *extra):
    audits = tmp_path / "audits"
    plan.main([str(lab), "--audits-dir", str(audits), "--name", "t1",
               "--exclude", "excluded", *extra])
    return str(audits / "t1")


def _post(run_dir, **kw):
    kw = {"min_size": KB, "chunk": 4 * KB, "min_dir_bytes": KB, "workers": 1, **kw}
    return post.run_post(run_dir, **kw)


@pytest.fixture
def db(lab, tmp_path):
    before = snapshot(lab)
    run_dir = _plan(lab, tmp_path)
    info = scan.run_task(run_dir, workers=2)
    assert info["failed"] == []
    _post(run_dir)
    assert snapshot(lab) == before, "target was modified"
    con = sqlite3.connect(os.path.join(run_dir, "audit.sqlite"))
    yield con
    con.close()


def test_plan_shards_and_current_run(lab, tmp_path):
    run_dir = _plan(lab, tmp_path)
    run = json.load(open(os.path.join(run_dir, "run.json")))
    assert run["target"] == os.path.realpath(lab)
    assert os.path.realpath(lab / "excluded") in run["excludes"]
    shards = scan.load_shards(run_dir)
    modes = {os.path.relpath(p, os.path.realpath(lab)): m for _, m, p in shards}
    assert modes["."] == "flat" and modes["userA"] == "flat"
    assert modes["userA/proj"] == "tree" and modes["userC/private"] == "tree"
    assert "userA/link" not in modes               # symlink is not a shard
    assert (tmp_path / "audits" / ".current-run").read_text().strip() == "t1"


def test_scan_is_idempotent_and_splits_tasks(lab, tmp_path):
    run_dir = _plan(lab, tmp_path)
    a = scan.run_task(run_dir, task_index=0, num_tasks=2)
    b = scan.run_task(run_dir, task_index=1, num_tasks=2)
    assert a["shards"] + b["shards"] == len(scan.load_shards(run_dir))
    again = scan.run_task(run_dir, task_index=0, num_tasks=2)
    assert again["scanned_now"] == 0                # requeue: nothing redone
    assert os.path.exists(os.path.join(run_dir, "scan.task0.DONE"))


def test_post_refuses_incomplete_scan(lab, tmp_path):
    run_dir = _plan(lab, tmp_path)
    scan.run_task(run_dir, task_index=0, num_tasks=2)
    with pytest.raises(RuntimeError, match="not done"):
        _post(run_dir)


def test_dir_status(db):
    st = dict(db.execute("SELECT path, status FROM dirs"))
    assert st["excluded"] == "excluded"
    assert st["userC/.ssh"] == "sensitive"
    if not IS_ROOT:
        assert st["userC/private"] == "unreadable"
    assert st["userA/proj/data"] == "ok"
    # nothing from inside the sensitive/excluded dirs was recorded
    assert not db.execute("SELECT 1 FROM files WHERE dir LIKE 'userC/.ssh%' "
                          "OR dir LIKE 'excluded%'").fetchone()
    # unreadable dir keeps owner + mtime for the stale/huge-dir report
    uid, mtime = db.execute("SELECT uid, mtime FROM dirs WHERE path='userC/private'").fetchone()
    assert uid == os.getuid() and mtime > 0


def test_file_duplicates(db):
    groups = {}
    for gid, size, n_phys, n_paths in db.execute(
            "SELECT gid, size, n_phys, n_paths FROM dup_groups"):
        paths = sorted(f"{d}/{n}" for d, n in db.execute(
            """SELECT f.dir, f.name FROM dup_members m JOIN files f
               ON f.dev=m.dev AND f.ino=m.ino WHERE m.gid=?""", (gid,)))
        groups[tuple(paths)] = (size, n_phys, n_paths)
    a = ("userA/proj/data/a.fq.gz", "userA/proj/hl.fq.gz",
         "userB/copy/a.fq.gz", "userB/partial/a.fq.gz")
    b = ("userA/proj/data/b.fq.gz", "userB/copy/b.fq.gz")
    assert groups[a] == (64 * KB, 3, 4)      # hardlink counted once physically
    assert groups[b] == (64 * KB, 2, 2)
    assert len(groups) == 2                  # c.bin, secret, .ssh, excluded not included
    total = db.execute("SELECT SUM(reclaimable) FROM dup_groups").fetchone()[0]
    assert total == 3 * 64 * KB
    # sensitive-named file never hashed
    assert not db.execute("""SELECT 1 FROM hashes h JOIN files f ON f.dev=h.dev AND f.ino=h.ino
                             WHERE f.name='my_secret.bin'""").fetchone()


def test_hardlinks(db):
    n, saved = db.execute(
        "SELECT COUNT(*), SUM(saved) FROM hardlink_groups WHERE n_paths > 1").fetchone()
    assert (n, saved) == (1, 64 * KB)


def test_dir_overlap(db):
    row = db.execute("""SELECT shared_bytes, shared_files FROM dir_overlap
                        WHERE dir_a='userA/proj/data' AND dir_b='userB/copy'""").fetchone()
    assert row == (128 * KB, 2)


def test_exact_dir_duplicates(db):
    groups = {}
    for gid, in db.execute("SELECT gid FROM dir_dup_groups"):
        groups[gid] = sorted(r[0] for r in db.execute(
            "SELECT path FROM dir_dup_members WHERE gid=?", (gid,)))
    assert ["userA/proj/data", "userB/copy"] in groups.values()
    # incomplete trees (containing unreadable dirs) never match
    assert db.execute("SELECT complete FROM dirs WHERE path='userC'").fetchone()[0] == (
        1 if IS_ROOT else 0)


def test_rollup_totals(db):
    files_total, newest = db.execute(
        "SELECT files_total, newest_mtime FROM dirs WHERE path='userA'").fetchone()
    assert files_total == 4                  # data/{a,b,notes} + hl ; link is not a file
    assert newest > 0


def test_classify(db):
    assert db.execute("SELECT ftype, category FROM ext_class WHERE ext='fq.gz'").fetchone() == (
        "FASTQ reads", "raw")
    raw_dirs = {r[0] for r in db.execute("SELECT dir FROM raw_locations")}
    assert {"userA/proj/data", "userB/copy", "userB/partial"} <= raw_dirs
    assert db.execute("SELECT category FROM ext_stats WHERE ext='bin'").fetchone() == ("unknown",)


def test_rules_names_and_markers(tmp_path, capsys):
    t = tmp_path / "t"
    files = {"run/hifi_reads/m84_s1.hifi_reads.bc1018.bam": "raw",
             "run/fail_reads/m84_s1.fail_reads.unassigned.bam": "raw",
             "run/hifi_reads/m84_s1.hifi_reads.bc1018.bam.pbi": "processed",
             "aln/birds.hal": "processed", "aln/birds.hal.bak": "processed",
             "graph/pan.gfa": "processed", "graph/pan.og": "processed",
             "kmers/reads.jf": "processed", "cov/s1.d4": "processed",
             "ref/genome.fa.masked": "reference", "xtal/refl.mtz": "processed",
             "tax/nucl_gb.accession2taxid.gz": "reference"}
    for rel in files:
        (t / rel).parent.mkdir(parents=True, exist_ok=True)
        (t / rel).write_text("x\n")
    (t / "run" / "pb_formats").mkdir()
    audits = tmp_path / "audits"
    plan.main([str(t), "--audits-dir", str(audits), "--name", "r"])
    run_dir = str(audits / "r")
    scan.run_task(run_dir)
    _post(run_dir, stages=["merge", "classify"])
    assert "rules:" not in capsys.readouterr().err       # every rule can match
    con = sqlite3.connect(os.path.join(run_dir, "audit.sqlite"))
    got = {d + "/" + n: c for d, n, c in con.execute(
        "SELECT f.dir, f.name, c.category FROM files f JOIN file_class c ON c.id = f.id")}
    markers = con.execute("SELECT path, marker FROM dir_markers").fetchall()
    con.close()
    assert got == files
    assert ("run", "PacBio run folder") in markers


def test_confirm_full_hash(lab, tmp_path):
    run_dir = _plan(lab, tmp_path)
    scan.run_task(run_dir)
    _post(run_dir, confirm=True)
    con = sqlite3.connect(os.path.join(run_dir, "audit.sqlite"))
    assert con.execute("SELECT MIN(confirmed), COUNT(*) FROM dup_groups").fetchone() == (1, 2)
    con.close()


def test_only_restricts_roots(lab, tmp_path):
    run_dir = _plan(lab, tmp_path, "--only", "userA/proj", "--only", "userB/copy")
    scan.run_task(run_dir)
    _post(run_dir)
    con = sqlite3.connect(os.path.join(run_dir, "audit.sqlite"))
    dirs = {r[0] for r in con.execute("SELECT path FROM dirs")}
    assert "userB/partial" not in dirs and "userA/proj/data" in dirs
    assert con.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 2
    con.close()


def test_workdir_copy_back(lab, tmp_path):
    run_dir = _plan(lab, tmp_path)
    scan.run_task(run_dir)
    _post(run_dir, workdir=str(tmp_path / "work"))
    assert os.path.exists(os.path.join(run_dir, "audit.sqlite"))
    assert not os.path.exists(tmp_path / "work" / "audit.sqlite")
