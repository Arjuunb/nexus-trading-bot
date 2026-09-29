"""Strategy research intelligence (PRD §12-16, §26, §38; Phase 5).

Observations become hypotheses, never changes. Production strategies are
never touched: everything here reads the journal's finished trades and
writes only Guardian's own research store.

**Strategy analyst (§12).** Per strategy *and version* (§16: results of
different versions are never combined), the journal's closed trades are cut
by side, session, regime, higher-timeframe bias, symbol, timeframe and setup
type, with winners and losers summarised separately.

**Hypotheses (§13).** A cohort whose trades lose significantly more than the
rest of the same strategy version becomes a filter hypothesis -- "excluding
this cohort may improve expectancy" -- with status UNPROVEN. It is found on
the first 60% of the trades by close time only, so the last 40% are a true
hold-out. A hypothesis is kept whatever happens to it, so a rejected idea is
never "rediscovered" (§26).

**Pipeline (§14).** Every stage runs in order and none is skipped:

    OBSERVATION -> HYPOTHESIS -> HISTORICAL_BACKTEST -> OUT_OF_SAMPLE
    -> WALK_FORWARD -> STRESS_TEST -> FORWARD_PAPER -> STATISTICAL_COMPARISON
    -> RECOMMENDATION -> OWNER_APPROVAL -> PRODUCTION_CANDIDATE

For a filter hypothesis each test replays the filter over recorded trades:
the trades it would have skipped are removed and expectancy compared. That
is exact for a filter (it only removes trades) but it is not a candle-level
backtest -- it cannot see trades the strategy would have taken instead --
and every stage says so. FORWARD_PAPER uses only trades closed after the
hypothesis was created. A failed stage ends the hypothesis as
REJECTED_BY_EVIDENCE. OWNER_APPROVAL is the owner's alone (§38): Guardian
recommends; approval means "approved for development", and production code
changes only when a person implements and deploys it.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from services.guardian.strategy import event_key, read_only

STAGES = ("OBSERVATION", "HYPOTHESIS", "HISTORICAL_BACKTEST", "OUT_OF_SAMPLE", "WALK_FORWARD",
          "STRESS_TEST", "FORWARD_PAPER", "STATISTICAL_COMPARISON", "RECOMMENDATION",
          "OWNER_APPROVAL", "PRODUCTION_CANDIDATE")
DIMENSIONS = ("side", "trading_session", "market_regime", "htf_bias", "symbol", "timeframe", "setup_type")
MIN_COHORT = 20
UNPROVEN, TESTING, RECOMMENDED = "UNPROVEN", "TESTING", "RECOMMENDED"
APPROVED, REJECTED_EVIDENCE, REJECTED_OWNER = "APPROVED_FOR_DEVELOPMENT", "REJECTED_BY_EVIDENCE", "REJECTED_BY_OWNER"
_FINAL = (APPROVED, REJECTED_EVIDENCE, REJECTED_OWNER)
METHOD_NOTE = ("Filter replay over recorded trades: the trades the filter would skip are removed and "
               "expectancy compared. Exact for a filter, but not a candle-level backtest: it cannot "
               "see trades the strategy would have taken instead.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    record_source TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    dimension TEXT NOT NULL, value TEXT NOT NULL,
    observation TEXT NOT NULL, hypothesis TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    stages TEXT NOT NULL,
    discovery_cutoff TEXT NOT NULL,
    sample INTEGER NOT NULL,
    owner_notes TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_hypothesis_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER NOT NULL,
    at TEXT NOT NULL, actor TEXT NOT NULL, entry TEXT NOT NULL, detail TEXT NOT NULL, evidence TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_ghl_no_update BEFORE UPDATE ON guardian_hypothesis_log
BEGIN SELECT RAISE(ABORT, 'the research log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ghl_no_delete BEFORE DELETE ON guardian_hypothesis_log
BEGIN SELECT RAISE(ABORT, 'the research log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_gh_no_delete BEFORE DELETE ON guardian_hypotheses
BEGIN SELECT RAISE(ABORT, 'hypotheses are kept so failed ideas are not rediscovered'); END;
"""


