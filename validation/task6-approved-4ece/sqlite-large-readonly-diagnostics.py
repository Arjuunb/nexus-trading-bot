"""Run only inside the existing staging API. Reads two configured stores only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

for name in ("price_action_paper.db", "smc_strategy_paper.db"):
    path = Path("/var/lib/tradexa") / name
    result = {"database": name}
    timed_out = [False]
    phase = "open"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            phase = "metadata"
            for pragma in ("page_size", "page_count", "freelist_count", "journal_mode"):
                result[pragma] = db.execute("PRAGMA " + pragma).fetchone()[0]
            phase = "quick_check"
            deadline = time.monotonic() + 5

            def progress():
                timed_out[0] = time.monotonic() >= deadline
                return int(timed_out[0])

            db.set_progress_handler(progress, 10000)
            rows = db.execute("PRAGMA quick_check(1)").fetchall()
            result["quick_check"] = "ok" if rows == [("ok",)] else "FAILED"
    except sqlite3.Error as error:
        result.update(error_phase=phase, error_type=type(error).__name__,
                      sqlite_code=getattr(error, "sqlite_errorcode", None),
                      sqlite_name=getattr(error, "sqlite_errorname", None))
    result["timeout_callback_fired"] = timed_out[0]
    print(json.dumps(result))
