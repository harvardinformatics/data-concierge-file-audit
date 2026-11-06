"""Data tables for report.md, built from summary.json (used by render.py; stdlib only).

t_NAME(summary) returns the Markdown lines of a `{{table:NAME}}` block; TABLES maps
NAME to it. Also the helpers the facts and charts modules share: rows of a summary
table, paths as shortened inline code (paths are untrusted), sizes, shares and counts.
"""
from __future__ import annotations

import os
import re
import tomllib
from datetime import date

import common as C
import summary as S


def _rows(t: dict) -> list[dict]:
    return [dict(zip(t["cols"], r)) for r in t["rows"]]


def _code(p) -> str:
    """A path as inline code; paths are untrusted, so no backticks, pipes or newlines."""
    p = re.sub(r"[\x00-\x1f\x7f]", "?", str(p)).replace("`", "'").replace("|", "\\|")
    return f"`{p}`"


def _keep(parts: list[str], idx) -> str:
    """Join the components at `idx`, with '…' wherever components are left out."""
    out, prev = [], -1
    for i in sorted(set(idx) & set(range(len(parts)))):
        if i > prev + 1:
            out.append("…")
        out.append(parts[i])
        prev = i
    return "/".join(out + (["…"] if prev < len(parts) - 1 else []))


def _short(p, maxlen: int = 72) -> str:
    """Shorten a long path in the middle, keeping its first three (area and project)
    and last two components, then fewer."""
    p = str(p)
    parts = p.split("/")
    for idx in ((0, 1, 2, -2, -1), (0, 1, 2, -1), (0, 1, -1), (0, -1)):
        if len(p) <= maxlen:
            break
        p = _keep(parts, [i % len(parts) for i in idx])
    return p


def _path(p) -> str:
    return _code(_short(p))


def _paths(ps) -> list[str]:
    """Shorten a table's paths as inline code; paths that would shorten to the same text
    also keep the first component where they differ."""
    ps = [str(p) for p in ps]
    short = [_short(p) for p in ps]
    out = list(short)
    for i, p in enumerate(ps):
        twins = [q for j, q in enumerate(ps) if short[j] == short[i] and q != p]
        if twins:
            parts = p.split("/")
            k = min(next((n for n, (x, y) in enumerate(zip(parts, q.split("/"))) if x != y),
                         len(parts) - 1) for q in twins)
            out[i] = _keep(parts, (0, 1, 2, k, len(parts) - 2, len(parts) - 1))
    return [_code(x) for x in out]


def _pair(a: str, b: str, maxlen: int = 60) -> tuple[str, str]:
    """Two paths shortened so they stay distinguishable: each keeps its first three
    components, the first component where they differ, and its last component."""
    pa, pb = a.split("/"), b.split("/")
    k = next((i for i, (x, y) in enumerate(zip(pa, pb)) if x != y), min(len(pa), len(pb)))
    def one(parts):
        q = "/".join(parts)
        return q if len(q) <= maxlen else _keep(parts, (0, 1, 2, k, len(parts) - 1))
    return _code(one(pa)), _code(one(pb))


