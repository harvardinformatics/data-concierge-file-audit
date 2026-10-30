"""Fixed sentences for report.md, written from summary.json (used by render.py; stdlib only).

f_NAME(summary) returns the bullets of a `{{facts:NAME}}` block (totals, coverage,
method and limits), so the agent writes only interpretation; FACTS maps NAME to it.
"""
from __future__ import annotations

import os

import common as C
from report_tables import (_cat, _code, _n, _old_bytes, _old_years, _pct, _plural, _rows)


def _size(b) -> str:            # thresholds read better without ".0": "256 MiB"
    return C.human_bytes(b).replace(".0 ", " ")


def f_scope(s):
    r, t = s["run"], s["totals"]
    dp = s.get("post_stats", {}).get("dupes", {})
    tgt, survey = r["target"], s.get("mode") == "survey"

    def rel(p):
        q = os.path.relpath(p, tgt) if p.startswith(tgt + "/") else p
        return _code(q) + (" (this tool's repo, always excluded)"
                           if os.path.realpath(p) == str(C.REPO_ROOT) else "")
    roots = [x for x in r.get("roots", ["."]) if x != "."]
    read = (f"- **Read:** metadata for {_n(t['files'])} files, {_n(t['symlinks'])} "
            f"symlinks and {_n(t['dirs'])} directories")
    if dp.get("candidates"):
        err = dp.get("hash_errors", 0)
        read += (f"; partial hashes (three {_size(dp.get('chunk', 1 << 20))} slices) of "
                 f"{_n(dp['candidates'] - err)} files ≥ {_size(dp.get('min_size', 0))} that share a size"
                 + (f" ({_n(err)} couldn't be read)" if err else ""))
    read += ("; no file contents." if survey
             else f"; content samples from {_n(s.get('peeks', {}).get('n', 0))} files.")
    limits = ("- **Limits:** file types come from extensions only. Duplicates are compared only "
              f"for files ≥ {_size(dp.get('min_size', 0))}, by partial hash, so they are likely, "
              "not byte-confirmed. Small-file duplicates and duplicate folder trees aren't "
              "reported, and only the names that reach `summary.json` were screened for "
              "injected instructions." if survey else
              "- **Limits:** duplicates are likely (partial hash) unless marked confirmed; "
              "unreadable directories are listed below and weren't scanned.")
    return [f"- **Target:** {_code(tgt)}" + (f", only {', '.join(_code(x) for x in roots)}"
                                            if roots else "")
            + f"; {s.get('mode', 'deep')} mode; scanned {r['created'][:10]}.",
            "- **Excluded:** " + (", ".join(rel(e) for e in r.get("excludes", [])) or "none") + ".",
            read, limits]


def f_storage(s):
    t = s["totals"]
    hl = s.get("duplicates", {}).get("hardlinks", {}).get("saved_bytes", 0)
    inodes = t["files"] + t["dirs"] + t["symlinks"] + t["other"]
    return [f"- **Scanned:** {C.human_bytes(t['bytes'])} by path in {_n(t['files'])} files"
            + (f"; {C.human_bytes(hl)} of that is extra paths to hardlinked files, so about "
               f"{C.human_bytes(t['bytes'] - hl)} is on disk." if hl else "."),
            f"- **Inodes seen:** {_n(inodes)} (files, directories, symlinks and other), "
            "to compare with `quota`'s file count."]


def f_age(s):
    rows = [r for r in _rows(s["age_by_year"]) if r["year"]]
    tb = sum(r["bytes"] for r in rows) or 1
    year = int((s.get("generated") or "0000")[:4])
    out = []
    for label, n in (("2+ years", 2), ("5+ years", 5)):
        sel = [r for r in rows if int(r["year"]) <= year - n]
        b = sum(r["bytes"] for r in sel)
        out.append(f"- **{label}:** {C.human_bytes(b)} ({_pct(b, tb)} of scanned bytes, "
                   f"{_n(sum(r['files'] for r in sel))} files) last changed in {year - n} or earlier.")
    out.append("- Dates are file modification times; hardlinked files count once per path.")
    return out


def f_types(s):
    cats = {r["category"]: r for r in _rows(s["by_category"])}
    raw, unk, rl = cats.get("raw", {}), cats.get("unknown", {}), s["raw_locations"]
    marks = ", ".join(f"{k.replace(' run folder', '')} {v['n']:,}"
                      for k, v in sorted(s.get("markers", {}).items())
                      if v.get("category") == "raw" and "run folder" in k)
    x = s["sensitive"]
    skipped = s.get("dir_status", {}).get("sensitive", 0)
    return [f"- **Raw data:** {C.human_bytes(raw.get('bytes', 0))} in {_n(raw.get('files', 0))} "
            f"files; raw locations hold {C.human_bytes(rl['bytes'])} across {_n(rl['dirs'])} "
            "directories" + (f". Run folders: {marks}." if marks else "."),
            f"- **Unclassified:** {C.human_bytes(unk.get('bytes', 0))} in "
            f"{_n(unk.get('files', 0))} files with {_n(s['unknown_exts']['n'])} different extensions.",
            f"- **Credential-like names:** {_n(x['files'])} files, {_n(x['in_code_env'])} of them "
            f"inside software environments; {_n(x['elsewhere'])} elsewhere, in "
            f"{_n(x['n_dirs_elsewhere'])} folders. Matched by name only, never opened"
            + (f"; {_n(skipped)} folders with such names weren't scanned." if skipped else ".")]


