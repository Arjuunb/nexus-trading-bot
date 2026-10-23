# Staging API stand-in: a background thread writes to two WAL-mode SQLite files
# (like the PA/SMC labs), and the ASGI lifespan shutdown stops it, as the real app does.
import json, os, sqlite3, threading, time

DATA = os.environ.get("HUB_DATA_DIR", "/var/lib/tradexa")
stop = threading.Event()

def writer():
    conns = []
    for name in ("price_action_paper.db", "smc_strategy_paper.db"):
        c = sqlite3.connect(os.path.join(DATA, name), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS v2_source_evidence(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
        conns.append(c)
    while not stop.is_set():
        for c in conns:
            c.execute("INSERT INTO v2_source_evidence(kind, payload) VALUES ('EQUITY_OBSERVED', ?)", ("x" * 1100,))
            c.commit()
        time.sleep(0.02)
    for c in conns:
        c.close()
    print("[shutdown] lab writers closed", flush=True)

thread = threading.Thread(target=writer, daemon=True)

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                thread.start()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                stop.set(); thread.join(10)
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = json.dumps({"status": "ok", "commit_short": "f8778ed"}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
