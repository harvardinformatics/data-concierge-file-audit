#!/usr/bin/env python3
"""Render <run>/report.md into a self-contained <run>/report.html (stdlib only).

The page itself (Markdown subset, escaping, CSS, collapsible sections) is mdhtml.py;
the blocks are report_tables.py, report_facts.py and report_charts.py.

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

After filling, the report is linted in both modes (warnings only): prose over a section's
word budget, numbers that repeat a table or facts block in the same section, and
hand-written row counts ("showing N of M"; the tables print their own).

  python3 render.py --run audits/<run>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import mdhtml as M  # noqa: E402
import summary as S  # noqa: E402
from report_charts import CHART_MD, CHARTS  # noqa: E402
from report_facts import FACTS  # noqa: E402
from report_tables import TABLES  # noqa: E402


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


def render(md: str, summary: dict | None = None) -> str:
    mode = summary.get("mode", "deep") if summary else None
    return M.page(md, charts_for(md, summary), COLLAPSED.get(mode, ()))


_MARK = re.compile(r"^\{\{(table|facts|chart):([a-z_]+)\}\}\s*$")
_REGION = re.compile(r"^<!-- (table|facts|chart):([a-z_]+) -->$")
_KINDS = {"table": TABLES, "facts": FACTS, "chart": CHARTS}
TEMPLATES = {"survey": "survey-template.md", "deep": "report-template.md"}


def required(mode: str) -> list[str]:
    """The blocks the mode's template asks for, named as expand() reports them; a report
    missing one gets a warning."""
    if mode not in TEMPLATES:
        return []
    with open(C.SCRIPTS_DIR.parent / "references" / TEMPLATES[mode], encoding="utf-8") as fh:
        marks = re.findall(_MARK.pattern, fh.read(), re.M)
    return [n if k == "table" else f"{k}:{n}" for k, n in marks]


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
_FREE = ("Key findings", "Recommended deep audits", "Organization observations")  # may repeat numbers
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
        if summary.get("schema_version") != S.SUMMARY_VERSION:
            C.die(f"summary.json is schema {summary.get('schema_version')}, render.py needs "
                  f"{S.SUMMARY_VERSION}: re-run `post.py --run {args.run} --stages summarize`")
        md, used = expand(md, summary)
        C.log(f"filled: {', '.join(used) or 'none'}")
        mode = summary.get("mode", "deep")
        missing = [n for n in required(mode) if n not in used]
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
