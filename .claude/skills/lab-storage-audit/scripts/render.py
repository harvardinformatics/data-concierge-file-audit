#!/usr/bin/env python3
"""Render <run>/report.md into a self-contained <run>/report.html (stdlib only).

Supports the Markdown subset the report template uses: headings, paragraphs,
nested lists, GFM pipe tables, fenced code, blockquotes, rules, `code`,
**bold**, *italic* and [links](https://…). Underscores are never emphasis,
because lab paths are full of them. Everything is HTML-escaped first (paths come
from the target and are untrusted); links are kept only for http(s)/mailto
and relative URLs. Inline CSS, light/dark theme, no external resources.

Data tables are filled in by script, not written by the agent: a line
`{{table:NAME}}` in report.md becomes a Markdown table built from summary.json,
wrapped in `<!-- table:NAME -->` ... `<!-- /table -->`, and report.md is rewritten
with it. A re-render regenerates every wrapped table, so the tables are identical
for the same summary.json. Names: see TABLES.

Fixed sentences work the same way: `{{facts:NAME}}` becomes bullets written from
summary.json (totals, coverage, method and limits), so the agent writes only
interpretation. Names: see FACTS.

After filling, a survey report is linted (warnings only): prose over a section's
word budget, numbers that repeat a table or facts block in the same section, and
hand-written row counts ("showing N of M"; the tables print their own).

  python3 render.py --run audits/<run>
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tomllib
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(SCRIPTS, *[os.pardir] * 4))   # excluded when inside a target

CSS = """
:root{--bg:#fff;--fg:#1d2025;--muted:#5d6470;--line:#d9dde3;--soft:#f3f5f8;--accent:#a51c30}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e4e6ea;--muted:#9aa1ad;
 --line:#343842;--soft:#20232a;--accent:#e0707f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.7em;margin:.2em 0 .6em}h2{font-size:1.3em;margin:1.8em 0 .5em;
 padding-bottom:.25em;border-bottom:1px solid var(--line)}h3{font-size:1.08em;margin:1.4em 0 .4em}
