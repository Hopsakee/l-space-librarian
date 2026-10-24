# /// script
# requires-python = ">=3.12"
# dependencies = ["python-fasthtml==0.14.12"]
# ///
"""Consumelijst: the consume list as a small web page for phone, Boox and Mac.

Reads and writes `consume_items` in l-space.db (see consume_selection/consume_list.py).
No accounts: it listens on the Mac Mini's Tailscale address only, so the tailnet
is the login (decided 2026-10-23). Writes are plain form POSTs, refused unless
they come from this page itself (see `same_origin`).

Plain HTML and a few lines of inline JS, no CDN: the Boox's e-ink browser gets a
fast, high-contrast page, and the page works with no internet beyond the tailnet.

    CONSUME_DB=~/data/l-space-librarian/l-space.db uv run consume_web/app.py
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fasthtml.common import (A, Article, Beforeware, Button, Details, Div, FastHTML, FileResponse,
                             Form, H1, H2, Header, Input, JSONResponse, Link, Main, Meta, Nav, Option,
                             P, RedirectResponse, Response, Script, Select, Small, Span, Style,
                             Summary, Title, serve)

from consume_selection import consume_list as cl
from consume_selection.db import connect, db_path, init_schema

MEDIA_LABEL = {"read": "Lezen", "watch": "Kijken", "listen": "Luisteren"}
STATUS_LABEL = {"todo": "Te doen", "doing": "Bezig", "done": "Klaar"}
SHOW_LABEL = {"open": "Open", "done": "Klaar", "dropped": "Gevallen"}

CSS = """
:root { --fg:#000; --bg:#fff; --line:#000; --soft:#444; --on-fg:#fff; }
@media (prefers-color-scheme: dark) { :root { --fg:#eee; --bg:#111; --line:#bbb; --soft:#bbb; --on-fg:#111; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:18px/1.4 system-ui, sans-serif; }
main { max-width: 46rem; margin: 0 auto; padding: 0 16px 4rem; }
header h1 { font-size: 1.4rem; margin: 1rem 0 .25rem; }
nav { display:flex; flex-wrap:wrap; gap:.4rem; margin:.6rem 0; }
nav a, button, .btn { display:inline-flex; align-items:center; min-height:44px; padding:0 .8rem;
  border:2px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg);
  text-decoration:none; font:inherit; font-size:1rem; cursor:pointer; }
nav a.on, button.on { background:var(--fg); color:var(--on-fg); font-weight:700; }
.wip { color:var(--soft); margin:0; }
.wip.over { color:var(--fg); font-weight:700; }
article { border:2px solid var(--line); border-radius:8px; padding:.75rem; margin:.9rem 0; }
article h2 { font-size:1.1rem; margin:0 0 .3rem; overflow-wrap:anywhere; }
.meta, .landing { color:var(--soft); margin:.2rem 0; font-size:.95rem; }
.row { display:flex; flex-wrap:wrap; gap:.4rem; margin:.5rem 0; align-items:center; }
.row form { display:contents; }
.copy { display:flex; gap:.4rem; margin:.5rem 0; }
.copy input { flex:1; min-width:0; min-height:44px; font:inherit; font-size:.9rem; padding:0 .5rem;
  border:2px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
details { margin-top:.4rem; }
summary { cursor:pointer; min-height:44px; display:flex; align-items:center; font-size:1rem; text-decoration:underline; }
details form { display:grid; gap:.5rem; margin-top:.4rem; }
select, textarea { min-height:44px; font:inherit; border:2px solid var(--line); border-radius:6px;
  background:var(--bg); color:var(--fg); padding:0 .5rem; }
h2.section { font-size:1.1rem; margin:2rem 0 .2rem; }
.empty { color:var(--soft); }
"""

# Copy without the Clipboard API: that API needs HTTPS or localhost, and the
# page is also opened over plain HTTP when developing. execCommand works on both.
JS = """
function cp(b){const i=b.previousElementSibling;i.focus();i.select();i.setSelectionRange(0,9999);
let ok=false;try{ok=document.execCommand('copy')}catch(e){}
b.textContent=ok?'Gekopieerd':'Selecteer en kopieer';}
"""


def same_origin(req, sess=None):
    """Refuse a POST that another web page made the browser send. There are no
    logins here, so without this any site open on the phone could post a form
    to this address and change the list."""
    if req.method != "POST":
        return None
    # Behind `tailscale serve` the browser's host may arrive as X-Forwarded-Host.
    # Only the proxy can set it: the container listens on 127.0.0.1, and a form
    # posted by another site cannot add headers.
    host = req.headers.get("x-forwarded-host") or req.headers.get("host", "")
    src = req.headers.get("origin") or req.headers.get("referer") or ""
    if not host or urlsplit(src).netloc != host:
        return Response("Geweigerd: verzoek kwam niet van deze pagina.", status_code=403)
    return None


app = FastHTML(default_hdrs=False, canonical=False, secret_key=secrets.token_hex(16),
               before=Beforeware(same_origin),
               hdrs=(Meta(name="viewport", content="width=device-width, initial-scale=1"),
                     # Home-screen app: own icon, opens without the browser bar.
                     Link(rel="manifest", href="/manifest.webmanifest"),
                     Link(rel="apple-touch-icon", href="/icon-180.png"),
                     Meta(name="apple-mobile-web-app-capable", content="yes"),
                     Meta(name="mobile-web-app-capable", content="yes"),
                     Meta(name="apple-mobile-web-app-title", content="Consume"),
                     Meta(name="theme-color", content="#ffffff"),
                     Style(CSS), Script(JS)))

ICONS = Path(__file__).resolve().parent / "icons"
MANIFEST = {
    "name": "Consumelijst", "short_name": "Consume", "lang": "nl",
    "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#ffffff", "theme_color": "#ffffff",
    "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"} for n in (180, 512)],
}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/icon-{size}.png")
def icon(size: int):
    if size not in (180, 512):
        return Response(status_code=404)
    return FileResponse(ICONS / f"icon-{size}.png", media_type="image/png")


def db() -> sqlite3.Connection:
    return connect()


# --- links -----------------------------------------------------------------

def _label(url: str) -> str:
    host = urlsplit(url).netloc.removeprefix("www.")
    if "youtube" in host or host == "youtu.be":
        return "YouTube"
    if host == "read.readwise.io":
        return "Reader"
    if host == "share.snipd.com":
        return "Snipd"
    return host or "Openen"


def _obsidian(vault: str, path: str) -> str:
    return f"obsidian://open?vault={quote(vault)}&file={quote(path.removesuffix('.md'))}"


def copy_box(value: str):
    return Div(Input(value=value, readonly=True, aria_label="tekst om te kopiëren"),
               Button("Kopieer", type="button", onclick="cp(this)"), cls="copy")


def open_links(r):
    """Every way to open the item, best first. A podcast without a Snipd link
    gets its title to copy, so it can be found with Snipd's search."""
    links = []
    for col in ("snipd_url", "reader_url", "url"):
        if r[col]:
            links.append(A(f"{_label(r[col])} ↗", href=r[col], cls="btn", target="_blank",
                           rel="noopener"))
    fname = r["file_name"] or ""
    if fname.startswith("Hopswiki:"):
        links.append(A("Hopswiki ↗", href=_obsidian("Hopswiki", fname.split(":", 1)[1].strip()),
                       cls="btn"))
        fname = ""
    if not links and not fname and r["note_path"]:
        # The note IS the item: a report written into the vault.
        links.append(A("Braincave ↗", href=_obsidian("Braincave", r["note_path"]), cls="btn"))
    out = [Div(*links, cls="row")] if links else []
    if fname:
        out.append(copy_box(Path(fname).name if "/" in fname else fname))
    if r["media_type"] == "listen" and not r["snipd_url"]:
        out.append(copy_box(r["title"]))
    return out


# --- one item --------------------------------------------------------------

def _post(iid: int, action: str, back: str, *children, **kw):
    return Form(*children, Input(type="hidden", name="back", value=back),
                method="post", action=f"/item/{iid}/{action}", **kw)


def meta_line(r) -> str:
    bits = [r["author"], r["source"]]
    if r["auto_rating"]:
        bits.append(f"AI {r['auto_rating']}")
    if r["rang"]:
        bits.append(f"rang {r['rang']}")
    bits += json.loads(r["goals"] or "[]")
    if r["lane"]:
        bits.append(r["lane"])
    if r["revisit"]:
        bits.append("🔁 opnieuw")
    return " · ".join(b for b in bits if b)


def card(r, back: str, stale: str | None = None):
    iid = r["id"]
    parts = [Header(H2(r["title"])), P(meta_line(r), cls="meta")]
    if r["landing"]:
        parts.append(P(f"→ {r['landing']}", cls="landing"))
    parts += open_links(r)

    if stale:  # not on this quarter: carry it or drop it
        parts.append(Div(_post(iid, "carry", back,
                               Input(type="hidden", name="quarter", value=stale),
                               Button(f"Naar {stale}")), cls="row"))
    elif r["status"] == "dropped":
        parts.append(P(f"Gevallen {r['dropped_at'] or ''}: {r['drop_code'] or '—'}"
                       + (f" — {r['drop_text']}" if r["drop_text"] else ""), cls="meta"))
        parts.append(Div(_post(iid, "status", back, Button("Terugzetten", name="status",
                                                           value="todo")), cls="row"))
        return Article(*parts, id=f"i{iid}")
    else:
        parts.append(Div(_post(iid, "status", back, *[
            Button(lbl, name="status", value=s, cls="on" if r["status"] == s else "")
            for s, lbl in STATUS_LABEL.items()]), cls="row"))

    if not stale:
        parts.append(Div(Span("Mijn oordeel", cls="meta"), _post(iid, "rating", back, *[
            Button(t, name="tier", value="" if r["my_rating"] == t else t,
                   cls="on" if r["my_rating"] == t else "",
                   aria_pressed=str(r["my_rating"] == t).lower())
            for t in cl.TIERS]), cls="row"))
    # Depth changes rarely, so it sits under "Meer" with the drop form.
    more = [Div(Span("Diepte", cls="meta"), _post(iid, "stage", back, *[
                Button(s, name="stage", value=s, cls="on" if r["stage"] == s else "")
                for s in cl.STAGES]), cls="row"),
            _post(iid, "drop", back,
                  # No reason chosen beforehand: a drop must be a decision, not a
                  # mis-tap on the button with the first reason already filled in.
                  Select(Option("kies een reden…", value="", selected=True, disabled=True),
                         *[Option(c, value=c) for c in cl.DROP_CODES], name="code",
                         aria_label="reden", required=True),
                  Input(name="text", placeholder="toelichting (mag leeg)"),
                  Button("Laten vallen"))]
    if r["media_type"] == "listen":
        more.append(_post(iid, "snipd", back,
                          Input(name="url", value=r["snipd_url"] or "",
                                placeholder="https://share.snipd.com/episode/…"),
                          Button("Snipd-link bewaren")))
    parts.append(Details(Summary("Meer"), *more))
    return Article(*parts, id=f"i{iid}")


# --- pages -----------------------------------------------------------------

def _qs(**kw) -> str:
    return urlencode({k: v for k, v in kw.items() if v})


@app.get("/")
def index(q: str = "", m: str = "listen", show: str = "open"):
    now = cl.current_quarter()
    q = q if len(q) == 7 and q[4:6] == "-Q" else now
    m = m if m in cl.MEDIA else "listen"
    show = show if show in SHOW_LABEL else "open"
    back = _qs(q=q, m=m, show=show)
    with closing(db()) as conn:
        items = cl.list_items(conn, q, m, show)
        n = cl.counts(conn, q, show)
        w = cl.wip(conn)
        stale = cl.stale_open(conn, q, m) if show == "open" and q == now else []

    wip_txt = (f"Open: {w['open']} ({w['this_quarter']} dit kwartaal, "
               f"{w['open'] - w['this_quarter']} uit eerdere) · grens {w['cap']} "
               f"({w['done_in_window']} klaar in 90 dagen)")
    if w["over_cap"]:
        wip_txt += " — eerst iets afmaken of laten vallen"
    body = [
        Header(H1(f"Consumelijst {q}"),
               P(wip_txt, cls="wip over" if w["over_cap"] else "wip"),
               Nav(A(f"← {cl.shift_quarter(q, -1)}", href="/?" + _qs(q=cl.shift_quarter(q, -1), m=m, show=show)),
                   *([A("nu", href="/?" + _qs(m=m, show=show))] if q != now else []),
                   A(f"{cl.shift_quarter(q, 1)} →", href="/?" + _qs(q=cl.shift_quarter(q, 1), m=m, show=show)))),
        Nav(*[A(f"{MEDIA_LABEL[k]} ({n[k]})", href="/?" + _qs(q=q, m=k, show=show),
                cls="on" if k == m else "") for k in cl.MEDIA]),
        Nav(*[A(lbl, href="/?" + _qs(q=q, m=m, show=k), cls="on" if k == show else "")
              for k, lbl in SHOW_LABEL.items()]),
    ]
    body += [card(r, back) for r in items] or [P("Niets hier.", cls="empty")]
    if stale:
        body.append(H2(f"Open uit eerdere kwartalen ({len(stale)})", cls="section"))
        body.append(P("Meenemen naar dit kwartaal, of laten vallen.", cls="meta"))
        body += [card(r, back, stale=q) for r in stale]
    return Title(f"Consumelijst {q}"), Main(*body)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def _back(iid: int, back: str):
    # `back` is our own query string; rebuild it so nothing else gets through.
    p = {k: v[0] for k, v in parse_qs(back).items() if k in ("q", "m", "show")}
    return RedirectResponse(f"/?{_qs(**p)}#i{iid}", status_code=303)


def _apply(iid: int, back: str, fn, *args):
    try:
        with closing(db()) as conn:
            fn(conn, iid, *args)
    except ValueError as e:
        return Response(f"Niet opgeslagen: {e}", status_code=400)
    return _back(iid, back)


@app.post("/item/{iid}/status")
def post_status(iid: int, status: str, back: str = ""):
    return _apply(iid, back, cl.set_status, status)


@app.post("/item/{iid}/stage")
def post_stage(iid: int, stage: str, back: str = ""):
    return _apply(iid, back, cl.set_stage, stage)


@app.post("/item/{iid}/rating")
def post_rating(iid: int, tier: str = "", back: str = ""):
    return _apply(iid, back, cl.set_my_rating, tier)


@app.post("/item/{iid}/drop")
def post_drop(iid: int, code: str, text: str = "", back: str = ""):
    return _apply(iid, back, cl.drop, code, text)


@app.post("/item/{iid}/carry")
def post_carry(iid: int, quarter: str, back: str = ""):
    return _apply(iid, back, cl.carry, quarter)


@app.post("/item/{iid}/snipd")
def post_snipd(iid: int, url: str = "", back: str = ""):
    return _apply(iid, back, cl.set_snipd_url, url)


if __name__ == "__main__":
    with closing(connect()) as c:
        init_schema(c)
    print(f"database: {db_path()}")
    serve(appname="__main__", host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8765")), reload=False)
