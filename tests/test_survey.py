"""Survey mode, splitting shards on the fly, summary v2 and the peek budget split.

All on tiny tmp_path trees; scale is checked on the cluster (docs/plans/survey-mode.md).
"""
import glob
import json
import os
import re
import sqlite3

import pytest

import peek
import plan
import post
import scan
import summary
from conftest import snapshot

KB = 1024
IS_ROOT = os.geteuid() == 0


def _plan(target, tmp_path, name, *extra):
    audits = tmp_path / "audits"
    plan.main([str(target), "--audits-dir", str(audits), "--name", name, *extra])
    return str(audits / name)


def _post(run_dir, **kw):
    return post.run_post(run_dir, **{"chunk": 4 * KB, "min_dir_bytes": KB, "workers": 1, **kw})


def _summary(run_dir):
    with open(os.path.join(run_dir, "summary.json")) as fh:
        return json.load(fh)


def rows(table):
    return [dict(zip(table["cols"], r)) for r in table["rows"]]


def _db(run_dir):
    return sqlite3.connect(os.path.join(run_dir, "audit.sqlite"))


def _content(run_dir):
    """Everything the scan recorded, independent of how it was sharded."""
    con = _db(run_dir)
    files = sorted(con.execute("SELECT dir, name, kind, size, ino FROM files"))
    dirs = sorted(con.execute("SELECT path, status, n_entries, n_files, bytes_direct FROM dirs"))
    dup = sorted(con.execute("SELECT size, n_phys, n_paths FROM dup_groups"))
    trees = sorted(con.execute("SELECT n_dirs, bytes_each FROM dir_dup_groups"))
    con.close()
    return files, dirs, dup, trees


@pytest.fixture
def deep_lab(lab):
    """`lab` plus a branching tree, so tree shards split and their children split again."""
    for d in range(3):
        for e in range(3):
            for g in range(2):
                p = lab / "big" / f"d{d}" / f"e{e}" / f"g{g}"
                p.mkdir(parents=True)
                (p / "f.txt").write_text(f"{d}{e}{g}\n")
    return lab


def _split_run(target, tmp_path, name, **kw):
    run_dir = _plan(target, tmp_path, name, "--exclude", "excluded")
    for i in range(2):                                  # two array tasks
        info = scan.run_task(run_dir, task_index=i, num_tasks=2, split_files=2, **kw)
        assert info["failed"] == []
    return run_dir


def test_split_matches_unsplit(deep_lab, tmp_path):
    before = snapshot(deep_lab)
    ref = _plan(deep_lab, tmp_path, "ref", "--exclude", "excluded")
    scan.run_task(ref, split_files=0)
    _post(ref, min_size=KB)

    run_dir = _split_run(deep_lab, tmp_path, "split", workers=2)
    assert snapshot(deep_lab) == before, "target was modified"
    ids = [sid for sid, _m, _r in scan.expand_shards(run_dir)]
    assert any(i.count(".") == 1 for i in ids), "no child shards"
    assert any(i.count(".") >= 2 for i in ids), "children never split again"
    for sid in ids:                                     # children stay in their parent's task
        task = int(sid.split(".")[0]) % 2
        assert os.path.exists(os.path.join(run_dir, f"scan.task{task}.DONE"))
    _post(run_dir, min_size=KB)
    assert _content(run_dir) == _content(ref)
    con = _db(run_dir)
    assert con.execute("SELECT COUNT(*) = COUNT(DISTINCT dir || '/' || name) FROM files").fetchone()[0]
    assert con.execute("SELECT COUNT(DISTINCT shard) FROM dirs").fetchone()[0] > len(
        scan.load_shards(run_dir))
    con.close()


def _drop(run_dir, sid):
    for p in glob.glob(os.path.join(run_dir, "shards", f"{sid}.*")):
        rest = os.path.basename(p)[len(sid) + 1:]
        if not rest[:1].isdigit():                      # this shard's files, not its children's
            os.remove(p)


def test_resume_after_crash(deep_lab, tmp_path):
    ref = _split_run(deep_lab, tmp_path, "ref")
    _post(ref, min_size=KB)
    run_dir = _split_run(deep_lab, tmp_path, "crash")
    ids = [sid for sid, _m, _r in scan.expand_shards(run_dir)]
    leaf = next(i for i in ids if i.count(".") >= 2)
    parent = next(i for i in ids if i.count(".") == 1)
    # a leaf killed before its .done; a parent killed before its .done (so its children
    # never started), leaving a half-written .tmp behind
    _drop(run_dir, leaf)
    for i in ids:
        if i == parent or i.startswith(parent + "."):
            _drop(run_dir, i)
    open(os.path.join(run_dir, "shards", f"{parent}.files.tsv.gz.tmp"), "w").close()

    with pytest.raises(RuntimeError, match="not done"):
        _post(run_dir, min_size=KB)
    redo = sum(scan.run_task(run_dir, task_index=i, num_tasks=2, split_files=2)["scanned_now"]
               for i in range(2))
    assert redo >= 2
    _post(run_dir, min_size=KB)
    assert _content(run_dir) == _content(ref)
    con = _db(run_dir)
    assert con.execute("SELECT COUNT(*) = COUNT(DISTINCT dir || '/' || name) FROM files").fetchone()[0]
    con.close()


