#!/usr/bin/env python3
"""Render <run>/report.md into a self-contained <run>/report.html (stdlib only).

Supports the Markdown subset the report template uses: headings, paragraphs,
nested lists, GFM pipe tables, fenced code, blockquotes, rules, `code`,
**bold**, *italic* and [links](https://…). Underscores are never emphasis,
because lab paths are full of them. Everything is HTML-escaped first (paths come
from the target and are untrusted); links are kept only for http(s)/mailto
and relative URLs. Inline CSS and script, light/dark theme, no external resources.

Data tables are filled in by script, not written by the agent: a line
`{{table:NAME}}` in report.md becomes a Markdown table built from summary.json,
wrapped in `<!-- table:NAME -->` ... `<!-- /table -->`, and report.md is rewritten
with it. A re-render regenerates every wrapped table, so the tables are identical
for the same summary.json. Names: see TABLES.

Fixed sentences work the same way: `{{facts:NAME}}` becomes bullets written from
summary.json (totals, coverage, method and limits), so the agent writes only
interpretation. Names: see FACTS.

Charts too: `{{chart:NAME}}` becomes a one-line caption in report.md and an inline
SVG (with a small inline script for hover) in report.html. Names: see CHARTS.

In report.html each `##` section and each table is collapsible: a table is labelled by
the caption line above it ("Top-level folders:") or TABLE_TITLES, and only the first
table under each heading starts open. The Patterns bullet is drawn as a call-out.

After filling, a survey report is linted (warnings only): prose over a section's
word budget, numbers that repeat a table or facts block in the same section, and
hand-written row counts ("showing N of M"; the tables print their own).

  python3 render.py --run audits/<run>
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import tomllib
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(SCRIPTS, *[os.pardir] * 4))   # excluded when inside a target

CSS = """
:root{--bg:#fff;--fg:#1d2025;--muted:#5d6470;--line:#d9dde3;--soft:#f3f5f8;--accent:#a51c30;
 --k0:#2a78d6;--k1:#eb6834;--k2:#1baf7a;--k3:#eda100;--k4:#e87ba4;--k5:#008300;--k6:#6250d6;
 --ko:#a3a6ad;--kr:#e3e5e9}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e4e6ea;--muted:#9aa1ad;
 --line:#343842;--soft:#20232a;--accent:#e0707f;--k0:#3987e5;--k1:#d95926;--k2:#199e70;
 --k3:#c98500;--k4:#d55181;--k5:#008300;--k6:#9085e9;--ko:#6b6f78;--kr:#363b45}}
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
nav{display:flex;flex-wrap:wrap;gap:.25em .9em;font-size:.92em;color:var(--muted);margin-bottom:1.5em}nav a{white-space:nowrap}
details>summary{display:block;cursor:pointer;list-style:none}details>summary::-webkit-details-marker{display:none}
details>summary>h2::before,details.tb>summary::before{content:"";display:inline-block;width:.42em;height:.42em;
 margin:0 .55em .18em .1em;border:solid var(--muted);border-width:0 2px 2px 0;transform:rotate(-45deg);transition:transform .12s}
details[open]>summary>h2::before,details.tb[open]>summary::before{transform:rotate(45deg)}
details:not([open])>summary>h2{color:var(--muted)}details>summary:hover>h2,details.tb>summary:hover{color:var(--accent)}
details.tb{margin:.7em 0}details.tb>summary{font-weight:600;font-size:.95em}
details.tb>summary small{font-weight:400;color:var(--muted);margin-left:.6em}details.tb>p{margin:.3em 0}
p.tn{font-size:.95em;margin:.7em 0}p.tn b{font-weight:600}
.pt{background:var(--soft);border-radius:6px;padding:.1em 1em;margin:.9em 0}
details.hl{border-left:3px solid var(--accent);background:var(--soft);padding:0 16px 6px;
 border-radius:0 6px 6px 0;margin:1.4em 0}details.hl>summary>h2{border-bottom:0;margin-top:.6em}
.gl{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:1em 0 1.4em}
.gl div{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.gl b{display:block;font-size:1.45em;font-weight:600;line-height:1.2}
.gl span{display:block;font-size:.88em}.gl small{display:block;color:var(--muted);font-size:.8em}
.ob{margin:.6em 0 1.2em;font-size:.88em}.ob ul{list-style:none;padding:0;margin:0 0 8px;display:flex;
 flex-wrap:wrap;gap:4px 14px}.ob li{display:flex;align-items:center;gap:6px}
.ob .r{display:grid;grid-template-columns:minmax(120px,28%) 1fr 72px;gap:10px;align-items:center;
 padding:2px 0}.ob .r code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ob i{width:10px;height:10px;border-radius:50%;flex:none}.ob .bar{display:flex;height:14px;border-radius:3px;overflow:hidden;background:var(--kr)}
.ob .bar s{display:block;height:100%;border-right:1px solid var(--bg)}.ob .bar s:hover{filter:brightness(1.15)}
.ob .n{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.ob.ar .r{grid-template-columns:minmax(120px,24%) 1fr 72px 66px 66px}.ob .r.h{color:var(--muted);font-size:.9em}.ob .r.h span{text-align:right}
.sb i.hx,.ob i.hx{background:repeating-linear-gradient(45deg,var(--muted) 0 2px,transparent 2px 5px)}
.sb{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:12px 32px;margin:1em 0 1.6em}
.sb svg{width:100%;max-width:520px;height:auto;flex:1 1 320px}
.sb path{stroke:var(--bg);stroke-width:1;transition:opacity .1s}.sb.sb-hl path{opacity:.22}
.sb.sb-hl path.on{opacity:1}.sb text{text-anchor:middle;fill:var(--fg)}
.sb .sb-c{font-size:22px;font-weight:600}.sb .sb-l{font-size:13px;fill:var(--muted)}
.sb .k0{fill:var(--k0)}.sb i.k0,.ob .k0{background:var(--k0)}.sb .k1{fill:var(--k1)}.sb i.k1,.ob .k1{background:var(--k1)}.sb .k2{fill:var(--k2)}.sb i.k2,.ob .k2{background:var(--k2)}.sb .k3{fill:var(--k3)}.sb i.k3,.ob .k3{background:var(--k3)}.sb .k4{fill:var(--k4)}.sb i.k4,.ob .k4{background:var(--k4)}.sb .k5{fill:var(--k5)}.sb i.k5,.ob .k5{background:var(--k5)}.sb .k6{fill:var(--k6)}.sb i.k6,.ob .k6{background:var(--k6)}
.sb .ko{fill:var(--ko)}.sb i.ko,.ob .ko{background:var(--ko)}.sb .kr{fill:var(--kr)}.sb i.kr{background:var(--kr)}
.sb ul{list-style:none;margin:0;padding:0;font-size:.9em;min-width:220px}
.sb li{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:4px}
.sb li[data-o]:hover,.sb li[data-o]:focus{background:var(--soft);outline:none}
.sb i{width:10px;height:10px;border-radius:50%;flex:none}
.sb li span:last-child{margin-left:auto;padding-left:16px;color:var(--muted);font-variant-numeric:tabular-nums}
.sb-h{font-weight:600;font-size:.9em;margin-bottom:4px}.sb-note{font-size:.82em;color:var(--muted);max-width:260px}
.sb-tip{display:none;position:absolute;z-index:2;pointer-events:none;max-width:340px;padding:8px 10px;
 font-size:.85em;background:var(--bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.18)}
.sb-tip strong{display:block}.sb-tip code{display:block;margin:2px 0 6px;font-size:.85em;background:none;padding:0;color:var(--muted)}
@media print{nav{display:none}a{color:inherit}.sb-tip{display:none}}
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


# label of a table block with no caption line ("Top-level folders:") above it in report.md
TABLE_TITLES = {
    "areas_top": "Top-level folders", "areas_detail": "Largest folders", "users": "Users",
    "owners": "Owners", "mixed_folders": "Folders with mixed ownership",
    "duplicates": "Likely duplicates", "big_duplicates": "Big duplicates",
    "dir_overlap": "Folders sharing files", "dir_trees": "Identical folder trees",
    "unreadable": "Unreadable directories", "raw_data": "Raw data locations",
    "unknown_types": "Unknown file types", "pipeline_workdirs": "Pipeline work folders",
    "age": "Files by year of last change", "size_histogram": "Files by size",
    "by_category": "Files by category", "types": "File types",
    "sensitive": "Credential-like names", "small_files": "Many small files",
    "touched_old": "Old data, recently touched", "raw_access": "Raw data by area",
    "links": "Links between areas", "shared": "Shared resources",
    "old_areas": "Old and former members' data by area"}


def _fold(label: str, lines: list[str], first: bool) -> tuple[str, bool]:
    """A table block (the table and its "Showing N of M" line) as a collapsible <details>,
    open when `first`. A block without a table ("None found.") stays one line.
    Returns (html, whether it held a table)."""
    rows = sum(ln.lstrip().startswith("|") for ln in lines) - 2
    if rows < 0:
        text = " ".join(x.strip() for x in lines if x.strip())
        return f'<p class="tn"><b>{inline(label)}:</b> {inline(text)}</p>', False
    m = re.search(r"\bShowing ([\d,]+ of [\d,]+ [^.]+)\.", "\n".join(lines))
    count = m.group(1) if m else _plural(rows, "row", "rows")
    return (f'<details class="tb"{" open" if first else ""}><summary>{inline(label)} '
            f'<small>{html.escape(count)}</small></summary>\n'
            f'{to_html(chr(10).join(lines), tables=False)[1]}\n</details>'), True


def to_html(md: str, charts: dict | None = None,
            tables: bool = True) -> tuple[str, str, list[tuple[str, str]]]:
    """`tables`: draw each table as a collapsible block (`_fold`), only the first one
    under each heading open, and the Patterns bullet as a call-out."""
    charts = charts or {}
    lines = md.splitlines()
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    title = "Lab storage audit"
    heading, caption, n_tables = "", None, 0     # tables seen under the current heading
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
            heading, n_tables = re.sub(r"[`*]", "", text), 0
            i += 1
        elif re.match(r"^(-{3,}|\*{3,})\s*$", ln):
            out.append("<hr>")
            i += 1
        elif tables and (m := re.match(r"^<!-- table:([a-z_]+) -->$", ln)):
            j = i + 1
            while j < len(lines) and lines[j] != "<!-- /table -->":
                j += 1
            block, held = _fold(caption or TABLE_TITLES.get(m.group(1), heading),
                                lines[i + 1:j], n_tables == 0)
            out.append(block)
            caption, n_tables = None, n_tables + held
            i = j + 1
        elif ln.lstrip().startswith("|") and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if tables:                              # a hand-written table (deep audits list)
                out.append(_fold(caption or heading, lines[i:j], n_tables == 0)[0])
                caption, n_tables = None, n_tables + 1
            else:
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
            block = _list(lines[i:j])
            out.append(f'<div class="pt">{block}</div>'
                       if tables and ln.startswith("- **Patterns:**") else block)
            i = j
        elif (m := re.match(r"^<!-- chart:([a-z_]+) -->$", ln)) and m.group(1) in charts:
            out.append(charts[m.group(1)])          # the caption inside is for report.md only
            while i < len(lines) and lines[i] != "<!-- /chart -->":
                i += 1
            i += 1
        elif not ln.strip() or re.match(r"^<!--.*-->\s*$", ln):    # blank or table marks
            i += 1
        else:
            j = i + 1
            while j < len(lines) and lines[j].strip() and not re.match(
                    r"^(#{1,6}\s|```|>|\||<!--|(-{3,}|\*{3,})\s*$)", lines[j]) and not _LIST.match(lines[j]):
                j += 1
            text = " ".join(x.strip() for x in lines[i:j])
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if tables and text.endswith(":") and k < len(lines) and (
                    lines[k].startswith("<!-- table:") or lines[k].lstrip().startswith("|")):
                caption = text[:-1]                 # "Top-level folders:" labels the table
            else:
                out.append("<p>" + inline(text) + "</p>")
            i = j
    return title, "\n".join(out), toc


def charts_for(md: str, summary: dict | None) -> dict[str, str]:
    """HTML for every chart region in md that summary.json can draw."""
    out = {}
    for name in re.findall(r"^<!-- chart:([a-z_]+) -->$", md, re.M):
        try:
            if summary is not None and name in CHARTS:
                out[name] = CHARTS[name](summary)
        except KeyError:
            pass
    return out


# sections that start collapsed, by title prefix; any section that is only "None…" does too
COLLAPSED = {"deep": ("Scope and method", "Storage usage", "Pipeline intermediates",
                      "Unreadable and stale directories", "Appendix"),
             "survey": ("Scope and method", "Storage usage", "Unreadable directories", "Appendix")}
HIGHLIGHT = ("Key findings", "Organization observations")    # drawn as call-outs

# opens a section when its nav link is followed, toggles all, and opens all before printing
_SECTIONS_JS = """(function(){var ds=[].slice.call(document.querySelectorAll('main>details')),
x=document.getElementById('xall');function op(h){var t=h&&h.length>1&&document.getElementById(h.slice(1)),
d=t&&t.closest('details');if(d)d.open=true}document.addEventListener('click',function(e){
var a=e.target.closest('a[href^="#"]');if(a&&a!==x)op(a.getAttribute('href'))});op(location.hash);
if(x)x.addEventListener('click',function(e){e.preventDefault();var o=ds.some(function(d){return !d.open});
ds.forEach(function(d){d.open=o});x.textContent=o?'Collapse all':'Expand all'});
window.addEventListener('beforeprint',function(){[].forEach.call(document.querySelectorAll('details'),
function(d){d.open=true})})})();"""


def _sections(body: str, collapse: tuple) -> tuple[str, int]:
    """Wrap each <h2> section of a rendered body in <details>, open unless its title starts
    with one of `collapse` or its whole body is one "None…" paragraph. Returns (html, n closed)."""
    parts = re.split(r'^(?=<h2 id=")', body, flags=re.M)
    out, shut_n = [parts[0]], 0
    for part in parts[1:]:
        head, _, rest = part.partition("\n")
        title = html.unescape(re.sub(r"<[^>]+>", "", head))
        shut = title.startswith(collapse) or bool(re.fullmatch(r"<p>None\b[^<]*</p>", rest.strip()))
        shut_n += shut
        cls = ' class="hl"' if title.startswith(HIGHLIGHT) else ""
        out.append(f"<details{cls}{'' if shut else ' open'}><summary>{head}</summary>\n"
                   f"{rest.rstrip()}\n</details>\n")
    return "".join(out), shut_n


def render(md: str, summary: dict | None = None) -> str:
    title, body, toc = to_html(md, charts_for(md, summary))
    mode = summary.get("mode", "deep") if summary else None
    body, shut = _sections(body, COLLAPSED.get(mode, ()))
    nav = ("<nav>" + "".join(f'<a href="#{h}">{html.escape(t)}</a>' for h, t in toc)
           + ('<a href="#" id="xall">Expand all</a>' if shut else "")
           + "</nav>") if len(toc) > 2 else ""
    script = f"<script>{_SECTIONS_JS}</script>" if toc else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{nav}{body}</main>{script}</body></html>\n")


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


MIXED = 0.8      # a folder is mixed when no file owner holds this share of its bytes


def _owner_shares(r: dict) -> list[tuple[str, int]]:
    """[(owner, bytes)] of a folders row, largest first, 'others' last (v3 summaries)."""
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

    def note(r):
        out = [] if r["in_group"] else ["not in group" if r["user"] else "deleted account"]
        if cut and r["newest"] and r["newest"] < cut:
            out.append("no new files in 2+ years")
        return "; ".join(out)

    def where(r):
        return ", ".join(f"{_code(p)} {_pct(b, r['bytes'])}" for p, b in r.get("top_folders") or [])
    body = [[r["user"] or f"(deleted, uid {r['uid']})", C.human_bytes(r["bytes"]),
             _pct(r["bytes"], tb), _n(r["files"]), where(r), r["newest"] or "", note(r)]
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


def _old_years(s) -> int:
    return s.get("areas", {}).get("old_years") or (3 if s.get("mode") == "survey" else 2)


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

# ------------------------------------------------------------- charts (HTML only)

# A `{{chart:NAME}}` line becomes a one-line caption in report.md and an inline SVG in
# report.html, drawn from summary.json at render time. Owner colours are the first 7 slots
# of the dataviz skill's validated categorical palette (--k0..--k6 in CSS); a sunburst can
# put any two owners side by side, so hover and the legend highlight carry identity too.
N_SLOTS = 7


def _glance(s) -> list[tuple[str, str, str]]:
    """(value, label, detail) for the at-a-glance row; a tile whose data is missing is left out."""
    t, tb = s["totals"], s["totals"]["bytes"] or 1
    out = [(C.human_bytes(t["bytes"]), "scanned", f"{_n(t['files'])} files, {_n(t['dirs'])} folders")]
    dup = (lambda: (C.human_bytes(s["duplicates"]["reclaimable_bytes"]), "likely duplicate copies",
                    f"{_pct(s['duplicates']['reclaimable_bytes'], tb)} of the total"))
    if s.get("mode") == "survey":                  # the lab's data life cycle
        yrs, u, ds = _old_years(s), s.get("users", {}), s.get("dir_status", {})
        tiles = (
            dup,
            lambda: (lambda b: (_pct(b, tb), f"unchanged {yrs}+ years", C.human_bytes(b)))(
                _old_bytes(s, yrs)),
            lambda: (C.human_bytes(u["former_bytes"]), "former members' data",
                     f"{_pct(u['former_bytes'], tb)} of the total"),
            lambda: (f"{_n(u['active'])} / {_n(u['former'])}", "active / former members",
                     f"{_n(u['n'])} file owners in all"),
            lambda: (_n(ds.get("unreadable", 0) + ds.get("sensitive", 0)), "folders not scanned",
                     "unreadable, or skipped as credential stores"),
        )
    else:
      tiles = (
        dup,
        lambda: (C.human_bytes(dict((r["category"], r["bytes"]) for r in _rows(s["by_category"]))
                               .get("raw", 0)), "raw data",
                 f"in {_n(s['raw_locations']['dirs'])} folders"),
        lambda: (_n(s["users"]["n"]), "owner" if s["users"]["n"] == 1 else "owners",
                 _plural(len(_mixed_rows(s)), "folder", "folders") + " with mixed ownership"),
        lambda: (lambda b: (_pct(b, tb), "unchanged 2+ years", C.human_bytes(b)))(
            _old_bytes(s, 2)),
      )
    for tile in tiles:
        try:
            out.append(tile())
        except (KeyError, TypeError, ValueError):
            pass
    return out


# report.md form of each chart: the HTML is drawn into report.html only
CHART_MD = {
    "sunburst": lambda s: ["*Figure: folder sizes by main owner; hatched where no owner holds "
                           f"{round(100 * MIXED)}% (interactive in report.html).*"],
    "owners": lambda s: ["*Figure: owner shares of the largest and the mixed folders "
                         "(in report.html).*"],
    "glance": lambda s: [f"- **{v}** {label} ({d})" for v, label, d in _glance(s)],
    "areas": lambda s: ["*Figure: what each area holds, with its share unchanged "
                        f"{_old_years(s)}+ years and owned by former members (in report.html).*"],
}

# content groups for the areas chart: (label, colour slot, categories; None = the rest)
_CONTENT = (("raw", "k1", ("raw",)), ("processed", "k0", ("processed",)),
            ("reference", "k2", ("reference",)), ("software", "k6", ("code-env", "container")),
            ("other", "ko", None))


def c_areas(s, limit: int = 15) -> str:
    """Content-mix bars: one 100% bar per area, plus size, old and former shares."""
    rows = _rows(s["areas"]["table"])
    if not rows:
        raise KeyError("areas rows")
    yrs = _old_years(s)
    esc = lambda x: html.escape(str(x), quote=True)  # noqa: E731
    named = {c for _, _, cats in _CONTENT if cats for c in cats}

    def groups(a):
        cats = dict(a["by_category"])
        return [(label, k, sum(cats.get(c, 0) for c in cs) if cs else
                 sum(b for c, b in cats.items() if c not in named)) for label, k, cs in _CONTENT]
    pick = rows[:limit]
    rest = rows[limit:]
    if rest:
        tot = {"path": f"other ({len(rest)} areas)", "bytes": sum(a["bytes"] for a in rest),
               "old_bytes": sum(a["old_bytes"] for a in rest),
               "former_bytes": sum(a["former_bytes"] for a in rest), "by_category": []}
        acc: dict = defaultdict(int)
        for a in rest:
            for c, b in a["by_category"]:
                acc[c] += b
        tot["by_category"] = list(acc.items())
        pick = pick + [tot]
    out = ['<figure class="ob ar"><ul>']
    out += [f'<li><i class="{k}"></i>{label}</li>' for label, k, _ in _CONTENT]
    out.append(f'</ul><div class="r h"><span></span><span></span><span>size</span>'
               f'<span>{yrs}+ yrs</span><span>former</span></div>')
    for a in pick:
        gs = groups(a)
        tot = sum(b for _, _, b in gs) or 1
        segs = "".join(f'<s class="{k}" style="width:{100 * b / tot:.2f}%" title="{label}: '
                       f'{esc(C.human_bytes(b))} ({esc(_pct(b, tot))})"></s>'
                       for label, k, b in gs if b)
        out.append(f'<div class="r"><code title="{esc(a["path"])}">{esc(a["path"])}</code>'
                   f'<div class="bar">{segs}</div><span class="n">{esc(C.human_bytes(a["bytes"]))}'
                   f'</span><span class="n">{esc(_pct(a["old_bytes"], a["bytes"]))}</span>'
                   f'<span class="n">{esc(_pct(a["former_bytes"], a["bytes"]) if a["former_bytes"] else "")}'
                   "</span></div>")
    return "".join(out) + "</figure>"


def c_glance(s) -> str:
    esc = html.escape
    return '<div class="gl">' + "".join(
        f"<div><b>{esc(v)}</b><span>{esc(label)}</span><small>{esc(d)}</small></div>"
        for v, label, d in _glance(s)) + "</div>"


def _owner_slots(s, shown: set) -> tuple[list, dict]:
    """(owners shown, in users-table order; owner -> colour slot). Slots follow the whole
    users table (bytes owned), so an owner has the same colour in every chart."""
    every = [u["user"] or f"uid:{u['uid']}" for u in _rows(s["users"]["table"])] \
        if "users" in s else []
    every += sorted(u for u in shown - set(every) if u)
    slot = {u: i for i, u in enumerate(every[:N_SLOTS])}
    return [u for u in every if u in shown], slot


def c_owners(s, limit: int = 16) -> str:
    """100% stacked bars of owner shares: top-level folders, then mixed folders."""
    rows = _rows(s["folders"])
    top = sorted((r for r in rows if r["level"] == 1), key=lambda r: (-r["bytes"], r["path"]))
    pick = top[:limit // 2] + [r for r in _mixed_rows(s) if r["level"] != 1]
    pick = pick[:limit]
    if not pick:
        raise KeyError("folders below the top level")
    order, slot = _owner_slots(s, {u for r in pick for u, _ in _owner_shares(r) if u != "others"})
    esc = lambda x: html.escape(str(x), quote=True)  # noqa: E731
    out = ['<figure class="ob"><ul>']
    out += [f'<li><i class="k{slot[u]}"></i>{esc(u)}</li>' for u in order if u in slot]
    if any(u not in slot for u in order) or any(u == "others" for r in pick for u, _ in _owner_shares(r)):
        out.append('<li><i class="ko"></i>other owners</li>')
    out.append("</ul>")
    for name, r in zip(_paths(r["path"] for r in pick), pick):
        own = _owner_shares(r)
        tot = sum(b for _, b in own) or 1
        segs = "".join(f'<s class="{f"k{slot[u]}" if u in slot else "ko"}" style="width:'
                       f'{100 * b / tot:.2f}%" title="{esc(u)}: {esc(C.human_bytes(b))} '
                       f'({esc(_pct(b, tot))})"></s>' for u, b in own if b)
        out.append(f'<div class="r"><code title="{esc(r["path"])}">{esc(name.strip("`"))}</code>'
                   f'<div class="bar">{segs}</div><span class="n">{esc(C.human_bytes(r["bytes"]))}'
                   "</span></div>")
    return "".join(out) + "</figure>"

_SB_JS = """(function(){var f=document.currentScript.previousElementSibling,
t=f.querySelector('.sb-tip'),g=[].slice.call(f.querySelectorAll('path[data-p]'));
g.forEach(function(s){var x=s.querySelector('title');if(x)s.removeChild(x)});
function mark(k){f.classList.toggle('sb-hl',!!k);g.forEach(function(s){s.classList.toggle('on',!!k&&k(s))})}
function rel(p,q){return p===q||q.indexOf(p+'/')===0||p.indexOf(q+'/')===0}
function row(v,l){var d=document.createElement('div'),b=document.createElement('b');
b.textContent=v;d.appendChild(b);d.appendChild(document.createTextNode(' '+l));t.appendChild(d)}
function show(s,e){var d=s.dataset;t.textContent='';var h=document.createElement('strong');
h.textContent=d.n;t.appendChild(h);var c=document.createElement('code');c.textContent=d.p;t.appendChild(c);
row(d.b,d.k?'not itemised: files directly in the folder, or folders too small to list':'('+d.pc+' of total)');
if(!d.k){row(d.f,'files, '+d.d+' folders');row(d.o||'unknown','owns the most bytes');
if(d.os)row(d.os,'file owners');if(d.dw)row(d.dw,'owns the folder itself');
if(d.nw)row(d.nw,'newest file'+(d.la?', last activity '+d.la:''));if(d.st)row(d.st,'status')}
t.style.display='block';var r=f.getBoundingClientRect(),x=e.clientX-r.left+14,y=e.clientY-r.top+14;
if(x+t.offsetWidth>r.width)x=Math.max(0,x-t.offsetWidth-28);t.style.left=x+'px';t.style.top=y+'px'}
function hide(){t.style.display='none';mark(null)}
var v=f.querySelector('svg');v.addEventListener('pointermove',function(e){
var s=e.target.closest('path[data-p]');if(!s)return hide();show(s,e);
mark(function(q){return rel(s.dataset.p,q.dataset.p)})});v.addEventListener('pointerleave',hide);
[].slice.call(f.querySelectorAll('li[data-o]')).forEach(function(l){var o=l.dataset.o;
function on(){mark(function(q){return q.dataset.o===o&&!q.dataset.k})}
l.addEventListener('pointerenter',on);l.addEventListener('focus',on);
l.addEventListener('pointerleave',hide);l.addEventListener('blur',hide)})})();"""


def _arc(r0: float, r1: float, a0: float, a1: float) -> str:
    """SVG path of an annular sector; angles in radians, clockwise from 12 o'clock."""
    def p(r, a):
        return f"{r * math.sin(a):.1f},{-r * math.cos(a):.1f}"
    if a1 - a0 >= 2 * math.pi - 1e-6:            # whole ring: two half circles, even-odd hole
        return (f"M{p(r1, 0)}A{r1:.1f},{r1:.1f} 0 1 1 {p(r1, math.pi)}A{r1:.1f},{r1:.1f} 0 1 1 "
                f"{p(r1, 0)}M{p(r0, 0)}A{r0:.1f},{r0:.1f} 0 1 0 {p(r0, math.pi)}"
                f"A{r0:.1f},{r0:.1f} 0 1 0 {p(r0, 0)}Z")
    big = int(a1 - a0 > math.pi)
    return (f"M{p(r1, a0)}A{r1:.1f},{r1:.1f} 0 {big} 1 {p(r1, a1)}L{p(r0, a1)}"
            f"A{r0:.1f},{r0:.1f} 0 {big} 0 {p(r0, a0)}Z")


def c_sunburst(s, size: int = 600, min_angle: float = 0.002) -> str:
    """Sunburst of the `folders` rows: rings go one level deeper from the centre, angle is
    bytes, colour is the folder's top owner. Grey arcs are a folder's bytes not in a listed
    subfolder. Untrusted names are escaped into attributes; the script reads them as text."""
    rows = _rows(s["folders"])
    if not rows:
        raise KeyError("folders rows")
    tb = s["totals"]["bytes"] or 1
    by = {r["path"]: r for r in rows}

    def up(p):
        while p != ".":
            p = _parent(p)
            if p in by:
                return p
        return None
    kids: dict = {}
    for r in rows:
        kids.setdefault(up(r["path"]), []).append(r)
    tops = kids.get(None, [])
    if len(tops) == 1:                               # one root: it's the centre
        centre, lv0 = tops[0], tops[0]["level"]
    else:                                            # several roots: a virtual centre
        centre, lv0 = {"path": None, "bytes": tb}, -1
        kids[None] = tops

    segs = []                                        # (ring, a0, a1, row or rest dict)

    def lay(node, a0, a1):
        ks = sorted(kids.get(node["path"], []), key=lambda r: (-r["bytes"], r["path"]))
        nb = max(node["bytes"], sum(k["bytes"] for k in ks), 1)
        a = a0
        for k in ks:
            span = (a1 - a0) * k["bytes"] / nb
            if span >= min_angle:
                segs.append((k["level"] - lv0, a, a + span, k))
                lay(k, a, a + span)
            a += span
        if ks and a1 - a >= min_angle:
            rest = node["bytes"] - sum(k["bytes"] for k in ks)
            segs.append((ks[0]["level"] - lv0, a, a1,
                         {"path": f"{node['path'] or '.'}/…", "bytes": rest, "rest": True}))
    lay(centre, 0.0, 2 * math.pi)
    if not segs:
        raise KeyError("folders below the top level")

    # owner -> colour slot, by bytes owned in the whole target (users table order)
    shown = {r.get("top_owner") for _, _, _, r in segs if not r.get("rest")}
    order, slot = _owner_slots(s, shown)
    owned = {(u["user"] or f"uid:{u['uid']}"): u["bytes"]
             for u in _rows(s["users"]["table"])} if "users" in s else {}

    R = size / 2 - 4
    rings = max(g[0] for g in segs)
    hole = R * (0.3 if rings > 2 else 0.36)
    w = (R - hole) / rings
    esc = lambda x: html.escape(str(x), quote=True)  # noqa: E731
    out = [f'<svg viewBox="{-size / 2} {-size / 2} {size} {size}" role="img" '
           f'aria-label="Sunburst chart of folder sizes, coloured by the user who owns the most '
           f'bytes in each folder, hatched where ownership is mixed. The folder tables list '
           f'the same folders.">']
    hatched = sorted({f"k{slot[r['top_owner']]}" if r.get("top_owner") in slot else "ko"
                      for _, _, _, r in segs if not r.get("rest") and _is_mixed(r)})
    if hatched:                                      # one pattern per colour: fill + light stripes
        out.append("<defs>" + "".join(
            f'<pattern id="sbh-{c}" patternUnits="userSpaceOnUse" width="7" height="7" '
            f'patternTransform="rotate(45)"><rect width="7" height="7" style="fill:var(--{c})"/>'
            f'<rect width="2.5" height="7" style="fill:var(--bg);opacity:.55"/></pattern>'
            for c in hatched) + "</defs>")
    for ring, a0, a1, r in segs:
        name = r["path"].rsplit("/", 1)[-1] if r["path"] != "." else r["path"]
        d = _arc(hole + (ring - 1) * w, hole + ring * w, a0, a1)
        attrs = {"p": r["path"], "n": name, "b": C.human_bytes(r["bytes"])}
        if r.get("rest"):
            cls, attrs["k"] = "kr", "rest"
            attrs["n"] = "not itemised"
            tip = f"{r['path']}: {attrs['b']} not itemised"
        else:
            o = r.get("top_owner")
            cls = f"k{slot[o]}" if o in slot else "ko"
            attrs |= {"pc": _pct(r["bytes"], tb), "f": _n(r["files"]), "d": _n(r["dirs"]),
                      "o": o or "", "nw": r.get("newest") or "",
                      "la": r.get("last_activity") or "",
                      "st": "" if r.get("status", "ok") == "ok" else r["status"],
                      "os": _shares_text(r) if len(_owner_shares(r)) > 1 else "",
                      "dw": r["dir_owner"] if _dir_owner_differs(r) else ""}
            tip = f"{r['path']}: {attrs['b']}, {_pct(r['bytes'], tb)}, owner {o or 'unknown'}"
        data = "".join(f' data-{k}="{esc(v)}"' for k, v in attrs.items() if v != "")
        fill = f' style="fill:url(#sbh-{cls})"' if not r.get("rest") and _is_mixed(r) else ""
        out.append(f'<path class="{cls}"{fill} d="{d}"{data}><title>{esc(tip)}</title></path>')
    label = centre["path"] or os.path.basename(s.get("run", {}).get("target", "")) or "target"
    label = os.path.basename(s.get("run", {}).get("target", "")) if label == "." else label
    out.append(f'<text class="sb-c" y="-4">{C.human_bytes(centre["bytes"])}</text>'
               f'<text class="sb-l" y="16">{esc(_clip(label, 24))}</text></svg>')

    items = [(f"k{slot[u]}", u, C.human_bytes(owned[u]) if u in owned else "")
             for u in order if u in slot]
    if any(u not in slot for u in order) or None in shown:
        items.append(("ko", "other users", ""))
    if hatched:
        items.append(("hx", f"no owner holds {round(100 * MIXED)}%", ""))
    items.append(("kr", "not itemised", ""))
    legend = "".join(
        (f'<li data-o="{esc(u)}" tabindex="0">' if c.startswith("k") and c[1:].isdigit()
         else "<li>") + f'<i class="{c}"></i><span>{esc(u)}</span><span>{esc(b)}</span></li>'
        for c, u, b in items)
    return (f'<figure class="sb">{"".join(out)}<div><div class="sb-h">Main owner of each folder'
            f'</div><ul>{legend}</ul><p class="sb-note">Size is all files the user owns here. '
            "Each ring is one folder level deeper; hover a segment or a name.</p></div>"
            f'<div class="sb-tip"></div></figure><script>{_SB_JS}</script>')


CHARTS = {name[2:]: fn for name, fn in globals().items() if name.startswith("c_")}

# the blocks each template asks for; a report missing one gets a warning
REQUIRED = {
    "survey": ("chart:glance", "chart:sunburst", "facts:raw_access", "raw_access", "links",
               "facts:shared", "shared", "facts:old", "old_areas", "age", "facts:types",
               "types", "raw_data", "unknown_types", "sensitive", "chart:areas", "facts:areas",
               "areas_top", "areas_detail", "small_files", "facts:pipeline",
               "pipeline_workdirs", "touched_old", "users", "facts:duplicates", "duplicates",
               "facts:scope", "facts:storage", "facts:unreadable", "unreadable",
               "size_histogram", "by_category"),
    "deep": ("chart:glance", "chart:sunburst", "chart:owners", "owners", "mixed_folders",
             "areas_top", "areas_detail", "small_files", "touched_old", "raw_data",
             "facts:duplicates", "duplicates", "dir_trees", "facts:scope", "facts:storage",
             "facts:unreadable", "unreadable", "facts:age", "age", "facts:pipeline",
             "pipeline_workdirs", "by_category", "unknown_types"),
}
_MARK = re.compile(r"^\{\{(table|facts|chart):([a-z_]+)\}\}\s*$")
_REGION = re.compile(r"^<!-- (table|facts|chart):([a-z_]+) -->$")
_KINDS = {"table": TABLES, "facts": FACTS, "chart": CHARTS}


def expand(md: str, summary: dict) -> tuple[str, list[str]]:
    """Fill every {{table|facts|chart:NAME}} marker and regenerate every wrapped block.
    Returns (new markdown, blocks used: table names bare, others as 'facts:NAME'/'chart:NAME').
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
            if kind == "chart":                     # the HTML is drawn into report.html only
                block = CHART_MD[name](summary)
        except KeyError as ex:                      # section absent from this summary
            block = [f"Not available: `summary.json` has no {ex}."]
        out += [f"<!-- {kind}:{name} -->", *block, f"<!-- /{kind} -->"]
        used.append(name if kind == "table" else f"{kind}:{name}")
        i += 1
    return "\n".join(out) + "\n", used


# words of the agent's own prose per section (tables and generated blocks don't count)
BUDGETS = {"survey": {"Key findings": 160, "Data life cycle": 270, "Data types": 90,
                      "Areas": 90, "Users": 60, "Big duplicates": 90,
                      "Recommended deep audits": 60, "Flagged content": 40,
                      "Scope and method": 20, "Storage usage": 60,
                      "Unreadable directories": 60, "Appendix": 30},
           "deep": {"Key findings": 160, "Organization observations": 200, "Ownership": 90,
                    "Per-folder overview": 90, "Raw data": 90, "Duplicates": 90,
                    "Flagged content": 40, "Scope and method": 20, "Storage usage": 60,
                    "Unreadable and stale": 60, "Pipeline intermediates": 40, "Appendix": 120}}
_FREE = ("Key findings", "Recommended deep audits", "Questions for the PI",   # may repeat numbers
         "Organization observations")
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
            elif re.match(r"^<!-- /(table|facts|chart) -->$", ln):
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
    summary = None
    if os.path.exists(sj):
        with open(sj, encoding="utf-8") as fh:
            summary = json.load(fh)
        md, used = expand(md, summary)
        C.log(f"filled: {', '.join(used) or 'none'}")
        mode = summary.get("mode", "deep")
        missing = [n for n in REQUIRED.get(mode, ()) if n not in used]
        if missing:
            C.log(f"WARNING: report.md has no {{{{table|facts|chart:...}}}} line for: "
                  f"{', '.join(missing)}")
        for w in lint(md, mode):
            C.log(f"WARNING: {w}")
        with open(src + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(md)
        os.replace(src + ".tmp", src)
    page = render(md, summary)
    dst = C.out_path(args.run, "report.html")
    with open(dst + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(dst + ".tmp", dst)
    C.log(f"wrote {dst} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
