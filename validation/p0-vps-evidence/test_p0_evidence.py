"""Tests for the P0 read-only evidence collector and analyzer.

    python3 -m unittest validation/p0-vps-evidence/test_p0_evidence.py -v
"""
import contextlib
import hashlib
import io
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import analyze_p0_evidence as A  # noqa: E402
import collect_p0_evidence as C  # noqa: E402

# Exact DDL of the Price Action stores at 215c96c (price_action_lab.py, price_action_governance.py).
PA_DDL = """
CREATE TABLE IF NOT EXISTS pa_activity(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
  symbol TEXT, strategy_id TEXT, object_id TEXT, created_at TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS pa_evaluations(
  correlation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, candle_time TEXT NOT NULL,
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT NOT NULL,
  missing_conditions_json TEXT NOT NULL DEFAULT '[]',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(session_id,idempotency_key));
CREATE INDEX IF NOT EXISTS pa_activity_session_created ON pa_activity(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS pa_evaluations_session_candle ON pa_evaluations(session_id, candle_time DESC);
CREATE TABLE IF NOT EXISTS pa_journal_revisions(
  id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
  initiated_by TEXT NOT NULL, payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(journal_id,revision_no), UNIQUE(journal_id,payload_hash));
"""


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def opts(**kw):
    base = dict(query_budget=2.0, db_budget=60.0, sample_k=48, big_sample_k=256,
                exact_max_bytes=0, index_fill=0.75)
    base.update(kw)
    return SimpleNamespace(**base)