def _clip(name, maxlen: int = 40) -> str:
    name = str(name)
    return name if len(name) <= maxlen else name[:maxlen // 2 - 1] + "…" + name[-(maxlen // 2 - 1):]


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _parent(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else "."


def _cutoff(s: dict, years: int) -> str:
    """The summary's date minus `years`, as YYYY-MM-DD ('' if unknown)."""
    g = (s.get("generated") or "")[:10]
    return f"{int(g[:4]) - years}{g[4:]}" if len(g) == 10 else ""


def _n(x) -> str:
    return "" if x is None else f"{x:,}"


def _pct(part, whole) -> str:
    if not whole:
        return ""
    x = 100 * (part or 0) / whole
    return "<0.1%" if 0 < x < 0.1 else f"{x:.1f}%"


def _md(head: list[str], body: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in body]
    return out


def _md_or_none(head: list[str], body: list[list]) -> list[str]:
    return _md(head, body) if body else ["None found."]


def _showing(shown: int, total: int, what: str = "rows") -> list[str]:
    return [f"Showing {shown:,} of {total:,} {what}."] if total > shown else []


def _total(s: dict, key: str, table: dict) -> int:
    return s.get("capped", {}).get(key, [0, len(table["rows"])])[1]


def _content(cats, nb) -> str:
    return ", ".join(f"{c} {_pct(b, nb)}" for c, b in cats or [] if nb)


def _folder_rows(s: dict, rows: list[dict]) -> list[list]:
    tb = s["totals"]["bytes"]
    out = []
    for name, r in zip(_paths(r["path"] for r in rows), rows):
        size = C.human_bytes(r["bytes"])
        if r["status"] != "ok":
            size += f" ({r['status']})"
        out.append([name, size, _pct(r["bytes"], tb), _n(r["files"]),
                    _content(r["top_categories"], r["bytes"]), r["top_owner"] or "",
                    r["newest"] or "", r["last_activity"] or ""])
    return out


_FOLDER_HEAD = ["Folder", "Size", "Share", "Files", "Main content", "Main owner",
                "Newest file", "Last activity"]


def t_areas_top(s):
    rows = sorted((r for r in _rows(s["folders"]) if r["level"] == 1),
                  key=lambda r: (-r["bytes"], r["path"]))
    body = _folder_rows(s, rows)
    if o := s.get("folders_other"):
        body.append([f"other ({o['n']:,} folders)", C.human_bytes(o["bytes"]),
                     _pct(o["bytes"], s["totals"]["bytes"]), _n(o["files"]), "", "", "", ""])
    return _md(_FOLDER_HEAD, body)


def t_areas_detail(s, limit=15):
    rows = sorted((r for r in _rows(s["folders"]) if r["level"] == 2),
                  key=lambda r: (-r["bytes"], r["path"]))
    total = s["folder_levels"]["2"]
    return _md(_FOLDER_HEAD, _folder_rows(s, rows[:limit])) + _showing(
        min(limit, len(rows)), total, "folders at this level")


def _user_note(r: dict, cut: str) -> str:
    """Note for a users row: not in the group or deleted, and no new files since `cut`."""
    out = [] if r["in_group"] else ["not in group" if r["user"] else "deleted account"]
    if cut and r["newest"] and r["newest"] < cut:
        out.append("no new files in 2+ years")
    return "; ".join(out)


def t_users(s, limit=15):
    u = s["users"]
    cut = _cutoff(s, 2)
    body = [[r["user"] or f"(deleted, uid {r['uid']})", "yes" if r["in_group"] else "no",
             C.human_bytes(r["bytes"]), _n(r["files"]), r["newest"] or "", _user_note(r, cut)]
            for r in _rows(u["table"])[:limit]]
    return _md(["User", "In group", "Size", "Files", "Newest file", "Note"], body) + _showing(
        len(body), u["n"], "users")


MIXED = 0.8      # a folder is mixed when no file owner holds this share of its bytes


def _owner_shares(r: dict) -> list[tuple[str, int]]:
    """[(owner, bytes)] of a folders row, largest first, 'others' last. A folder with one
    owner has an empty `owners` list, so that owner comes from `top_owner`."""
    own = [(u, b) for u, b in r.get("owners") or []]
    if not own and r.get("top_owner"):
        own = [(r["top_owner"], r["bytes"])]
    if r.get("other_owners_bytes"):
        own.append(("others", r["other_owners_bytes"]))
    return own


def _is_mixed(r: dict) -> bool:
    own = _owner_shares(r)
    tot = sum(b for _, b in own)
    return bool(tot) and len(own) > 1 and own[0][1] / tot < MIXED


def _dir_owner_differs(r: dict) -> bool:
    return bool(r.get("dir_owner") and r.get("top_owner") and r["dir_owner"] != r["top_owner"])


def _shares_text(r: dict) -> str:
    own = _owner_shares(r)
    tot = sum(b for _, b in own)
    def pct(b):
        x = 100 * b / tot
        return "<1%" if 0 < x < 0.5 else f"{round(x)}%"
    return ", ".join(f"{u} {pct(b)}" for u, b in own) if tot else ""


def _mixed_rows(s) -> list[dict]:
    """The deepest listed folders with mixed file owners or a folder owner who isn't the
    main file owner: a flagged folder with a flagged listed subfolder is left out."""
    flagged = [r for r in _rows(s["folders"]) if _is_mixed(r) or _dir_owner_differs(r)]
    paths = [r["path"] for r in flagged]
    deep = [r for r in flagged if not any(
        q.startswith(r["path"] + "/") or (r["path"] == "." and q != ".") for q in paths)]
    return sorted(deep, key=lambda r: (-r["bytes"], r["path"]))


def t_owners(s, limit=15):
    """Who owns what: only a file's owner can move or change it."""
    u, tb = s["users"], s["totals"]["bytes"]
    cut = _cutoff(s, 2)

    def where(r):
        return ", ".join(f"{_code(p)} {_pct(b, r['bytes'])}" for p, b in r.get("top_folders") or [])
    body = [[r["user"] or f"(deleted, uid {r['uid']})", C.human_bytes(r["bytes"]),
             _pct(r["bytes"], tb), _n(r["files"]), where(r), r["newest"] or "", _user_note(r, cut)]
            for r in _rows(u["table"])[:limit]]
    return _md(["Owner", "Size", "Share", "Files", "Main folders (share of theirs)",
                "Newest file", "Note"], body) + _showing(len(body), u["n"], "owners")


def t_mixed_folders(s, limit=12):
    rows = _mixed_rows(s)
    body = [[name, C.human_bytes(r["bytes"]), _shares_text(r),
             (r.get("dir_owner") or "") + (" (differs)" if _dir_owner_differs(r) else "")]
            for name, r in zip(_paths(r["path"] for r in rows[:limit]), rows[:limit])]
    return _md_or_none(["Folder", "Size", "File owners", "Folder owner"], body) + _showing(
        len(body), len(rows), "folders")


def _frac(x) -> str:
    return "?" if x is None else f"{round(100 * x)}%"


def t_duplicates(s, limit=12):
    """Folder pairs and single-file groups in one list, largest first. A file group whose
    copies all sit in a listed folder pair is counted in that pair's row."""
    d = s["duplicates"]
    groups = _rows(d["top_groups"])
    gdirs = [{_parent(p) for p in g["paths"]} for g in groups]
    items = []
    for o in _rows(d["dir_overlap"]):
        cover = [g for g, ds in zip(groups, gdirs) if {o["dir_a"], o["dir_b"]} <= ds]
        if cover and o["shared_files"] <= len(cover):
            continue                # only copies of listed groups: they show as group rows
        items.append({"rec": o["shared_bytes"], "pair": o, "groups": []})
    for g, ds in zip(groups, gdirs):
        home = next((it for it in items if "pair" in it
                     and ds <= {it["pair"]["dir_a"], it["pair"]["dir_b"]}), None)
        if home:
            home["groups"].append(g)
            home["rec"] = max(home["pair"]["shared_bytes"],
                              sum(x["reclaimable_outside_pipeline_work"] for x in home["groups"]))
        else:
            items.append({"rec": g["reclaimable_outside_pipeline_work"], "group": g})
    items.sort(key=lambda it: -it["rec"])
    body = []
    for it in items[:limit]:
        if "pair" in it:
            o, gs = it["pair"], it["groups"]
            copies = max([2] + [x["copies_outside_pipeline_work"] for x in gs])
            body.append([f"{_plural(o['shared_files'], 'file', 'files')} ({_frac(o.get('frac_a'))} / "
                         f"{_frac(o.get('frac_b'))} of each folder)", _n(copies),
                         C.human_bytes(o["shared_bytes"]), C.human_bytes(it["rec"]),
                         " and ".join(_pair(o["dir_a"], o["dir_b"]))])
        else:
            g = it["group"]
            dirs = list(dict.fromkeys(_parent(p) for p in g["paths"]))[:2]
            more = g["copies_outside_pipeline_work"] - sum(_parent(p) in dirs for p in g["paths"])
            where = " and ".join(_pair(*dirs)) if len(dirs) == 2 else ", ".join(_paths(dirs))
            body.append([_code(os.path.basename(g["paths"][0])) if g["paths"] else "",
                         _n(g["copies_outside_pipeline_work"]), C.human_bytes(g["size"]),
                         C.human_bytes(g["reclaimable_outside_pipeline_work"]),
                         where + (f" (+{more} more)" if more > 0 else "")])
    n_groups = _total(s, "duplicates.top_groups", d["top_groups"])
    n_pairs = d["n_dir_overlap"]
    return _md_or_none(["Item", "Copies", "Size each", "Reclaimable", "Where"], body) + (
        [f"Largest first: {len(body):,} items from {n_groups:,} duplicate groups and {n_pairs:,} "
         "folder pairs outside pipeline work."] if body else [])


def t_dir_trees(s, limit=10):
    t = s["duplicates"]["dir_trees"]
    body = [[_n(r["copies"]), C.human_bytes(r["bytes_each"]), _n(r["files_each"]),
             C.human_bytes(r["reclaimable"]), ", ".join(_path(p) for p in r["paths"][:2])]
            for r in _rows(t["top"])[:limit]]
    return _md(["Copies", "Size each", "Files each", "Reclaimable", "Example paths"],
               body) + _showing(len(body), t["n"], "duplicate trees")


def t_unreadable(s, limit=15):
    p = s["problem_dirs"]
    rows = _rows(p["groups"])[:limit]
    body = [[name, r["status"], _n(r["n"]),
             ", ".join(_code(_clip(e)) for e in r["examples"]), ", ".join(r["owners"]),
             _n(r["owners_outside_group"]), r["newest"] or ""]
            for name, r in zip(_paths(r["parent"] for r in rows), rows)]
    return _md(["Parent", "Status", "Count", "Examples", "Owners", "Owners outside group",
                "Newest"], body) + _showing(len(body), p["n_groups"], "groups")


def t_raw_data(s, limit=10):
    r_ = s["raw_locations"]
    rows = _rows(r_["top"])[:limit]
    body = [[name, r["ftype"], _n(r["dirs"]), _n(r["files"]), C.human_bytes(r["bytes"]),
             r["newest"] or ""] for name, r in zip(_paths(r["path"] for r in rows), rows)]
    return _md(["Folder", "Type", "Dirs", "Files", "Size", "Newest"], body) + _showing(
        len(body), _total(s, "raw_locations.top", r_["top"]), "locations")


def _hints() -> dict[str, str]:
    """rules.toml [[hint]] entries: extensions we can only guess at (not classified)."""
    with open(C.SCRIPTS_DIR / "rules.toml", "rb") as fh:
        rules = tomllib.load(fh)
    return {e.lower(): h["likely"] for h in rules.get("hint", []) for e in h.get("ext", [])}


def t_unknown_types(s, limit=10):
    u = s["unknown_exts"]
    hints = _hints()
    body = [[_code(r["ext"]) if r["ext"] else "(none)", _n(r["files"]),
             C.human_bytes(r["bytes"]), _n(r["dirs"]), r["newest"] or "",
             hints.get((r["ext"] or "").lower(), "")]
            for r in _rows(u["top"])[:limit]]
    return _md(["Extension", "Files", "Size", "Dirs", "Newest", "Likely"], body) + _showing(
        len(body), u["n"], "unknown extensions")


def t_pipeline_workdirs(s, limit=10):
    w = s["pipeline_workdirs"]
    rows = _rows(w["top"])[:limit]
    body = [[name, r["marker"], C.human_bytes(r["bytes"]), _n(r["files"]), r["newest"] or ""]
            for name, r in zip(_paths(r["path"] for r in rows), rows)]
    return _md(["Folder", "Kind", "Size", "Files", "Newest"], body) + _showing(
        len(body), w["n"], "work dirs")


def t_age(s, span=10):
    """Files by year of last change; years more than `span` before the run are one row."""
    rows = _rows(s["age_by_year"])
    old = int((s.get("generated") or "0")[:4]) - span
    early = [r for r in rows if r["year"] and int(r["year"]) <= old]
    body = ([[f"{old} and earlier", _n(sum(r["files"] for r in early)),
              C.human_bytes(sum(r["bytes"] for r in early))]] if len(early) > 1 else [])
    body += [[r["year"] or "(none)", _n(r["files"]), C.human_bytes(r["bytes"])]
             for r in rows if not (len(early) > 1 and r in early)]
    return _md(["Year", "Files", "Size"], body)


def t_size_histogram(s):
    def label(b):
        return "0 bytes" if b == 0 else f"from {b:,} bytes"
    return _md(["File size (decimal)", "Files", "Size"], [
        [label(r["from_bytes"]), _n(r["files"]), C.human_bytes(r["bytes"])]
        for r in _rows(s["size_histogram"])])


def t_by_category(s):
    tb = s["totals"]["bytes"]
    return _md(["Category", "Files", "Size", "Share"], [
        [r["category"], _n(r["files"]), C.human_bytes(r["bytes"]), _pct(r["bytes"], tb)]
        for r in _rows(s["by_category"])])


def t_types(s, limit=10):
    tb = s["totals"]["bytes"]
    body = [[r["ftype"], r["category"], C.human_bytes(r["bytes"]), _pct(r["bytes"], tb),
             _n(r["files"]), r["newest"] or ""] for r in _rows(s["types"])[:limit]]
    return _md(["Type", "Category", "Size", "Share", "Files", "Newest"], body) + _showing(
        len(body), len(s["types"]["rows"]), "types")


def t_sensitive(s, limit=5):
    x = s["sensitive"]
    rows = _rows(x["top_dirs_elsewhere"])[:limit]
    body = [[name, _n(r["files"])] for name, r in zip(_paths(r["dir"] for r in rows), rows)]
    return _md_or_none(["Folder with credential-like names (outside software environments)",
                        "Files"], body) + _showing(len(body), x["n_dirs_elsewhere"], "folders")


SMALL_AVG = 1 << 20          # "many small files": average under 1 MiB ...
SMALL_MIN_FILES = 100_000    # ... over at least this many files


def t_small_files(s, limit=8):
    """Folders with many small files: the deepest listed folders (no listed subfolder)."""
    rows = [r for r in _rows(s["folders"]) if r["path"] != "." and r["files"]]
    paths = [r["path"] for r in rows]
    leaf = [r for r in rows if not any(p.startswith(r["path"] + "/") for p in paths)]
    small = sorted((r for r in leaf if r["files"] >= SMALL_MIN_FILES
                    and r["bytes"] / r["files"] < SMALL_AVG), key=lambda r: (-r["files"], r["path"]))
    body = [[name, _n(r["files"]), C.human_bytes(r["bytes"]), C.human_bytes(r["bytes"] / r["files"])]
            for name, r in zip(_paths(r["path"] for r in small[:limit]), small[:limit])]
    return _md_or_none(["Folder", "Files", "Size", "Average file"], body) + _showing(
        len(body), len(small), "folders")


def t_touched_old(s, limit=8):
    """Folders whose newest file is a year or more older than their last activity."""
    def gap(r):
        a, b = r.get("newest"), r.get("last_activity")
        return (date.fromisoformat(b) - date.fromisoformat(a)).days if a and b else 0
    rows = sorted((r for r in _rows(s["folders"]) if gap(r) >= 365),
                  key=lambda r: (-r["bytes"], r["path"]))
    body = [[name, C.human_bytes(r["bytes"]), r["newest"], r["last_activity"]]
            for name, r in zip(_paths(r["path"] for r in rows[:limit]), rows[:limit])]
    return _md_or_none(["Folder", "Size", "Newest file", "Last activity"], body) + _showing(
        len(body), len(rows), "folders")


def _old_years(s) -> int:
    return S.OLD_YEARS.get(s.get("mode"), 2)


def _old_bytes(s, years: int) -> int:
    """Bytes last changed in (summary year - years) or earlier."""
    year = int(s["generated"][:4])
    return sum(r["bytes"] for r in _rows(s["age_by_year"])
               if r["year"] and int(r["year"]) <= year - years)


def _cat(a: dict, *names) -> int:
    return sum(b for c, b in a.get("by_category") or [] if c in names)


def t_raw_access(s, limit=12):
    """Where raw data sits, by area."""
    areas = [a for a in _rows(s["areas"]["table"]) if a["raw_bytes"]]
    total = sum(a["raw_bytes"] for a in areas)
    areas.sort(key=lambda a: (-a["raw_bytes"], a["path"]))
    body = [[_code(a["path"]), C.human_bytes(a["raw_bytes"]), _pct(a["raw_bytes"], total),
             _pct(a["raw_bytes"], a["bytes"])] for a in areas[:limit]]
    return _md_or_none(["Area", "Raw data", "Share of all raw", "Share of the area"], body) + \
        _showing(len(body), len(areas), "areas")


def t_links(s, limit=10):
    """Symlinks from one area into another, or out of the scanned target."""
    rows = _rows(s["life_cycle"]["links"]["top"])
    def name(p):                                    # "." = a folder outside every area
        return {"(outside)": "(outside the target)", ".": "(other folders)"}.get(p) or _code(p)
    body = [[name(r["from"]), name(r["to"]), _n(r["links"])] for r in rows[:limit]]
    return _md_or_none(["From", "To", "Links"], body)


_SHARED = (("Reference genomes, indexes, annotation", ("reference",), "reference"),
           ("Container images", ("container",), "container"))


def t_shared(s):
    """Resources several areas each hold: candidates for one shared lab copy."""
    areas = _rows(s["areas"]["table"])
    across = {r["kind"]: r for r in _rows(s["life_cycle"]["copied_across_areas"])}

    def largest(key):
        top = sorted((a for a in areas if key(a)), key=lambda a: (-key(a), a["path"]))[:3]
        return ", ".join(_code(a["path"]) for a in top)

    def copied(kind):
        r = across.get(kind)
        return (f"{C.human_bytes(r['reclaimable'])} in {_plural(r['groups'], 'group', 'groups')}"
                if r else "none found")
    body = []
    for label, cats, kind in _SHARED:
        held = [a for a in areas if _cat(a, *cats)]
        body.append([label, _n(len(held)), C.human_bytes(sum(_cat(a, *cats) for a in held)),
                     copied(kind), largest(lambda a, c=cats: _cat(a, *c))])
    envs = [a for a in areas if a["envs"]]
    body.append([f"Software environments ({_n(sum(a['envs'] for a in envs))})", _n(len(envs)),
                 C.human_bytes(sum(a["env_bytes"] for a in envs)), "not compared",
                 largest(lambda a: a["env_bytes"])])
    return _md(["Resource", "Areas holding it", "Size", "Copies across areas", "Largest in"], body)


def t_old_areas(s, limit=12):
    """Top-level areas by bytes not changed in old_years, with former members' bytes."""
    yrs = _old_years(s)
    newest = {r["path"]: r["newest"] for r in _rows(s["folders"])}
    areas = sorted((a for a in _rows(s["areas"]["table"]) if a["old_bytes"] or a["former_bytes"]),
                   key=lambda a: (-a["old_bytes"], a["path"]))
    body = [[_code(a["path"]), C.human_bytes(a["bytes"]), C.human_bytes(a["old_bytes"]),
             _pct(a["old_bytes"], a["bytes"]), C.human_bytes(a["former_bytes"]) if a["former_bytes"] else "",
             newest.get(a["path"]) or ""] for a in areas[:limit]]
    return _md_or_none(["Area", "Size", f"Unchanged {yrs}+ years", "Share of the area",
                        "Former members' data", "Newest file"], body) + _showing(
        len(body), len(areas), "areas")


TABLES = {name[2:]: fn for name, fn in globals().items() if name.startswith("t_")}