def test_survey_defaults(lab, tmp_path, capsys):
    run_dir = _plan(lab, tmp_path, "sv", "--mode", "survey", "--exclude", "excluded")
    line = capsys.readouterr().out.strip()
    assert "\n" not in line and "mode survey" in line and "depth 3" in line
    with open(os.path.join(run_dir, "run.json")) as fh:
        run = json.load(fh)
    assert run["mode"] == "survey" and run["depth"] == 3 and run["listing"]
    scan.run_task(run_dir)
    stats = _post(run_dir)                              # mode defaults only
    assert stats["mode"] == "survey" and "peek" not in stats["stages"]
    assert stats["dupes"]["min_size"] == 256 << 20 and stats["dupes"]["max_pair_dirs"] == 20
    assert stats["rollup"]["tree_dups"] is False and "dir_dup_groups" not in stats["rollup"]
    assert not os.path.exists(os.path.join(run_dir, "peeks.jsonl"))
    s = _summary(run_dir)
    assert s["mode"] == "survey" and s["run"]["folder_depth"] == 3
    assert s["duplicates"]["file_groups"] == 0          # 64 KiB test files are below 256 MiB
    assert "dir_trees" not in s["duplicates"] and "peeks" not in s
    assert _post(run_dir, min_size=KB)["dupes"]["groups"] == 2     # explicit flags win
    again = _post(run_dir, stages=["rollup", "summarize"])           # rerun keeps earlier stats
    assert again["stages"] == ["rollup", "summarize"] and again["dupes"]["groups"] == 2
    assert _summary(run_dir)["post_stats"]["merge"]["files"] == again["merge"]["files"] > 0


def test_summary_v2_shape_and_budget(lab, tmp_path):
    for i in range(200):                                # many dirs and unknown types
        d = lab / "many" / f"p{i % 20}" / f"s{i}"
        d.mkdir(parents=True)
        (d / f"x.u{i}").write_bytes(b"x" * (i + 1))
        (d / f"y{i}.txt").write_bytes(b"y" * 10)
    locked = []
    for i in range(40):                                 # unreadable dirs under a few parents
        d = lab / "locked" / f"g{i % 4}" / f"home{i}"
        d.mkdir(parents=True)
        d.chmod(0)
        locked.append(d)
    try:
        run_dir = _plan(lab, tmp_path, "v2", "--exclude", "excluded")
        scan.run_task(run_dir)
        stats = _post(run_dir, min_size=KB)
    finally:
        for d in locked:
            d.chmod(0o700)
    s = _summary(run_dir)
    assert s["schema_version"] == summary.SUMMARY_VERSION == 2
    assert stats["summarize"]["bytes"] == os.path.getsize(os.path.join(run_dir, "summary.json"))
    assert stats["summarize"]["bytes"] < summary.WARN_BYTES

    def tables(x):
        if isinstance(x, dict):
            if set(x) == {"cols", "rows"}:
                yield x
            else:
                for v in x.values():
                    yield from tables(v)
    found = list(tables(s))
    assert len(found) >= 12
    for t in found:
        assert all(len(r) == len(t["cols"]) for r in t["rows"])
    assert len(s["unknown_exts"]["top"]["rows"]) == 25 and s["unknown_exts"]["n"] == 201
    assert s["capped"]["unknown_exts.top"] == [25, 201]
    assert all(shown < total for shown, total in s["capped"].values())
    with open(os.path.join(run_dir, "summary.json")) as fh:
        lines = fh.read().splitlines()
    n_rows = sum(len(t["rows"]) for t in found)
    assert len(lines) > n_rows and max(map(len, lines)) < 2000      # one row per line
    assert len(s["folders"]["rows"]) <= 120
    date = re.compile(r"\d{4}-\d{2}-\d{2}$")
    assert date.match(s["totals"]["newest"]) and date.match(rows(s["users"]["table"])[0]["newest"])
    hist = rows(s["size_histogram"])
    assert sum(h["files"] for h in hist) == s["totals"]["files"]
    assert {h["from_bytes"] for h in hist} >= {1, 10, 100, 10000}
    if not IS_ROOT:
        g = rows(s["problem_dirs"]["groups"])
        locked_groups = [x for x in g if x["parent"].startswith("locked/")]
        assert len(locked_groups) == 4 and all(x["n"] == 10 for x in locked_groups)
        assert all(len(x["examples"]) == 2 for x in locked_groups)
        assert s["problem_dirs"]["n"] >= 40


