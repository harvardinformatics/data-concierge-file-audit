"""report.md -> a self-contained HTML page (used by render.py; stdlib only).

Supports the Markdown subset the report template uses: headings, paragraphs,
nested lists, GFM pipe tables, fenced code, blockquotes, rules, `code`,
**bold**, *italic* and [links](https://…). Underscores are never emphasis,
because lab paths are full of them. Everything is HTML-escaped first (paths come
from the target and are untrusted); links are kept only for http(s)/mailto
and relative URLs. Inline CSS and script, light/dark theme, no external resources.

Each `##` section and each table is collapsible: a table is labelled by the caption
line above it ("Top-level folders:") or TABLE_TITLES, and only the first table under
each heading starts open. The Patterns bullet is drawn as a call-out.
"""
from __future__ import annotations

import html
import re


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
    """The escaped URL if it is http(s), mailto or relative; else None. Browsers drop control
    characters and spaces before reading a scheme ("\\x01javascript:" runs as javascript:),
    so a URL containing any is refused rather than taken as relative."""
    u = html.unescape(u)
    if re.search(r"[\x00-\x20\x7f]", u):
        return None
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
    "duplicates": "Likely duplicates", "dir_trees": "Identical folder trees",
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
    count = m.group(1) if m else f"{rows:,} {'row' if rows == 1 else 'rows'}"
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


def page(md: str, charts: dict | None = None, collapse: tuple = ()) -> str:
    """report.md as a self-contained HTML page. `charts` maps a chart region's name
    to its HTML; `##` sections whose title starts with one of `collapse` start closed."""
    title, body, toc = to_html(md, charts)
    body, shut = _sections(body, collapse)
    nav = ("<nav>" + "".join(f'<a href="#{h}">{html.escape(t)}</a>' for h, t in toc)
           + ('<a href="#" id="xall">Expand all</a>' if shut else "")
           + "</nav>") if len(toc) > 2 else ""
    script = f"<script>{_SECTIONS_JS}</script>" if toc else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{nav}{body}</main>{script}</body></html>\n")