def build_pa_db(path, revisions=6000, evaluations=20000, activity=40000, seed=7):
    rnd = random.Random(seed)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(PA_DDL)
    h = lambda *a: hashlib.sha256("|".join(map(str, a)).encode()).hexdigest()
    rows = []
    for i in range(revisions):
        reason = "MATERIAL_EVIDENCE_CHANGED" if rnd.random() < 0.8 else rnd.choice(
            ["SETUP_CREATED", "LIFECYCLE_TRANSITION", "EXECUTION_FILL", "OUTCOME_CLOSED"])
        payload = json.dumps({"i": i, "pad": "".join(rnd.choice("abcdef0123456789") for _ in range(9400))})
        rows.append((f"rev-{h('r', i)}", f"j-{i // 25}", i % 25, reason, f"2026-09-{1 + i * 28 // revisions:02d}T00:00:00Z",
                     "price_action_runtime", h("p", i), payload))
    c.executemany("INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)", rows)
    rows = [(f"PA-{h('c', i)}", "sess-1", h("i", i), f"2026-08-24T{i % 24:02d}:00:00Z", "BTCUSDT", "5m", "PA_V1", "1",
             "WATCHING", "no eligible setup", "[]", json.dumps({"pad": "x" * rnd.randint(2400, 3400)}),
             "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z") for i in range(evaluations)]
    c.executemany("INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    rows = [(f"act-{h('a', i)}", "sess-1", rnd.choice(["closed_candle_decision", "market_data_stream_event"]),
             "BTCUSDT", "PA_V1", f"o{i}", "2026-09-01T00:00:00Z", json.dumps({"pad": "y" * rnd.randint(150, 450)}))
            for i in range(activity)]
    c.executemany("INSERT INTO pa_activity VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()


class SafetyTests(unittest.TestCase):
    def test_docker_allowlist(self):
        ok = [["ps", "-a"], ["inspect", "x"], ["inspect", "--size", "a", "b"], ["image", "ls"], ["image", "inspect", "i"],
              ["volume", "ls"], ["volume", "inspect", "v"], ["system", "df"], ["logs", "--since", "60m", "c"],
              ["stats", "--no-stream"], ["info"], ["version"]]
        bad = [["exec", "c", "sh"], ["rm", "c"], ["image", "rm", "i"], ["image", "prune"], ["system", "prune"],
               ["volume", "rm", "v"], ["stats"], ["compose", "ps"], ["run", "alpine"], ["container", "rm", "c"],
               ["cp", "c:/x", "/y"], ["restart", "c"], ["logs", "c", "rm"], []]
        for a in ok:
            self.assertTrue(C.docker_args_allowed(a), a)
        for a in bad:
            self.assertFalse(C.docker_args_allowed(a), a)

    def test_runner_refuses_before_executing(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            with self.assertRaises(PermissionError):
                C.Runner().docker(["exec", "app", "sh"])
            with self.assertRaises(PermissionError):
                C.Runner().cmd(["rm", "-rf", "/"])

    def test_environment_values_never_leak(self):
        env = ["HUB_MASTER_KEY=master-secret-1", "SUPABASE_KEY=supa-secret-2", "HUB_SECRET=session-secret-3",
               "HUB_CONTROL_KEY=control-secret-4", "GIT_COMMIT=abc123", "PRICE_ACTION_LAB_ENABLED=false"]
        out = json.dumps(C.filter_env(env))
        for secret in ("master-secret-1", "supa-secret-2", "session-secret-3", "control-secret-4", "HUB_SECRET",
                       "HUB_CONTROL_KEY"):
            self.assertNotIn(secret, out)
        parsed = json.loads(out)
        self.assertEqual(parsed["values"]["GIT_COMMIT"], "abc123")
        self.assertTrue(parsed["present"]["HUB_MASTER_KEY"])

    def test_container_inventory_is_sanitized(self):
        inspect = [{"Id": "a" * 64, "Name": "/nexus-trading-bot-app-1", "Image": "sha256:" + "d" * 64,
                    "Config": {"Image": "nexus-trading-bot-app:main-abc", "Env": ["HUB_SECRET=zzz-top", "GIT_COMMIT=abc"],
                               "Labels": {"com.docker.compose.project": "nexus-trading-bot", "other.secret": "nope"}},
                    "State": {"Status": "running", "Pid": 42, "StartedAt": "2026-10-17T17:56:16.5Z"},
                    "HostConfig": {"Memory": 0}, "Mounts": [{"Type": "volume", "Name": "v", "Source": "/x",
                                                             "Destination": "/var/lib/tradexa", "RW": True}],
                    "NetworkSettings": {"Networks": {"default": {"IPAddress": "172.18.0.2"}}}}]

        class Fake(C.Runner):
            def docker(self, args, timeout=60):
                assert C.docker_args_allowed(args)
                return ("a" * 64 + "\n") if args[0] == "ps" else json.dumps(inspect)
        out = json.dumps(C.containers(Fake()))
        self.assertNotIn("zzz-top", out)
        self.assertNotIn("other.secret", out)
        self.assertIn("nexus-trading-bot-app-1", out)

    def test_access_log_parser_drops_client_data(self):
        lines = [
            '203.0.113.9 - - [22/Oct/2026:10:00:00 +0000] "GET /research/price-action/bot-status HTTP/1.1" 200 3901 "-" "Mozilla"',
            '203.0.113.9 - - [22/Oct/2026:10:00:01 +0000] "GET /journal/records/0123456789abcdef?token=abc HTTP/1.1" 504 0 "-" "x"',
            '198.51.100.4 - - [22/Oct/2026:10:00:02 +0000] "GET /instances HTTP/2.0" 200 1448 "https://ref" "UA"']
        out = C.parse_access_log("\n".join(lines))
        text = json.dumps(out)
        for leak in ("203.0.113.9", "198.51.100.4", "token=abc", "Mozilla", "https://ref"):
            self.assertNotIn(leak, text)
        self.assertEqual(out["requests"], 3)
        self.assertIn("/journal/records/:id", out["error_paths"])

    def test_clip_redacts_tokens(self):
        self.assertNotIn("abc", C.clip("failed with token=abc"))


class HeaderTests(unittest.TestCase):
    def test_truncated_copy_is_detected_without_opening(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.db")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE t(x)")
            c.executemany("INSERT INTO t VALUES (?)", [("z" * 500,)] * 200)
            c.commit(); c.close()
            self.assertTrue(C.sqlite_header(p)["header_size_matches_file"])
            with open(p, "rb") as fh:
                data = fh.read()
            q = os.path.join(d, "cut.db")
            with open(q, "wb") as fh:
                fh.write(data[: len(data) // 2])
            self.assertFalse(C.sqlite_header(q)["header_size_matches_file"])
            n = os.path.join(d, "not.db")
            with open(n, "wb") as fh:
                fh.write(b"hello")
            self.assertFalse(C.sqlite_header(n)["valid"])


class InspectDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="p0-pa-")
        cls.path = os.path.join(cls.dir, "price_action_paper.db")
        build_pa_db(cls.path)
        exact = sqlite3.connect(cls.path)
        cls.exact = dict(exact.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall())
        cls.page = exact.execute("PRAGMA page_size").fetchone()[0]
        exact.close()

    @classmethod
    def tearDownClass(cls):
        for f in os.listdir(cls.dir):
            os.remove(os.path.join(cls.dir, f))
        os.rmdir(cls.dir)

    def test_reader_changes_nothing_while_app_holds_wal(self):
        app = sqlite3.connect(self.path)          # the "app": keeps -wal/-shm alive
        app.execute("PRAGMA journal_mode=WAL")
        app.execute("INSERT INTO pa_activity VALUES ('extra','s','k',NULL,NULL,NULL,'t','{}')")
        app.commit()
        files = sorted(os.listdir(self.dir))
        before = {f: sha(os.path.join(self.dir, f)) for f in files}
        stats = {f: os.stat(os.path.join(self.dir, f)).st_mtime_ns for f in files}
        info = C.inspect_db(self.path, opts(), big_tables=("pa_journal_revisions", "pa_evaluations", "pa_activity"))
        self.assertTrue(info["opened"])
        self.assertNotIn("error", info["inspection"])
        self.assertEqual(sorted(os.listdir(self.dir)), files, "no file may be created")
        self.assertEqual({f: sha(os.path.join(self.dir, f)) for f in files}, before, "no byte may change")
        self.assertEqual({f: os.stat(os.path.join(self.dir, f)).st_mtime_ns for f in files}, stats)
        app.close()

    def test_concurrent_writer_is_never_blocked(self):
        app = sqlite3.connect(self.path, timeout=0.05, check_same_thread=False)
        app.execute("PRAGMA journal_mode=WAL")
        stop, errors, writes = threading.Event(), [], [0]

        def writer():
            n = 0
            while not stop.is_set():
                try:
                    app.execute("INSERT INTO pa_activity VALUES (?,?,?,?,?,?,?,?)",
                                (f"live-{n}", "s", "k", None, None, None, "t", "{}"))
                    app.commit()
                    n += 1
                    writes[0] = n
                except sqlite3.OperationalError as exc:
                    errors.append(str(exc))
        t = threading.Thread(target=writer)
        t.start()
        try:
            info = C.inspect_db(self.path, opts(), big_tables=("pa_journal_revisions",))
        finally:
            stop.set(); t.join()
            app.execute("DELETE FROM pa_activity WHERE id LIKE 'live-%'"); app.commit(); app.close()
        self.assertEqual(errors, [], "the writer must never see busy/locked")
        self.assertGreater(writes[0], 10)
        self.assertNotIn("error", info["inspection"])

    def test_estimates_track_exact_page_accounting(self):
        info = C.inspect_db(self.path, opts(), big_tables=("pa_journal_revisions", "pa_evaluations", "pa_activity"))
        tables = info["inspection"]["tables"]
        report = {}
        for name in ("pa_journal_revisions", "pa_evaluations", "pa_activity"):
            est, exact = tables[name]["est_bytes"], self.exact[name]
            report[name] = round(est / exact, 3)
            self.assertLess(abs(est - exact) / exact, 0.15, f"{name}: est {est} vs exact {exact}")
        est_total = sum(t["est_bytes"] + sum(t["est_index_bytes"].values()) for t in tables.values())
        exact_total = sum(self.exact.values())
        report["total_incl_indexes"] = round(est_total / exact_total, 3)
        self.assertLess(abs(est_total - exact_total) / exact_total, 0.20)
        cats = tables["pa_journal_revisions"]["sample_categories"]
        share = cats.get("MATERIAL_EVIDENCE_CHANGED", 0) / sum(cats.values())
        self.assertAlmostEqual(share, 0.8, delta=0.08)
        print(f"\n  estimate/exact ratios: {report}; sampled duplicate share {share:.2f}")


class WalkTests(unittest.TestCase):
    def test_bounded_walk(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a", "b"))
            for i, size in enumerate((10_000, 200_000, 3_000)):
                with open(os.path.join(d, "a", "b" if i else "", f"f{i}.db"), "wb") as fh:
                    fh.write(b"x" * size)
            os.link(os.path.join(d, "a", "b", "f1.db"), os.path.join(d, "hard.db"))
            w = C.walk(d)
            self.assertEqual(w["files"], 3, "hard link counted once")
            self.assertEqual(w["apparent_bytes"], 213_000)
            self.assertEqual(w["largest_files"][0]["apparent"], 200_000)
            self.assertTrue(w["allocated_by_mtime_day"])
            self.assertEqual(C.walk(d, max_entries=1)["truncated"], "max_entries")


class BackupTests(unittest.TestCase):
    def test_snapshot_listing_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            snap = os.path.join(d, "backups", "20261022T080000Z")
            os.makedirs(snap)
            good = os.path.join(snap, "ledger.db")
            c = sqlite3.connect(good); c.execute("CREATE TABLE t(x)")
            c.executemany("INSERT INTO t VALUES (?)", [("q" * 900,)] * 100); c.commit(); c.close()
            with open(good, "rb") as fh:
                data = fh.read()
            with open(os.path.join(snap, "price_action_paper.db"), "wb") as fh:
                fh.write(data[:4096 * 2])
            with open(os.path.join(snap, "manifest.json"), "w") as fh:
                json.dump({"created": "2026-10-22T08:00:00Z", "files": ["ledger.db"],
                           "errors": ["price_action_paper.db: database or disk is full"]}, fh)
            with open(os.path.join(d, "backups", "20261021T080000Z.tlxb"), "wb") as fh:
                fh.write(b"\0" * 1000)
            with open(os.path.join(d, "backups", "20261021T080000Z.tlxb.json"), "w") as fh:
                json.dump({"files": ["a.db", "b.db"], "encrypted": True}, fh)
            conts = [{"name": "app", "mounts": [{"destination": "/var/lib/tradexa", "source": d}]}]
            out = C.backups(conts, [])["app"]
            self.assertEqual(out["snapshot_count"], 2)
            snap_row = next(e for e in out["entries"] if e["name"] == "20261022T080000Z")
            self.assertFalse(snap_row["db_header_checks"]["price_action_paper.db"]["header_size_matches_file"])
            self.assertTrue(snap_row["db_header_checks"]["ledger.db"]["header_size_matches_file"])
            self.assertIn("disk is full", snap_row["manifest"]["errors"][0])


@unittest.skipUnless(subprocess.run(["git", "-C", REPO, "cat-file", "-e", "215c96c^{commit}"],
                                    capture_output=True).returncode == 0, "audited commits not fetched")
class RevisionMatchTests(unittest.TestCase):
    def _app_tree(self, commit, d):
        paths = [p for p in C.FINGERPRINT_FILES
                 if subprocess.run(["git", "-C", REPO, "cat-file", "-e", f"{commit}:{p}"], capture_output=True).returncode == 0]
        archive = subprocess.run(["git", "-C", REPO, "archive", commit, *paths], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=archive, check=True)

    def test_running_source_maps_to_its_commit(self):
        for commit, lineage in (("215c96c", "guardian"), ("f8778ed", "main")):
            with tempfile.TemporaryDirectory() as d:
                self._app_tree(commit, d)
                blobs = C.fingerprint_root(d)
                m = A.match_revision(blobs, REPO)
                self.assertTrue(any(line.startswith(commit) for line in m["exact"]), (commit, m))
                lin = A.lineage(REPO, m["exact"][0])
                self.assertTrue(lin[lineage], (commit, lin))
                if lineage == "guardian":
                    self.assertFalse(lin["main"])


class AnalyzerSmokeTests(unittest.TestCase):
    def test_report_renders_from_partial_evidence(self):
        ev = {"schema": C.SCHEMA, "host": "vps", "started_at": "t0", "finished_at": "t1",
              "containers": [], "disk": {"walks": {"volume:v": {"allocated_bytes": 10, "files": 1,
                                                                "allocated_by_mtime_day": {"2026-10-21": 10},
                                                                "largest_files": []}}},
              "resources": {"error": "skipped"}, "backups": {}, "databases": {}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(ev, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.main([fh.name])
        os.remove(fh.name)
        out = buf.getvalue()
        self.assertIn("| B1 |", out)
        self.assertIn("2026-10-21", out)


if __name__ == "__main__":
    unittest.main()
