"""Charts for report.html, drawn from summary.json (used by render.py; stdlib only).

A `{{chart:NAME}}` line becomes a one-line caption in report.md (CHART_MD) and an
inline SVG or HTML figure in report.html (CHARTS[NAME]); hover uses a small inline
script, and untrusted names are escaped into attributes and read back as text.
"""
from __future__ import annotations

import html
import math
import os
from collections import defaultdict

import common as C
from report_tables import (_clip, _dir_owner_differs, _is_mixed, MIXED, _mixed_rows, _n,
                           _old_bytes, _old_years, _owner_shares, _parent, _paths, _pct,
                           _plural, _rows, _shares_text)


# Owner colours are the first 7 slots of the dataviz skill's validated categorical palette
# (--k0..--k6 in mdhtml.CSS); a sunburst can put any two owners side by side, so hover and
# the legend highlight carry identity too.
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