# ------------------------------------------------------------ statistics
def welch(a: list[float], b: list[float]) -> dict:
    """Difference of means with Welch's standard error and a two-sided
    normal-approximation p-value (samples of 20+ each)."""
    ma, mb = statistics.mean(a), statistics.mean(b)
    va = statistics.variance(a) if len(a) > 1 else 0.0
    vb = statistics.variance(b) if len(b) > 1 else 0.0
    se = math.sqrt(va / len(a) + vb / len(b))
    z = (ma - mb) / se if se > 0 else 0.0
    return {"mean_a": round(ma, 4), "mean_b": round(mb, 4), "diff": round(ma - mb, 4),
            "z": round(z, 3), "p": round(math.erfc(abs(z) / math.sqrt(2)), 5), "n_a": len(a), "n_b": len(b)}


def p_text(p: float) -> str:
    """A p-value as text; one that rounds to zero is said to be below the
    precision, never "0"."""
    return "p<0.00001" if p < 0.00001 else f"p={p}"


def filter_effect(trades: list[dict], dimension: str, value: str) -> Optional[dict]:
    """Expectancy (mean R) of all trades vs. the trades the filter keeps."""
    rs = [t["realized_r"] for t in trades]
    kept = [t["realized_r"] for t in trades if str(t.get(dimension)) != value]
    cut = [t["realized_r"] for t in trades if str(t.get(dimension)) == value]
    if not rs or not kept or not cut:
        return None
    return {"trades": len(rs), "skipped": len(cut), "expectancy_all": round(statistics.mean(rs), 4),
            "expectancy_filtered": round(statistics.mean(kept), 4),
            "improvement": round(statistics.mean(kept) - statistics.mean(rs), 4),
            "cohort_mean_r": round(statistics.mean(cut), 4)}


def _summary(trades: list[dict]) -> dict:
    rs = [t["realized_r"] for t in trades]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]

    def mean(key):
        vals = [float(t[key]) for t in trades if t.get(key) is not None]
        return round(statistics.mean(vals), 3) if vals else None
    return {"trades": len(rs), "win_rate": round(len(wins) / len(rs), 3),
            "average_r": round(statistics.mean(rs), 3), "total_r": round(sum(rs), 3),
            "mae_r": mean("mae_r"), "mfe_r": mean("mfe_r"), "slippage": mean("slippage"),
            "decision_latency_ms": mean("decision_latency_ms")}


# --------------------------------------------------------------- analyst
def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


def closed_trades(journal_path: Optional[str]) -> list[dict]:
    """Finished forward-paper trades with a measured R, oldest first. Only
    FORWARD_PAPER: simulated, backtested, research and migrated records are
    never mixed into the evidence."""
    if not journal_path:
        return []
    conn = read_only(journal_path)
    try:
        cols = ", ".join(("journal_record_id", "record_source", "record_origin", "strategy_id",
                          "strategy_version", "position_closed_at", "realized_r", "net_pnl", *DIMENSIONS,
                          "mae_r", "mfe_r", "slippage", "decision_latency_ms"))
        rows = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM trade_records WHERE status='CLOSED' AND realized_r IS NOT NULL "
            "AND position_closed_at IS NOT NULL AND record_origin='FORWARD_PAPER'")]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        conn.close()
    for r in rows:
        r["realized_r"] = float(r["realized_r"])
        r["closed_ts"] = _epoch(r["position_closed_at"])
    rows.sort(key=lambda r: r["closed_ts"])
    return rows