a{color:var(--accent)}code{font:.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 background:var(--soft);padding:.1em .3em;border-radius:4px;overflow-wrap:anywhere}
pre{background:var(--soft);padding:12px;border-radius:6px;overflow-x:auto}pre code{padding:0}
.tw{overflow-x:auto;margin:.8em 0}table{border-collapse:collapse;font-size:.92em;min-width:50%}
th,td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top}
th{background:var(--soft)}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
blockquote{margin:.8em 0;padding:.4em 1em;border-left:3px solid var(--accent);background:var(--soft)}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
nav{font-size:.92em;color:var(--muted);margin-bottom:1.5em}nav a{margin-right:.9em;white-space:nowrap}
@media print{nav{display:none}a{color:inherit}}
"""

_LIST = re.compile(r"^( *)([-*+]|\d+[.)]) +(.*)$")
_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_NUM = re.compile(r"^[~<>≈]?\s*[-+]?[\d.,]+\s*(%|[KMGTP]i?B|B|[KMGT]|x)?$")


def _safe_url(u: str) -> str | None:
    u = html.unescape(u).strip()
    if re.match(r"^(https?:|mailto:)", u, re.I) or not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
        return html.escape(u, quote=True)
    return None


def inline(s: str) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"
    s = re.sub(r"`([^`]+)`", keep, s)
    s = html.escape(s, quote=False)

    def link(m):
        u = _safe_url(m.group(2))
        return f'<a href="{u}">{m.group(1)}</a>' if u else m.group(0)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _table(rows: list[str]) -> str:
    head, body = _cells(rows[0]), [_cells(r) for r in rows[2:]]
    num = [bool(body) and all(_NUM.match(r[i]) or not r[i] for r in body if i < len(r))
           for i in range(len(head))]
    cls = lambda i: ' class="n"' if i < len(num) and num[i] else ""  # noqa: E731
    out = ["<div class=tw><table><thead><tr>"]
    out += [f"<th{cls(i)}>{inline(c)}</th>" for i, c in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td{cls(i)}>{inline(c)}</td>" for i, c in enumerate(r))
                   + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _list(lines: list[str]) -> str:
    items: list[list] = []       # [indent, tag, text]; continuation lines join the item
    for ln in lines:
        m = _LIST.match(ln)
        if m:
            items.append([len(m.group(1)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)])
        elif items:
            items[-1][2] += " " + ln.strip()

    def emit(k: int, ind: int) -> tuple[str, int]:
        tag = items[k][1]
        out = [f"<{tag}>"]
        while k < len(items) and items[k][0] >= ind:
            if items[k][0] == ind and items[k][1] != tag:
                break                            # '-' list followed by '1.' list
            if items[k][0] > ind:                # deeper: nest inside the previous item
                sub, k = emit(k, items[k][0])
                if out[-1].endswith("</li>"):
                    out[-1] = out[-1][:-5] + sub + "</li>"
                else:
                    out.append(f"<li>{sub}</li>")
                continue
            out.append(f"<li>{inline(items[k][2])}</li>")
            k += 1
        out.append(f"</{tag}>")
        return "".join(out), k

    parts, k = [], 0
    while k < len(items):        # an item less indented than the first starts a new list
        s, k = emit(k, items[k][0])
        parts.append(s)
    return "".join(parts)


def to_html(md: str) -> tuple[str, str, list[tuple[str, str]]]:
    lines = md.splitlines()
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    title = "Lab storage audit"
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
        elif m := re.match(r"^(#{1,6})\s+(.*)$", ln):
            n, text = len(m.group(1)), m.group(2).strip()
            hid = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or f"h{i}"
            if n == 1 and title == "Lab storage audit":
                title = re.sub(r"[`*]", "", text)
            if n == 2:
                toc.append((hid, re.sub(r"[`*]", "", text)))
            out.append(f'<h{n} id="{hid}">{inline(text)}</h{n}>')
            i += 1
        elif re.match(r"^(-{3,}|\*{3,})\s*$", ln):
            out.append("<hr>")
            i += 1
        elif ln.lstrip().startswith("|") and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
        elif ln.startswith(">"):
            j = i
            while j < len(lines) and lines[j].startswith(">"):
                j += 1
            inner = to_html("\n".join(re.sub(r"^> ?", "", x) for x in lines[i:j]))[1]
            out.append(f"<blockquote>{inner}</blockquote>")
            i = j
        elif _LIST.match(ln):
            j = i + 1
            while j < len(lines) and lines[j].strip() and (
                    _LIST.match(lines[j]) or lines[j].startswith("  ")):
                j += 1
            out.append(_list(lines[i:j]))
            i = j
        elif not ln.strip() or re.match(r"^<!--.*-->\s*$", ln):    # blank or table marks
            i += 1
        else:
            j = i + 1
            while j < len(lines) and lines[j].strip() and not re.match(
                    r"^(#{1,6}\s|```|>|\||<!--|(-{3,}|\*{3,})\s*$)", lines[j]) and not _LIST.match(lines[j]):
                j += 1
            out.append("<p>" + inline(" ".join(x.strip() for x in lines[i:j])) + "</p>")
            i = j
    return title, "\n".join(out), toc


def render(md: str) -> str:
    title, body, toc = to_html(md)
    nav = ("<nav>" + "".join(f'<a href="#{h}">{html.escape(t)}</a>' for h, t in toc)
           + "</nav>") if len(toc) > 2 else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{nav}{body}</main></body></html>\n")


# ------------------------------------------------------------- tables from summary.json

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
    total = s.get("folder_levels", {}).get("2", len(rows))
    return _md(_FOLDER_HEAD, _folder_rows(s, rows[:limit])) + _showing(
        min(limit, len(rows)), total, "folders at this level")


def t_users(s, limit=15):
    u = s["users"]
    cut = _cutoff(s, 2)

    def note(r):
        out = [] if r["in_group"] else ["not in group" if r["user"] else "deleted account"]
        if cut and r["newest"] and r["newest"] < cut:
            out.append("no new files in 2+ years")
        return "; ".join(out)
    body = [[r["user"] or f"(deleted, uid {r['uid']})", "yes" if r["in_group"] else "no",
             C.human_bytes(r["bytes"]), _n(r["files"]), r["newest"] or "", note(r)]
            for r in _rows(u["table"])[:limit]]
    return _md(["User", "In group", "Size", "Files", "Newest file", "Note"], body) + _showing(
        len(body), u["n"], "users")


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
    n_pairs = d.get("n_dir_overlap", len(d["dir_overlap"]["rows"]))
    return _md_or_none(["Item", "Copies", "Size each", "Reclaimable", "Where"], body) + (
        [f"Largest first: {len(body):,} items from {n_groups:,} duplicate groups and {n_pairs:,} "
         "folder pairs outside pipeline work."] if body else [])


def t_big_duplicates(s, limit=10):
    t = s["duplicates"]["top_groups"]
    body = [[C.human_bytes(r["size"]), _n(r["copies_outside_pipeline_work"]),
             C.human_bytes(r["reclaimable_outside_pipeline_work"]),
             _path(r["paths"][0]) if r["paths"] else ""] for r in _rows(t)[:limit]]
    return _md(["Size each", "Copies", "Reclaimable", "Example path"], body) + _showing(
        len(body), _total(s, "duplicates.top_groups", t), "duplicate groups")


def t_dir_overlap(s, limit=10):
    t = s["duplicates"]["dir_overlap"]
    body = [[*_pair(r["dir_a"], r["dir_b"]), C.human_bytes(r["shared_bytes"]),
             _n(r["shared_files"])] for r in _rows(t)[:limit]]
    return _md(["Folder A", "Folder B", "Shared", "Files"], body) + _showing(
        len(body), _total(s, "duplicates.dir_overlap", t), "folder pairs")


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
    with open(os.path.join(SCRIPTS, "rules.toml"), "rb") as fh:
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
                        "Files"], body) + _showing(len(body), x.get("n_dirs_elsewhere", 0), "folders")


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


TABLES = {name[2:]: fn for name, fn in globals().items() if name.startswith("t_")}

# ------------------------------------------------------------- fixed sentences (facts)

def _size(b) -> str:            # thresholds read better without ".0": "256 MiB"
    return C.human_bytes(b).replace(".0 ", " ")


def f_scope(s):
    r, t = s["run"], s["totals"]
    dp = s.get("post_stats", {}).get("dupes", {})
    tgt, survey = r["target"], s.get("mode") == "survey"

    def rel(p):
        q = os.path.relpath(p, tgt) if p.startswith(tgt + "/") else p
        return _code(q) + (" (this tool's repo, always excluded)"
                           if os.path.realpath(p) == REPO else "")
    roots = [x for x in r.get("roots", ["."]) if x != "."]
    read = (f"- **Read:** metadata for {_n(t['files'])} files, {_n(t.get('symlinks', 0))} "
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
    inodes = t["files"] + t["dirs"] + t.get("symlinks", 0) + t.get("other", 0)
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
            f"{_n(x.get('n_dirs_elsewhere', 0))} folders. Matched by name only, never opened"
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
           f"{_n(d.get('n_dir_overlap', 0))} pairs of folders share duplicate files.",
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


FACTS = {name[2:]: fn for name, fn in globals().items() if name.startswith("f_")}

# the blocks each template asks for; a report missing one gets a warning
REQUIRED = {
    "survey": ("facts:scope", "facts:storage", "areas_top", "areas_detail", "small_files",
               "facts:pipeline", "pipeline_workdirs", "touched_old", "users", "age", "facts:age",
               "facts:types", "types", "raw_data", "unknown_types", "sensitive",
               "facts:duplicates", "duplicates", "facts:unreadable", "unreadable",
               "size_histogram", "by_category"),
    "deep": ("areas_top", "areas_detail", "users", "raw_data", "big_duplicates", "dir_trees",
             "dir_overlap", "pipeline_workdirs", "unreadable", "age", "by_category",
             "unknown_types"),
}
_MARK = re.compile(r"^\{\{(table|facts):([a-z_]+)\}\}\s*$")
_REGION = re.compile(r"^<!-- (table|facts):([a-z_]+) -->$")
_KINDS = {"table": TABLES, "facts": FACTS}


def expand(md: str, summary: dict) -> tuple[str, list[str]]:
    """Fill every {{table:NAME}} / {{facts:NAME}} marker and regenerate every wrapped block.
    Returns (new markdown, blocks used: table names bare, facts as 'facts:NAME').
    Unknown names raise ValueError."""
    out, used = [], []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _MARK.match(ln) or _REGION.match(ln)
        if not m:
            out.append(ln)
            i += 1
            continue
        kind, name = m.groups()
        if name not in _KINDS[kind]:
            raise ValueError(f"unknown {kind} {name!r}; valid: {', '.join(sorted(_KINDS[kind]))}")
        if _REGION.match(ln):                       # skip the old block up to its end mark
            while i < len(lines) and lines[i] != f"<!-- /{kind} -->":
                i += 1
        try:
            block = _KINDS[kind][name](summary)
        except KeyError as ex:                      # section absent from this summary
            block = [f"Not available: `summary.json` has no {ex}."]
        out += [f"<!-- {kind}:{name} -->", *block, f"<!-- /{kind} -->"]
        used.append(name if kind == "table" else f"facts:{name}")
        i += 1
    return "\n".join(out) + "\n", used


# words of the agent's own prose per section (tables and generated blocks don't count)
BUDGETS = {"survey": {"Key findings": 160, "Scope and method": 20, "Storage usage": 60,
                      "Areas": 90, "Users": 60, "Age": 60, "Data types": 90,
                      "Big duplicates": 90, "Unreadable directories": 60,
                      "Recommended deep audits": 60, "Questions for the PI": 140,
                      "Flagged content": 40, "Appendix": 30}}
_FREE = ("Key findings", "Recommended deep audits", "Questions for the PI")   # may repeat numbers
_NUMS = re.compile(r"\d[\d,]*(?:\.\d+)?\s?(?:[KMGTP]iB|%)|\d{1,3}(?:,\d{3})+")


def lint(md: str, mode: str) -> list[str]:
    """Warnings for a filled report: sections over their prose budget, prose repeating a
    number from a table or facts block in the same section, hand-written row counts."""
    budgets = BUDGETS.get(mode)
    if not budgets:
        return []
    warns: list[str] = []
    for sec in re.split(r"^## ", md, flags=re.M)[1:]:
        title, _, body = sec.partition("\n")
        prose, data, inside = [], [], False
        for ln in body.splitlines():
            if _REGION.match(ln):
                inside = True
            elif re.match(r"^<!-- /(table|facts) -->$", ln):
                inside = False
            elif inside:
                data.append(ln)
            elif not ln.lstrip().startswith("|") and not re.match(r"^[^-*\s\d].*:\s*$", ln):
                prose.append(ln)                    # (lines ending in ':' are template captions)
        text = " ".join(prose)
        key = next((k for k in budgets if title.startswith(k)), None)
        words = len(re.findall(r"\S+", re.sub(r"\*\*[^*]+:\*\*|None\.", "", text)))
        if key and words > budgets[key]:
            warns.append(f"'{title}': {words} words of prose (budget {budgets[key]})")
        if re.search(r"\bshowing\s+\d", text, re.I):
            warns.append(f"'{title}': row counts are printed by the tables; don't write 'showing N'")
        if not title.startswith(_FREE):
            norm = lambda x: re.sub(r"\s", "", x)  # noqa: E731
            have = {norm(x) for x in _NUMS.findall(" ".join(data))}
            dup = sorted({x for x in _NUMS.findall(text) if norm(x) in have})
            if dup:
                warns.append(f"'{title}': prose repeats {', '.join(dup[:5])} from its tables")
    return warns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory containing report.md")
    args = ap.parse_args(argv)
    src = C.out_path(args.run, "report.md")
    with open(src, encoding="utf-8") as fh:
        md = fh.read()
    sj = C.out_path(args.run, "summary.json")
    if os.path.exists(sj):
        with open(sj, encoding="utf-8") as fh:
            summary = json.load(fh)
        md, used = expand(md, summary)
        C.log(f"filled: {', '.join(used) or 'none'}")
        mode = summary.get("mode", "deep")
        missing = [n for n in REQUIRED.get(mode, ()) if n not in used]
        if missing:
            C.log(f"WARNING: report.md has no {{{{table:...}}}}/{{{{facts:...}}}} line for: "
                  f"{', '.join(missing)}")
        for w in lint(md, mode):
            C.log(f"WARNING: {w}")
        with open(src + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(md)
        os.replace(src + ".tmp", src)
    page = render(md)
    dst = C.out_path(args.run, "report.html")
    with open(dst + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(dst + ".tmp", dst)
    C.log(f"wrote {dst} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
