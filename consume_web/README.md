# consume_web

The consume list as a web page for phone, Boox and Mac. It reads and writes
`consume_items` in `l-space.db`. The logic lives in
`consume_selection/consume_list.py`. Adding items is not possible here, on purpose:
the list is a filter against hoarding, and items arrive through the
`_CONSUME_LIST` skill.

## Run it on the Mac Mini

```bash
TS_IP=$(tailscale ip -4) docker compose -f consume_web/compose.yaml up -d --build
```

Open `http://<tailscale-ip>:8765` on a device with Tailscale on. There is no login:
the port is published on the Tailscale address only, so the tailnet is the login.
Writes refuse a request whose `Origin`/`Referer` is not the page itself.

After a reboot, the container can only bind that address once Tailscale is up on
the Mac. If `docker ps` shows `consume-web` not running, start it again with the
command above.

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