def test_peek_budget_split(tmp_path):
    t = tmp_path / "t"
    for i in range(10):
        (t / f"u{i}").mkdir(parents=True)
        (t / f"u{i}" / f"a.zz{i}").write_bytes(b"\x00" * (1000 + i))
    (t / "raw").mkdir()
    (t / "raw" / "s.fq").write_text("@r\nACGT\n+\nFFFF\n")
    (t / "calls").mkdir()
    (t / "calls" / "x.vcf").write_text("##fileformat=VCFv4.2\n")
    (t / "docs").mkdir()
    (t / "docs" / "notes.md").write_text("big notes\n" * 1000)  # biggest, but not raw/processed
    run_dir = _plan(t, tmp_path, "pk")
    scan.run_task(run_dir)
    _post(run_dir, min_size=KB, max_peeks=5)
    with open(os.path.join(run_dir, "peeks.jsonl")) as fh:
        picked = [json.loads(x) for x in fh]
    cats = [p["category"] for p in picked]
    assert cats == ["unknown"] * 3 + ["raw", "processed"]
    s = _summary(run_dir)
    assert s["peeks"]["by_category"] == {"unknown": 3, "known": 2}


def test_peek_budget_falls_back(lab, tmp_path):
    """With few unknown types, the known side gets the rest of the budget (and vice versa)."""
    run_dir = _plan(lab, tmp_path, "fb", "--exclude", "excluded")
    scan.run_task(run_dir)
    _post(run_dir, min_size=KB)
    con = _db(run_dir)
    picks = peek.select(con, 5)
    con.close()
    keys = [k for k, _ in picks]
    assert len(picks) == 5 and keys[0].startswith("unknown:")
    assert sum(k.startswith("unknown:") for k in keys) < 3


def test_folder_newest_ignores_dir_mtimes(tmp_path):
    """A reorganised folder (fresh dir mtimes, old files): `newest` is its newest file,
    `last_activity` also counts dir mtimes."""
    t = tmp_path / "t"
    (t / "old" / "sub").mkdir(parents=True)
    f = t / "old" / "sub" / "a.txt"
    f.write_text("x\n")
    os.utime(f, (1_600_000_000, 1_600_000_000))       # 2020-09-13; the dirs keep today's mtime
    (t / "empty").mkdir()
    run_dir = _plan(t, tmp_path, "nw")
    scan.run_task(run_dir)
    _post(run_dir, min_size=KB)
    folders = {r["path"]: r for r in rows(_summary(run_dir)["folders"])}
    assert folders["old"]["newest"] == folders["old/sub"]["newest"] == "2020-09-13"
    assert folders["old"]["last_activity"] > "2020-09-13"     # the dirs' own (today's) mtime
    assert "empty" not in folders or folders["empty"]["newest"] is None
    con = _db(run_dir)                                  # a DB built before activity_mtime
    con.execute("ALTER TABLE dirs DROP COLUMN activity_mtime")
    con.commit()
    con.close()
    _post(run_dir, stages=["rollup", "summarize"])
    assert rows(_summary(run_dir)["folders"])[0]["last_activity"]


def test_sensitive_breakdown(tmp_path):
    t = tmp_path / "t"
    files = ["env/conda-meta/history", "env/ssl/cert.pem",
             "env/lib/python3.12/site-packages/certifi/cacert.pem",
             "tools/repo/.git/hooks/x.key", "proj/.env", "proj/sub/api_token", "proj/sub/b.token"]
    for rel in files:
        (t / rel).parent.mkdir(parents=True, exist_ok=True)
        (t / rel).write_text("x\n")
    run_dir = _plan(t, tmp_path, "sens")
    scan.run_task(run_dir)
    _post(run_dir, min_size=KB)
    sens = _summary(run_dir)["sensitive"]
    assert (sens["files"], sens["in_code_env"], sens["elsewhere"]) == (6, 3, 3)
    assert rows(sens["top_dirs_elsewhere"]) == [{"dir": "proj/sub", "files": 2},
                                                {"dir": "proj", "files": 1}]


def test_folders_always_list_levels_1_and_2(tmp_path, monkeypatch):
    """Tiny top-level folders and the largest level-2 folders are always listed, so the
    report's Areas tables don't depend on the 0.1% cut-off."""
    t = tmp_path / "t"
    (t / "big" / "proj").mkdir(parents=True)
    (t / "big" / "proj" / "data.bin").write_bytes(b"x" * 2_000_000)
    for i in range(1200):                               # files dominate the file share
        (t / "big" / "proj" / f"f{i}.txt").write_text("y")
    (t / "tiny" / "sub").mkdir(parents=True)
    (t / "tiny" / "sub" / "a.txt").write_text("z")      # far below 0.1% of bytes and files
    run_dir = _plan(t, tmp_path, "lv")
    scan.run_task(run_dir)
    _post(run_dir, min_size=KB)
    s = _summary(run_dir)
    lv = {r["path"]: r["level"] for r in rows(s["folders"])}
    assert lv["tiny"] == 1 and lv["tiny/sub"] == 2 and lv["big/proj"] == 2 and lv["."] == 0
    assert s["folder_levels"] == {"1": 2, "2": 2}
    assert all(r["status"] == "ok" for r in rows(s["folders"]))
    monkeypatch.setattr(summary, "MAX_LEVEL1", 1)       # more top-level folders than the cap
    _post(run_dir, stages=["summarize"])
    s = _summary(run_dir)
    assert s["folders_other"] == {"n": 1, "bytes": 1, "files": 1}
    assert "tiny" not in {r["path"] for r in rows(s["folders"])}
