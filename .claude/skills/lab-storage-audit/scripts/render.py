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

  python3 render.py --run audits/<run>
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

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


def _showing(shown: int, total: int, what: str = "rows") -> list[str]:
    return [f"Showing {shown:,} of {total:,} {what}."] if total > shown else []


def _total(s: dict, key: str, table: dict) -> int:
    return s.get("capped", {}).get(key, [0, len(table["rows"])])[1]


def _content(cats, nb) -> str:
    return ", ".join(f"{c} {_pct(b, nb)}" for c, b in cats or [] if nb)


def _folder_rows(s: dict, rows: list[dict]) -> list[list]:
    tb = s["totals"]["bytes"]
    out = []
    for r in rows:
        size = C.human_bytes(r["bytes"])
        if r["status"] != "ok":
            size += f" ({r['status']})"
        out.append([_code(r["path"]), size, _pct(r["bytes"], tb), _n(r["files"]),
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
    body = [[r["user"] or f"(deleted, uid {r['uid']})", "yes" if r["in_group"] else "no",
             C.human_bytes(r["bytes"]), _n(r["files"]), r["newest"] or ""]
            for r in _rows(u["table"])[:limit]]
    return _md(["User", "In group", "Size", "Files", "Newest file"], body) + _showing(
        len(body), u["n"], "users")


def t_big_duplicates(s, limit=10):
    t = s["duplicates"]["top_groups"]
    body = [[C.human_bytes(r["size"]), _n(r["copies_outside_pipeline_work"]),
             C.human_bytes(r["reclaimable_outside_pipeline_work"]),
             _code(r["paths"][0]) if r["paths"] else ""] for r in _rows(t)[:limit]]
    return _md(["Size each", "Copies", "Reclaimable", "Example path"], body) + _showing(
        len(body), _total(s, "duplicates.top_groups", t), "duplicate groups")


def t_dir_overlap(s, limit=10):
    t = s["duplicates"]["dir_overlap"]
    body = [[_code(r["dir_a"]), _code(r["dir_b"]), C.human_bytes(r["shared_bytes"]),
             _n(r["shared_files"])] for r in _rows(t)[:limit]]
    return _md(["Folder A", "Folder B", "Shared", "Files"], body) + _showing(
        len(body), _total(s, "duplicates.dir_overlap", t), "folder pairs")


def t_dir_trees(s, limit=10):
    t = s["duplicates"]["dir_trees"]
    body = [[_n(r["copies"]), C.human_bytes(r["bytes_each"]), _n(r["files_each"]),
             C.human_bytes(r["reclaimable"]), ", ".join(_code(p) for p in r["paths"][:2])]
            for r in _rows(t["top"])[:limit]]
    return _md(["Copies", "Size each", "Files each", "Reclaimable", "Example paths"],
               body) + _showing(len(body), t["n"], "duplicate trees")


def t_unreadable(s, limit=15):
    p = s["problem_dirs"]
    body = [[_code(r["parent"]), r["status"], _n(r["n"]),
             ", ".join(_code(e) for e in r["examples"]), ", ".join(r["owners"]),
             _n(r["owners_outside_group"]), r["newest"] or ""]
            for r in _rows(p["groups"])[:limit]]
    return _md(["Parent", "Status", "Count", "Examples", "Owners", "Owners outside group",
                "Newest"], body) + _showing(len(body), p["n_groups"], "groups")


def t_raw_data(s, limit=10):
    r_ = s["raw_locations"]
    body = [[_code(r["path"]), r["ftype"], _n(r["dirs"]), _n(r["files"]),
             C.human_bytes(r["bytes"]), r["newest"] or ""] for r in _rows(r_["top"])[:limit]]
    return _md(["Folder", "Type", "Dirs", "Files", "Size", "Newest"], body) + _showing(
        len(body), _total(s, "raw_locations.top", r_["top"]), "locations")


def t_unknown_types(s, limit=10):
    u = s["unknown_exts"]
    body = [[_code(r["ext"]) if r["ext"] else "(none)", _n(r["files"]),
             C.human_bytes(r["bytes"]), _n(r["dirs"]), r["newest"] or ""]
            for r in _rows(u["top"])[:limit]]
    return _md(["Extension", "Files", "Size", "Dirs", "Newest"], body) + _showing(
        len(body), u["n"], "unknown extensions")


def t_pipeline_workdirs(s, limit=10):
    w = s["pipeline_workdirs"]
    body = [[_code(r["path"]), r["marker"], C.human_bytes(r["bytes"]), _n(r["files"]),
             r["newest"] or ""] for r in _rows(w["top"])[:limit]]
    return _md(["Folder", "Kind", "Size", "Files", "Newest"], body) + _showing(
        len(body), w["n"], "work dirs")


def t_age(s):
    return _md(["Year", "Files", "Size"], [[r["year"] or "(none)", _n(r["files"]),
                                           C.human_bytes(r["bytes"])]
                                          for r in _rows(s["age_by_year"])])


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


TABLES = {name[2:]: fn for name, fn in globals().items() if name.startswith("t_")}
# the tables each template asks for; a report missing one gets a warning
REQUIRED = {
    "survey": ("areas_top", "areas_detail", "users", "age", "raw_data", "unknown_types",
               "big_duplicates", "dir_overlap", "unreadable", "size_histogram", "by_category"),
    "deep": ("areas_top", "areas_detail", "users", "raw_data", "big_duplicates", "dir_trees",
             "dir_overlap", "pipeline_workdirs", "unreadable", "age", "by_category",
             "unknown_types"),
}
_MARK = re.compile(r"^\{\{table:([a-z_]+)\}\}\s*$")
_REGION = re.compile(r"^<!-- table:([a-z_]+) -->$")


def expand(md: str, summary: dict) -> tuple[str, list[str]]:
    """Fill every {{table:NAME}} marker and regenerate every wrapped table.
    Returns (new markdown, names used). Unknown names raise ValueError."""
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
        name = m.group(1)
        if name not in TABLES:
            raise ValueError(f"unknown table {name!r}; valid: {', '.join(sorted(TABLES))}")
        if _REGION.match(ln):                       # skip the old table up to its end mark
            while i < len(lines) and lines[i] != "<!-- /table -->":
                i += 1
        try:
            table = TABLES[name](summary)
        except KeyError as ex:                      # section absent from this summary
            table = [f"Not available: `summary.json` has no {ex}."]
        out += [f"<!-- table:{name} -->", *table, "<!-- /table -->"]
        used.append(name)
        i += 1
    return "\n".join(out) + "\n", used


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
        C.log(f"filled tables: {', '.join(used) or 'none'}")
        missing = [n for n in REQUIRED.get(summary.get("mode", "deep"), ()) if n not in used]
        if missing:
            C.log(f"WARNING: report.md has no {{{{table:...}}}} line for: {', '.join(missing)}")
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