def f_duplicates(s):
    d = s["duplicates"]
    dp = s.get("post_stats", {}).get("dupes", {})
    hl = d.get("hardlinks", {}).get("saved_bytes", 0)
    out = [f"- **Totals:** {_n(d['file_groups'])} groups of identical files"
           + (f" ≥ {_size(dp['min_size'])}" if dp.get("min_size") else "")
           + f"; {C.human_bytes(d['reclaimable_bytes'])} could be reclaimed, "
           f"{C.human_bytes(d['reclaimable_bytes_outside_pipeline_work'])} of it outside pipeline "
           f"work ({_n(d['file_groups_outside_pipeline_work'])} groups). "
           f"{_n(d['n_dir_overlap'])} pairs of folders share duplicate files.",
           "- **Method:** " + ("full hashes; confirmed duplicates." if d.get("confirmed") else
                               "size plus a partial hash, so these are likely duplicates, not "
                               "byte-confirmed.")
           + (f" Hardlinks ({C.human_bytes(hl)} of extra paths) already share their data and "
              "aren't counted." if hl else "")]
    extra = []
    if dp.get("groups_too_wide_for_pairs"):
        n = dp["groups_too_wide_for_pairs"]
        extra.append(f"{_plural(n, 'group spans', 'groups span')} more than "
                     f"{dp.get('max_pair_dirs', 20)} folders and {'isn' if n == 1 else 'aren'}'t paired")
    if dp.get("hash_errors"):
        extra.append(f"{_plural(dp['hash_errors'], 'file', 'files')} couldn't be read")
    if extra:
        out.append("- **Gaps:** " + "; ".join(extra) + ".")
    return out


def f_pipeline(s):
    w = s["pipeline_workdirs"]
    return [f"- **Pipeline work dirs:** {_n(w['n'])} (Nextflow `work/`, Snakemake `.snakemake/` "
            f"and similar) hold {C.human_bytes(w['bytes'])} in {_n(w['files'])} files."]


def f_unreadable(s):
    p, ds = s["problem_dirs"], s.get("dir_status", {})
    out = [f"- **Unreadable:** {_n(ds.get('unreadable', p['n']))} directories, grouped by "
           f"parent into {_n(p['n_groups'])} groups below."]
    if ds.get("sensitive"):
        out.append(f"- **Skipped:** {_n(ds['sensitive'])} directories whose names look like "
                   "credential stores weren't scanned.")
    return out


def _min_dup(s) -> str:
    return _size(s.get("post_stats", {}).get("dupes", {}).get("min_size", 0))


def f_raw_access(s):
    areas = _rows(s["areas"]["table"])
    raw = sorted((a for a in areas if a["raw_bytes"]), key=lambda a: (-a["raw_bytes"], a["path"]))
    total = sum(a["raw_bytes"] for a in raw)
    across = {r["kind"]: r for r in _rows(s["life_cycle"]["copied_across_areas"])}.get("raw")
    ln = s["life_cycle"]["links"]
    out = [f"- **Where:** {C.human_bytes(total)} of raw data in {_n(len(raw))} of "
           f"{_n(len(areas))} areas" + (
               f"; {_code(raw[0]['path'])} holds {_pct(raw[0]['raw_bytes'], total)} of it."
               if raw else ".")]
    out.append("- **Copied:** " + (
        f"{C.human_bytes(across['reclaimable'])} of raw files ≥ {_min_dup(s)} are copies of "
        f"files in another area ({_plural(across['groups'], 'group', 'groups')}, "
        f"{across['areas']} areas)." if across else
        f"no raw files ≥ {_min_dup(s)} have copies in another area."))
    out.append(f"- **Linked:** {_n(ln['cross_area'])} symlinks point from one area into another "
               f"and {_n(ln['outside'])} outside the target, of {_n(ln['n'])} symlinks.")
    return out


def f_shared(s):
    areas = _rows(s["areas"]["table"])
    ref = [a for a in areas if _cat(a, "reference")]
    envs = [a for a in areas if a["envs"]]
    return [f"- **Kept per area:** reference data in {_n(len(ref))} areas, software "
            f"environments in {_n(len(envs))}; copies are compared only for files ≥ {_min_dup(s)}, "
            "so smaller indexes and environments aren't counted as copies."]


def f_old(s):
    yrs, year, tb = _old_years(s), int(s["generated"][:4]), s["totals"]["bytes"] or 1
    u = s["users"]
    out = []
    for n in (yrs, 5):
        b = _old_bytes(s, n)
        out.append(f"- **{n}+ years:** {C.human_bytes(b)} ({_pct(b, tb)}) last changed in "
                   f"{year - n} or earlier.")
    grp = s.get("run", {}).get("target_group")
    out.append(f"- **Former members:** {_plural(u['former'], 'account', 'accounts')} not in the "
               + (f"`{grp}` group" if grp else "lab's group") + " (or deleted) own "
               f"{C.human_bytes(u['former_bytes'])} ({_pct(u['former_bytes'], tb)}); "
               f"{_n(u['active'])} owners are current members.")
    out.append("- Dates are file modification times; membership is the FASRC unix group.")
    return out


def f_areas(s):
    areas = sorted((a["bytes"] for a in _rows(s["areas"]["table"])), reverse=True)
    tb = s["totals"]["bytes"] or 1
    cum, k90 = 0, 0
    for b in areas:
        cum += b
        k90 += 1
        if cum >= 0.9 * tb:
            break
    return [f"- **Concentration:** the 3 largest of {_n(len(areas))} areas hold "
            f"{_pct(sum(areas[:3]), tb)} of the bytes; {_n(k90)} hold 90%."]


FACTS = {name[2:]: fn for name, fn in globals().items() if name.startswith("f_")}