def by_version(trades: Iterable[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for t in trades:
        key = (t["record_source"], t["strategy_id"] or "", t["strategy_version"] or "")
        out.setdefault(key, []).append(t)
    return out


def analyse(trades: list[dict]) -> dict:
    """PRD §12 for one strategy version: overall, winners vs losers, and
    every cohort -- only cohorts of 20+ trades are compared with the rest."""
    cohorts = []
    for dim in DIMENSIONS:
        values = sorted({str(t.get(dim)) for t in trades if t.get(dim) not in (None, "")})
        for value in values:
            inside = [t for t in trades if str(t.get(dim)) == value]
            rest = [t for t in trades if str(t.get(dim)) != value]
            row = {"dimension": dim, "value": value, **_summary(inside)}
            if len(inside) >= MIN_COHORT and len(rest) >= MIN_COHORT:
                row["versus_rest"] = welch([t["realized_r"] for t in inside], [t["realized_r"] for t in rest])
            cohorts.append(row)
    return {"overall": _summary(trades),
            "winners": _summary([t for t in trades if t["realized_r"] > 0]),
            "losers": _summary([t for t in trades if t["realized_r"] <= 0]),
            "cohorts": cohorts}


# ------------------------------------------------------------- the engine
class ResearchEngine:
    def __init__(self, store, *, journal_path: Optional[str], discovery_fraction: float = 0.6,
                 seed: int = 7, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self.journal_path = journal_path
        self.discovery_fraction = float(discovery_fraction)
        self.seed = int(seed)
        with store._lock:
            store._c.executescript(_SCHEMA)
            store._c.commit()

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    # --------------------------------------------------------------- store
    def _rows(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self.store._lock:
            return [dict(r) for r in self.store._c.execute(sql, tuple(args))]

    def _log(self, hid: int, actor: str, entry: str, detail: str, evidence: Any = None) -> None:
        self.store._c.execute(
            "INSERT INTO guardian_hypothesis_log(hypothesis_id,at,actor,entry,detail,evidence) VALUES (?,?,?,?,?,?)",
            (hid, self._now(), actor, entry, detail, None if evidence is None else json.dumps(evidence, default=str)))

    def list(self) -> list[dict]:
        return [self._decode(r) for r in self._rows(
            "SELECT * FROM guardian_hypotheses ORDER BY CASE WHEN status IN ('UNPROVEN','TESTING','RECOMMENDED') "
            "THEN 0 ELSE 1 END, id DESC")]

    def get(self, hid: int) -> Optional[dict]:
        rows = self._rows("SELECT * FROM guardian_hypotheses WHERE id=?", (int(hid),))
        if not rows:
            return None
        out = self._decode(rows[0])
        out["log"] = [{**r, "evidence": json.loads(r["evidence"]) if r["evidence"] else None}
                      for r in self._rows("SELECT * FROM guardian_hypothesis_log WHERE hypothesis_id=? "
                                          "ORDER BY seq", (int(hid),))]
        return out

    @staticmethod
    def _decode(row: dict) -> dict:
        out = dict(row)
        out["stages"] = json.loads(out["stages"])
        out["owner_notes"] = json.loads(out["owner_notes"])
        return out

    # ----------------------------------------------------------- discover
    def discover(self, trades: list[dict]) -> list[int]:
        """New hypotheses from the discovery window of each strategy version."""
        created = []
        for (source, strategy, version), rows in by_version(trades).items():
            n = int(len(rows) * self.discovery_fraction)
            window = rows[:n]
            if len(window) < 2 * MIN_COHORT:
                continue
            cutoff = window[-1]["position_closed_at"]
            for cohort in analyse(window)["cohorts"]:
                test = cohort.get("versus_rest")
                if not test or test["p"] >= 0.05 or test["diff"] >= 0 or test["mean_a"] >= 0:
                    continue
                identity = event_key("hypothesis", source, strategy, version, cohort["dimension"],
                                     cohort["value"], "exclude")
                if self._rows("SELECT id FROM guardian_hypotheses WHERE identity=?", (identity,)):
                    continue                         # kept forever: never rediscovered
                observation = (f"Trades with {cohort['dimension']} = {cohort['value']} average "
                               f"{test['mean_a']}R against {test['mean_b']}R for the rest of "
                               f"{strategy} {version} ({test['n_a']} vs {test['n_b']} trades, {p_text(test['p'])}).")
                hypothesis = (f"Skipping {cohort['dimension']} = {cohort['value']} setups may improve "
                              f"{strategy} {version}'s expectancy.")
                stages = {s: {"state": "PENDING"} for s in STAGES}
                stages["OBSERVATION"] = {"state": "PASS", "at": self._now(), "evidence": test}
                stages["HYPOTHESIS"] = {"state": "PASS", "at": self._now(),
                                        "evidence": {"dimension": cohort["dimension"], "value": cohort["value"],
                                                     "discovery_trades": len(window), "cutoff": cutoff}}
                with self.store._lock:
                    cur = self.store._c.execute(
                        "INSERT INTO guardian_hypotheses(identity,created_at,record_source,strategy_id,"
                        "strategy_version,dimension,value,observation,hypothesis,status,stage,stages,"
                        "discovery_cutoff,sample,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, self._now(), source, strategy, version, cohort["dimension"], cohort["value"],
                         observation, hypothesis, UNPROVEN, "HISTORICAL_BACKTEST", json.dumps(stages),
                         cutoff, len(window), self._now()))
                    hid = int(cur.lastrowid)
                    self._log(hid, "guardian", "created", hypothesis, {"observation": test})
                    self.store._c.commit()
                created.append(hid)
        return created

    # ---------------------------------------------------------- advance
    def _evaluate(self, stage: str, h: dict, rows: list[dict]) -> Optional[dict]:
        """One stage's verdict: {"state": PASS|FAIL, ...} or None to wait."""
        dim, value = h["dimension"], h["value"]
        cutoff, created = _epoch(h["discovery_cutoff"]), _epoch(h["created_at"])
        # Windows by close time, not position: a late journal backfill cannot
        # move a trade from one window into another.
        disc = [r for r in rows if r["closed_ts"] <= cutoff]
        hold = [r for r in rows if cutoff < r["closed_ts"] <= created]
        forward = [r for r in rows if r["closed_ts"] > created]

        def verdict(ok: bool, evidence: dict, why: str) -> dict:
            return {"state": "PASS" if ok else "FAIL", "at": self._now(), "evidence": evidence, "why": why,
                    "method": METHOD_NOTE}

        if stage == "HISTORICAL_BACKTEST":
            eff = filter_effect(disc, dim, value)
            return eff and verdict(eff["improvement"] > 0, eff, "the filter raises expectancy on the discovery trades")
        if stage == "OUT_OF_SAMPLE":
            cohort = [r for r in hold if str(r.get(dim)) == value]
            if len(cohort) < 10:
                return None                          # wait for a large enough hold-out
            eff = filter_effect(hold, dim, value)
            return eff and verdict(eff["improvement"] > 0 and eff["cohort_mean_r"] < 0, eff,
                                   "held-out trades the hypothesis never saw show the same effect")
        if stage == "WALK_FORWARD":
            base = disc + hold
            k = 4
            size = len(base) // k
            folds = [base[i * size:(i + 1) * size] for i in range(k)]
            results = [filter_effect(f, dim, value) for f in folds]
            usable = [r for r in results if r and r["skipped"] >= 5]
            if len(usable) < 3:
                return None
            holds = sum(1 for r in usable if r["improvement"] > 0)
            return verdict(holds >= len(usable) - 1 and holds >= 3,
                           {"folds": usable, "improved_in": holds},
                           "the improvement holds in all but at most one chronological fold")
        if stage == "STRESS_TEST":
            base = disc + hold
            rng = random.Random(self.seed + int(h["id"]))
            better = 0
            for _ in range(1000):
                sample = [rng.choice(base) for _ in base]
                eff = filter_effect(sample, dim, value)
                better += 1 if eff and eff["improvement"] > 0 else 0
            ranked = sorted(base, key=lambda r: r["realized_r"])
            trimmed = ranked[:max(1, int(len(ranked) * 0.95))]      # the best 5% removed
            eff_trim = filter_effect(trimmed, dim, value)
            probability = better / 1000
            return verdict(probability >= 0.95 and bool(eff_trim and eff_trim["improvement"] > 0),
                           {"bootstrap_probability_of_improvement": probability,
                            "without_best_5pct": eff_trim},
                           "95%+ of resamples improve, and the effect survives removing the best trades")
        if stage == "FORWARD_PAPER":
            cohort = [r for r in forward if str(r.get(dim)) == value]
            if len(cohort) < MIN_COHORT:
                return None                          # forward trades accumulate over time
            eff = filter_effect(forward, dim, value)
            return eff and verdict(eff["improvement"] > 0 and eff["cohort_mean_r"] < 0, eff,
                                   "trades closed after the hypothesis was created show the same effect")
        if stage == "STATISTICAL_COMPARISON":
            inside = [r["realized_r"] for r in rows if str(r.get(dim)) == value]
            rest = [r["realized_r"] for r in rows if str(r.get(dim)) != value]
            if len(inside) < MIN_COHORT or len(rest) < MIN_COHORT:
                return None
            test = welch(inside, rest)
            return verdict(test["diff"] < 0 and test["p"] < 0.01, test,
                           "over every recorded trade the cohort is worse than the rest at p < 0.01")
        if stage == "RECOMMENDATION":
            return {"state": "PASS", "at": self._now(), "why": "every evidence stage passed",
                    "evidence": {"recommendation": "APPROVE FOR DEVELOPMENT as a research candidate",
                                 "production": "unchanged"}}
        return None                                  # OWNER_APPROVAL, PRODUCTION_CANDIDATE: owner only

    def advance(self, trades: list[dict]) -> int:
        """Run the next pending stage of every open hypothesis, in order."""
        groups = by_version(trades)
        moved = 0
        for h in self.list():
            if h["status"] in _FINAL or h["stage"] in ("OWNER_APPROVAL", "PRODUCTION_CANDIDATE"):
                continue
            rows = groups.get((h["record_source"], h["strategy_id"], h["strategy_version"]), [])
            while h["stage"] not in ("OWNER_APPROVAL", "PRODUCTION_CANDIDATE"):
                result = self._evaluate(h["stage"], h, rows)
                if result is None:
                    break
                stages = h["stages"]
                stages[h["stage"]] = result
                failed = result["state"] == "FAIL"
                nxt = STAGES[STAGES.index(h["stage"]) + 1]
                status = (REJECTED_EVIDENCE if failed else RECOMMENDED if h["stage"] == "RECOMMENDATION"
                          else TESTING)
                with self.store._lock:
                    self.store._c.execute(
                        "UPDATE guardian_hypotheses SET stages=?, stage=?, status=?, updated_at=? WHERE id=?",
                        (json.dumps(stages, default=str), h["stage"] if failed else nxt, status, self._now(), h["id"]))
                    self._log(h["id"], "guardian", f"{h['stage']} {result['state']}", result.get("why", ""),
                              result.get("evidence"))
                    self.store._c.commit()
                moved += 1
                if failed:
                    break
                h = self.get(h["id"])
        return moved

    def cycle(self) -> dict:
        trades = closed_trades(self.journal_path)
        created = self.discover(trades)
        moved = self.advance(trades)
        return {"trades": len(trades), "created": created, "advanced": moved}

    def view(self) -> dict:
        """What /guardian/research shows."""
        return {"hypotheses": self.list(), "stages": list(STAGES), "method": METHOD_NOTE,
                "last_run": self.store.meta("research.last_run"), "owner_actions": list(self.OWNER_ACTIONS)}

    def analyst(self) -> list[dict]:
        out = []
        for (source, strategy, version), rows in by_version(closed_trades(self.journal_path)).items():
            out.append({"record_source": source, "strategy_id": strategy or None,
                        "strategy_version": version or None, **analyse(rows)})
        return out

    # ------------------------------------------------------ owner controls
    OWNER_ACTIONS = ("REVIEW", "REJECT", "SEND_TO_BACKTEST", "SEND_TO_FORWARD_PAPER", "APPROVE_FOR_DEVELOPMENT")

    def owner_action(self, hid: int, action: str, *, note: str = "") -> dict:
        """PRD §38. None of these touches a strategy: they change this
        hypothesis's standing in Guardian's research store, and each is
        logged append-only."""
        action = str(action or "").upper()
        if action not in self.OWNER_ACTIONS:
            raise ValueError(f"unknown owner action {action!r}")
        h = self.get(hid)
        if h is None:
            raise KeyError(hid)
        if h["status"] in _FINAL:
            raise ValueError(f"hypothesis is {h['status']}; it is kept as history and cannot change")
        fields: dict[str, Any] = {}
        if action == "REJECT":
            fields["status"] = REJECTED_OWNER
        elif action == "SEND_TO_BACKTEST":
            if h["stage"] != "HISTORICAL_BACKTEST":
                raise ValueError("already past the historical stage")
        elif action == "SEND_TO_FORWARD_PAPER":
            if STAGES.index(h["stage"]) < STAGES.index("FORWARD_PAPER"):
                raise ValueError("every earlier stage must pass first; no stage may be skipped")
        elif action == "APPROVE_FOR_DEVELOPMENT":
            if h["stage"] != "OWNER_APPROVAL" or h["status"] != RECOMMENDED:
                raise ValueError("only a hypothesis Guardian recommends after every stage can be approved")
            stages = h["stages"]
            stages["OWNER_APPROVAL"] = {"state": "PASS", "at": self._now(), "why": "approved by the owner",
                                        "evidence": {"note": note}}
            stages["PRODUCTION_CANDIDATE"] = {
                "state": "PASS", "at": self._now(),
                "why": "approved for development; production code is unchanged until a person implements, "
                       "tests and deploys it"}
            fields.update(status=APPROVED, stage="PRODUCTION_CANDIDATE", stages=json.dumps(stages))
        notes = h["owner_notes"] + [{"at": self._now(), "action": action, "note": note}]
        fields.update(owner_notes=json.dumps(notes), updated_at=self._now())
        with self.store._lock:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.store._c.execute(f"UPDATE guardian_hypotheses SET {sets} WHERE id=?", (*fields.values(), int(hid)))
            self._log(int(hid), "owner", action, note or action)
            self.store._c.commit()
        return self.get(hid)
