# consume_web

The consume list as a web page for phone, Boox and Mac. It reads and writes
`consume_items` in `l-space.db`. The logic lives in
`consume_selection/consume_list.py`. Most items arrive through the advice
(`_CONSUME_ADVICE` → `_CONSUME_LIST`), but every tab can also add one by hand,
from the catalogue only; there is no free-text entry. The Boeken tab searches his
LibraryThing and ToRead books (since 2026-10-24). Artikelen, Kijken and Luisteren
search Readwise, ToRead, Hopswiki, Watch Later and the podcast feeds (since
2026-11-12). The list stays a filter against hoarding: when an add by hand puts
the list over its cap, one advice item he has not started (`lane` set, status
`todo`; the oldest of the same medium, else of another) is dropped with
`iets-urgenter`. The page names the dropped item and offers Terugzetten. Hand-added
items have no `lane`, so they never make way themselves.

**Fresh catalogue on request** (2026-11-12). What he adds by hand is mostly what
he just saved, and the nightly intake is too late for it. A search on Kijken or
Artikelen is a POST to `/find`. Unless the catalogue was refreshed within 10
minutes, that writes a row in `catalogue_refresh`. The box's `catalogue-refresh`
job (every minute, `_CONSUME_LIST/Tools/CatalogueRefresh.py`) claims the row,
gives every playlist video a catalogue row, ingests recent Reader saves, and
writes its result back. While a request is open the page reloads itself every
5 s. After 5 minutes it says that the box job is not running. "Nu verversen"
forces a refresh. Podcasts have no on-demand refresh, because their feeds are
fetched on the Mac at night.

Five tabs: Boeken, Artikelen, Kijken, Luisteren, Werk. **Werk** is reading his job
needs, by a deadline: added by hand here only (typed in, or a ToRead file), with
a bookmark like a book, and three ways out: Klaar, Deadline verzetten, Laten
vallen. Every tab shows how many work documents are late or due within a week.
No cap: the deadline is the control. Of the other four: A book is read in sessions and
has a bookmark (`p. 30 / 145 · 21%`); books count against their own limit of 5,
not the item limit. Which catalogue item is a book is decided by the `item_media`
view: LibraryThing, a ToRead EPUB, or a ToRead PDF over 25 pages.

## Run it on the Mac Mini

```bash
docker compose -f consume_web/compose.yaml up -d --build
tailscale serve --bg 8765
```

The container listens on the Mac's loopback (`127.0.0.1:8765`) only.
`tailscale serve` puts HTTPS in front of it on the tailnet: open
`https://mm.taila9d9bf.ts.net` on a device with Tailscale on.
There is no login, so the tailnet is the login. Writes refuse a request whose
`Origin`/`Referer` is not the page itself.

`tailscale serve` needs "HTTPS Certificates" switched on once in the Tailscale
admin console (DNS page). The certificate is public in Certificate Transparency
logs, so the machine and tailnet names are public; the machine is still only
reachable from the tailnet (decided 2026-10-24). `--bg` keeps the setting
across reboots; `tailscale serve status` shows it and `tailscale serve reset`
removes it.

**On the phone:** open the address in Safari, then Share → "Zet op beginscherm".
It opens as its own app, without the browser bar. There is no offline mode: every
tap writes to the database.

## Two writers, one database

The Hoggle box and this container both write `l-space.db`. That is safe because
both run in OrbStack's one Linux VM and share SQLite's WAL locking there.

**Never open `l-space.db` with macOS's own `sqlite3` while either runs.** WAL
coordinates through the `-shm` shared-memory file, and a macOS process sees that
file from outside the VM. Check the database from inside a container instead.

The check that both writers work together, from the Mac (passed 2026-10-23):

```bash
docker exec consume-web python -c "import sqlite3; c=sqlite3.connect('/data/l-space.db',timeout=30); [(c.execute('UPDATE consume_items SET updated_at=updated_at WHERE id=1'),c.commit()) for _ in range(2000)]; print('container ok')" &
docker exec -u hoggle labyrinth python3 -c "import sqlite3; c=sqlite3.connect('/home/hoggle/data/l-space-librarian/l-space.db',timeout=30); [(c.execute('UPDATE consume_items SET updated_at=updated_at WHERE id=2'),c.commit()) for _ in range(2000)]; print('box ok')"
wait
docker exec consume-web python -c "import sqlite3; print(sqlite3.connect('/data/l-space.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

Expected: `container ok`, `box ok`, then `ok`.

## Develop

```bash
CONSUME_DB=/path/to/a/copy/l-space.db uv run consume_web/app.py   # 127.0.0.1:8765
uv run --with pytest --with fastcore pytest tests/test_consume_list.py
```

Test against a copy, never the live file.
