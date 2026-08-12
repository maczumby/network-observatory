#!/usr/bin/env python3
"""
common.py — the shared spine of the three Observatory screens.

Every exporter (map, warmth, workbench) renders through here so the three
pages stay one product: one design-token system and font set (inlined at
build time — pages make zero network requests), one nav, one HTML/JSON
escaper, one read over the trusted `people_v` view, and one portable person
identity (both the DB integer id and the content key the map has always used
for localStorage, so nothing a user saved is orphaned).

Standard library only.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

import trellis  # noqa: E402

_ASSET_CACHE = {}

FONTS_MARKER = "/*__OBS_FONTS__*/"
TOKENS_MARKER = "/*__OBS_TOKENS__*/"
NAV_MARKER = "<!--__OBS_NAV__-->"

SCREENS = (("map", "observatory.html", "Map"),
           ("warmth", "warmth.html", "Warmth"),
           ("workbench", "workbench.html", "People"))


def esc(s):
    """The one HTML escaper (Python side)."""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def json_for_script(payload):
    """Compact JSON safe to embed inside a <script> block."""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (blob.replace("</", "<\\/")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))


def read_asset(name):
    if name not in _ASSET_CACHE:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            _ASSET_CACHE[name] = f.read()
    return _ASSET_CACHE[name]


def person_key(url, name, company):
    """The map's stable content identity, replicated exactly (template.html
    build(): url wins; else name|company with the map's company fallback).
    This is what localStorage flags/notes are keyed by — never change it."""
    if url and url.strip():
        u = url.strip().lower()
        while u.endswith("/"):
            u = u[:-1]
        return "u:" + u
    comp = company.strip() if company and company.strip() else "Independent / unknown"
    return "n:" + (name or "").lower() + "|" + comp.lower()


def status_of(p):
    """The single ★/◷ derivation every screen shares."""
    return {
        "star": (p.get("priority") or "normal") in ("important", "critical"),
        "due": bool(p.get("follow_up_on")
                    and p["follow_up_on"] <= trellis.TODAY.isoformat()),
    }


def nav_html(active, variant="bar"):
    """The instrument rail. active in {'map','warmth','workbench'};
    variant 'overlay' floats it over the map's sky."""
    cls = "obs-nav obs-nav--overlay" if variant == "overlay" else "obs-nav"
    links = "".join(
        '<a href="{href}"{cur}>{label}</a>'.format(
            href=href, label=label,
            cur=' aria-current="page"' if key == active else "")
        for key, href, label in SCREENS)
    return (
        '<nav class="{cls}" data-obs-nav>'
        '<span class="obs-nav-dot" aria-hidden="true"></span>'
        '<span class="obs-nav-brand">OBSERVATORY</span>'
        '{links}'
        '<span class="obs-mode" id="obs-mode" data-mode="static" hidden></span>'
        '</nav>'
        '<script>{mode}</script>'
    ).format(cls=cls, links=links, mode=_MODE_SNIPPET)


# How a page learns what it can do. file:// -> static (paste-block flows).
# Served -> ask /api/status: rw true -> live writes; anything else (older
# serve.py, no --rw, error) -> served read-only, same as static.
_MODE_SNIPPET = """
(function(){
  var el=document.getElementById('obs-mode');
  function set(mode,label){
    document.documentElement.setAttribute('data-obs-mode',mode);
    if(!el) return;
    el.setAttribute('data-mode',mode);
    if(label){ el.textContent=label; el.hidden=false; }
  }
  if(location.protocol==='file:'){ set('static',''); return; }
  var ctl=('AbortController' in window)?new AbortController():null;
  var t=setTimeout(function(){ if(ctl) ctl.abort(); },1500);
  fetch('/api/status',{credentials:'same-origin',signal:ctl&&ctl.signal})
    .then(function(r){ return r.ok?r.json():null; })
    .then(function(j){
      clearTimeout(t);
      if(j&&j.rw){ set('live','live'); } else { set('served-ro','read-only'); }
    })
    .catch(function(){ clearTimeout(t); set('served-ro','read-only'); });
})();
""".strip()


def inline_assets(template, active, nav_variant="bar"):
    """Fill the three build-time markers. Loud when a marker is missing —
    a silently token-less page would look broken in ways QA might miss."""
    out = template
    for marker, value in ((FONTS_MARKER, read_asset("fonts.css")),
                          (TOKENS_MARKER, read_asset("tokens.css")),
                          (NAV_MARKER, nav_html(active, nav_variant))):
        if marker not in out:
            raise SystemExit(f"Template is missing the {marker} marker.")
        out = out.replace(marker, value, 1)
    return out


def render_page(template_path, data_token, payload, out_path, active,
                nav_variant="bar"):
    """template + payload -> a finished self-contained page. Returns bytes written."""
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    if data_token not in template:
        raise SystemExit(f"Template is missing the {data_token} token: {template_path}")
    html = inline_assets(template, active, nav_variant)
    html = html.replace(data_token, json_for_script(payload), 1)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        written = f.write(html)
    return written


def load_people(conn):
    """Everyone the product trusts (people_v), with warmth + status attached.
    Each dict carries BOTH identities: `id` (DB integer — deep links, write
    API) and `key` (content key — the map's localStorage identity)."""
    today = trellis.TODAY.isoformat()
    people = []
    for r in conn.execute("""
        SELECT p.*, agg.last_on, agg.n,
               (SELECT content FROM notes nt WHERE nt.connection_id = p.id
                ORDER BY nt.created_at DESC LIMIT 1) AS note
        FROM people_v p
        LEFT JOIN (SELECT connection_id, MAX(occurred_on) AS last_on, COUNT(*) AS n
                   FROM interactions GROUP BY connection_id) agg
               ON agg.connection_id = p.id
        ORDER BY p.connected_year DESC, p.id DESC"""):
        ds = trellis.days_since(r["last_on"])
        people.append({
            "id": r["id"],
            "key": person_key(r["url"], r["full_name"], r["company"]),
            "name": r["full_name"] or "(unnamed)",
            "first": r["first_name"] or "", "last": r["last_name"] or "",
            "company": r["company"] or "", "title": r["title"] or "",
            "func": r["func"] or "Other",
            "is_founder": int(r["is_founder"] or 0),
            "rank": r["rank"] or 2,
            "year": r["connected_year"], "month": r["connected_month"],
            "url": r["url"] or "", "email": r["email"] or "",
            "origin": r["source"] or "manual",
            "priority": r["priority"] or "normal",
            "flag": 1 if (r["priority"] or "normal") in ("important", "critical") else 0,
            "note": r["note"] or "",
            "follow_up_on": r["follow_up_on"],
            "follow_up_due": bool(r["follow_up_on"] and r["follow_up_on"] <= today),
            "follow_up_reason": r["follow_up_reason"],
            "last_on": r["last_on"], "days": ds, "n": r["n"] or 0,
            "bucket": trellis.warmth_bucket(ds),
        })
    return people


def hidden_counts(conn):
    """What the trust filter is holding back, by reason — so a coverage line can
    say it out loud instead of quietly shrinking the denominator. Reads the same
    view the screens do, so the numbers can't disagree with what's on screen."""
    counts = {r["hidden_reason"]: r["n"] for r in conn.execute("""
        SELECT hidden_reason, COUNT(*) AS n FROM people_all_v
        WHERE hidden_reason IS NOT NULL GROUP BY hidden_reason""")}
    return {"muted": counts.get("muted", 0),
            "no_signal": counts.get("no signal yet", 0)}
