"""Observation-only strategy evidence and flat-to-flat position lineage.

The observer has a journal store only: no execution or ledger mutation methods.
One completed statistical trade is one fully closed position episode. A partial
reduction closes a ledger leg and creates a remainder leg inside that episode.
An explicit scale-in contributes its own immutable entry-risk receipt; scale-out
never changes the original risk. A reversal closes one episode and opens another.
Missing historical entry/lineage is captured as unknown rather than reconstructed
from current configuration or coincident symbol names.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from data.journal_store import JournalStore


SCOPE_FIELDS = ("strategy_id", "strategy_version", "strategy_config_hash", "instance_id",
                "simulation_session_id", "execution_mode", "owner_id", "account_id",
                "lab_id", "source_kind", "symbol")
CORRELATION_FIELDS = ("signal_id", "decision_id", "order_id", "trade_id", "position_id", "episode_id")


def decimal_text(value: Any) -> str | None:
    """Keep authoritative input precision; never round or introduce float sums."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid financial amount") from exc
    if not amount.is_finite():
        raise ValueError("non-finite financial amount")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if amount == 0 else text


def evidence_json(value: Any) -> str:
    def default(item):
        if isinstance(item, Decimal):
            return decimal_text(item)
        if is_dataclass(item):
            return asdict(item)
        raise TypeError(f"unsupported evidence type: {type(item).__name__}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=default)


def scope_from(context: Mapping[str, Any]) -> dict:
    scope = {key: context.get(key) for key in SCOPE_FIELDS}
    scope["strategy_config_hash"] = (context.get("strategy_config_hash")
                                      or context.get("config_fingerprint"))
    return scope


def _mapping(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return vars(value).copy()


def build_episode(header: dict, legs: list[dict] | None = None,
                  events: list[dict] | None = None) -> dict:
    """Pure projection shared by live reads and strictly read-only rebuilds.

    Event order must be the persisted capture order. ``legs`` is accepted for
    read-only adapters; the actual execution receipts determine economics.
    """
    from decimal import localcontext
    episode = dict(header)
    open_legs, trade_ids = set(), set()
    totals = {"net_pnl": Decimal(0), "gross_pnl": Decimal(0), "fees": Decimal(0), "funding": Decimal(0)}
    known = {key: True for key in totals}
    risk_known, risk, closes, closed_at = True, Decimal(0), 0, None
    funding_coverage, fees_coverage = set(), set()
    identity_states = set()
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        for event in events or []:
            facts = event.get("payload")
            if facts is None:
                facts = json.loads(event["payload_json"])
            action, trade_id = facts["action"], event["trade_id"]
            trade_ids.add(trade_id)
            if action in ("opened", "recovered"):
                identity_states.add(facts.get("identity_status") or "unknown")
                open_legs.add(trade_id)
                amount = facts.get("initial_risk")
                if amount is None or Decimal(amount) <= 0:
                    risk_known = False
                else:
                    risk += Decimal(amount)
                continue
            closes += 1
            closed_at = event.get("observed_at")
            open_legs.discard(trade_id)
            if action == "reduced":
                remainder = facts.get("remainder_trade_id")
                if remainder:
                    open_legs.add(remainder); trade_ids.add(remainder)
            for key in totals:
                amount = facts.get(key)
                if amount is None:
                    known[key] = False
                else:
                    totals[key] += Decimal(amount)
            fees_coverage.add(facts.get("fees_coverage", "UNKNOWN"))
            funding_coverage.add(facts.get("funding_coverage", "UNKNOWN"))
    episode.pop("metadata_json", None)
    episode.update(
        status="closed" if closes and not open_legs else "open",
        closed_at=closed_at if closes and not open_legs else None,
        root_initial_risk=episode.get("initial_risk_amount_text"),
        initial_risk=decimal_text(risk) if risk_known else None,
        initial_risk_amount=decimal_text(risk) if risk_known else None,
        config_fingerprint=episode.get("strategy_config_hash"),
        identity_status=(next(iter(identity_states)) if len(identity_states) == 1 else "mixed_or_unknown"),
        evidence_kind="executed", realised_leg_count=closes,
        trade_ids=sorted(trade_ids), open_trade_ids=sorted(open_legs),
        fees_coverage=next(iter(fees_coverage)) if len(fees_coverage) == 1 else "UNKNOWN",
        funding_coverage=next(iter(funding_coverage)) if len(funding_coverage) == 1 else "UNKNOWN",
        **{key: decimal_text(value) if known[key] else None for key, value in totals.items()})
    return episode


class StrategyEvidence:
    """Publish already committed FillResult receipts to the decision journal."""

    def __init__(self, store: JournalStore):
        self.store = store

    def capture_event(self, event_id: str, *, kind: str, payload: dict, **context) -> bool:
        fields = (*SCOPE_FIELDS, *CORRELATION_FIELDS, "observed_at")
        scope = {key: context.get(key) for key in fields}
        scope["strategy_config_hash"] = (context.get("strategy_config_hash")
                                          or context.get("config_fingerprint"))
        return self.store.record_evidence_event(event_id, kind=kind, payload=payload, **scope)

    def observe_fill(self, fill, context: Mapping[str, Any]) -> bool:
        """Return False on an exact durable retry; reject conflicting receipts.

        Transactions cover event + episode links together. Producer failures may
        be caught by its observational callback; this method never places orders.
        A recovery receipt is labeled honestly and retains persisted entry time.
        """
        facts = _mapping(fill)
        action = facts.get("action")
        if action not in ("opened", "recovered", "reduced", "closed"):
            return False
        execution_id = str(facts.get("execution_id") or "")
        if not execution_id:
            raise ValueError("durable execution_id required for evidence")
        receipt = dict(facts.get("receipt") or {})
        frozen = (receipt.get("sizing_context") or {}).get("evidence_context") or {}
        ctx = {**context, **frozen}
        identity = ctx.get("strategy_identity") or ctx.get("journal_strategy_identity") or {}
        ctx = {**identity, **ctx}
        provenance = ctx.get("journal_execution") or {}
        ctx = {**ctx, **{key: value for key, value in provenance.items() if value is not None}}
        for key in SCOPE_FIELDS:
            if receipt.get(key) is not None:
                ctx[key] = receipt[key]
        scope = scope_from({**ctx, "symbol": facts.get("symbol") or ctx.get("symbol")})
        trade_id = str(facts.get("trade_id") or receipt.get("trade_id") or "")
        position_id = str(facts.get("position_id") or receipt.get("position_id") or "")
        if not trade_id or not position_id:
            raise ValueError("actual trade_id and position_id required for evidence")
        scope_key = hashlib.sha256(evidence_json([
            scope.get("owner_id"), scope.get("account_id"), scope.get("instance_id"),
            scope.get("simulation_session_id")]).encode()).hexdigest()
        event_id = "fill:" + scope_key + ":" + execution_id
        previous = self.store.evidence_events(event_id=event_id)
        if action == "recovered" and previous:
            old = previous[0]
            if (old.get("trade_id"), old.get("position_id")) != (trade_id, position_id):
                raise ValueError("recovered execution receipt conflict")
            if any(old.get(key) != scope.get(key) for key in SCOPE_FIELDS):
                raise ValueError("recovered execution scope conflict")
            if any(decimal_text(facts.get(key)) != old["payload"].get(key) for key in ("price", "size")):
                raise ValueError("recovered execution fill conflict")
            # The original capture is authoritative, including its fill time.
            # Recovery only lets the orchestrator repair missing journal prose.
            return False
        # The same scope/trade is checked below too. Avoid time-dependent derived
        # status on retries: it can have changed after subsequent fills.
        opened = action in ("opened", "recovered")
        leg = self.store.episode_for_trade(trade_id, instance_id=scope.get("instance_id"),
                                          simulation_session_id=scope.get("simulation_session_id"),
                                          owner_id=scope.get("owner_id"), account_id=scope.get("account_id"))
        if not opened and not leg:
            # A receipt with a position belonging to another account cannot be
            # attached by symbol or by a coincident trade ID.
            try:
                other_leg = self.store.episode_for_trade(trade_id)
            except ValueError:
                other_leg = None
            if other_leg and other_leg.get("position_id") == position_id:
                raise ValueError("execution receipt conflicts with episode scope")
        episode_id = ctx.get("episode_id") if opened else (leg or {}).get("episode_id")
        if opened and not episode_id:
            digest = hashlib.sha256(evidence_json([scope, trade_id, position_id]).encode()).hexdigest()
            episode_id = "episode:" + digest
        if previous:
            episode_id = previous[0].get("episode_id")
        if leg:
            if not previous:
                # Another journal connection may have committed this exact
                # receipt after our first read. Durable insert compares facts.
                previous = self.store.evidence_events(event_id=event_id)
            episode = self.store.get_episode(leg["episode_id"])
            if any(episode.get(key) != scope.get(key) for key in SCOPE_FIELDS):
                raise ValueError("execution receipt conflicts with episode scope")
            if not previous:
                if opened:
                    raise ValueError("episode leg already opened by another execution")
                if trade_id not in episode["open_trade_ids"]:
                    raise ValueError("episode leg already closed by another execution")
        existing_episode = self.store.get_episode(episode_id) if episode_id else None
        if opened and existing_episode:
            if any(existing_episode.get(key) != scope.get(key) for key in SCOPE_FIELDS):
                raise ValueError("scale-in episode scope conflict")
            if existing_episode["status"] == "closed" and not previous:
                raise ValueError("cannot attach entry to a completed episode")

        initial_risk = receipt.get("initial_risk_amount", receipt.get("risk_amount_at_entry"))
        if initial_risk is None:
            initial_risk = ctx.get("risk_amount_at_entry", ctx.get("initial_risk_amount"))
        # Receipt risk is the actual fill risk. A legacy open with neither a
        # captured receipt nor entry-time risk remains unverified.
        initial_risk = decimal_text(initial_risk) if opened else None
        fees = receipt.get("booked_fees", facts.get("fee"))
        funding = receipt.get("funding") if "funding" in receipt else ctx.get("funding")
        funding_coverage = str(receipt.get("funding_coverage") or ctx.get("funding_coverage") or "UNKNOWN").upper()
        funding_coverage = {"NOT_MODELED": "UNMODELED"}.get(funding_coverage, funding_coverage)
        net = receipt.get("net_pnl", facts.get("pnl")) if not opened else None
        gross = receipt.get("gross_pnl") if not opened else None
        fees = decimal_text(fees) if not opened else None
        funding = decimal_text(funding) if not opened else None
        net = decimal_text(net)
        if (gross is None and net is not None and fees is not None and funding is not None
                and funding_coverage in ("BOOKED", "MODELED", "VERIFIED_ZERO", "UNMODELED")):
            gross = Decimal(net) + Decimal(fees) + Decimal(funding)
        payload = {
            "action": action, "execution_id": execution_id, "side": facts.get("side"),
            "price": decimal_text(facts.get("price")), "size": decimal_text(facts.get("size")),
            "entry": decimal_text(receipt.get("entry")), "stop": decimal_text(receipt.get("stop", ctx.get("stop"))),
            "initial_risk": initial_risk, "net_pnl": net, "gross_pnl": decimal_text(gross),
            "fees": fees, "funding": funding, "fees_coverage": "BOOKED" if fees is not None else "UNKNOWN",
            "funding_coverage": funding_coverage,
            "parent_trade_id": facts.get("parent_trade_id") or None,
            "remainder_trade_id": facts.get("remainder_trade_id") or None,
            "remainder_position_id": facts.get("remainder_position_id") or None,
            "lineage_status": "captured" if opened or leg else "unknown",
            "recovery": action == "recovered", "receipt": receipt,
            "identity_status": ctx.get("identity_status"),
        }
        fields = {**scope, "signal_id": ctx.get("signal_id") or ctx.get("alert_id"),
                  "decision_id": ctx.get("decision_id") or ctx.get("journal_decision_id"),
                  "order_id": ctx.get("order_id"),
                  "trade_id": trade_id, "position_id": position_id, "episode_id": episode_id,
                  "observed_at": facts.get("executed_at") or receipt.get("executed_at")}
        links = []
        if opened:
            links.append({"trade_id": trade_id, "position_id": position_id,
                          "parent_trade_id": None, "initial_risk": initial_risk, "is_entry": True})
        elif leg and action == "reduced":
            remainder_trade = facts.get("remainder_trade_id")
            remainder_position = facts.get("remainder_position_id")
            if not remainder_trade or not remainder_position:
                raise ValueError("reduction requires actual remainder IDs")
            links.append({"trade_id": remainder_trade, "position_id": remainder_position,
                          "parent_trade_id": trade_id, "initial_risk": None, "is_entry": False})
        identity = ctx.get("strategy_identity") or ctx.get("journal_strategy_identity") or ctx
        if identity.get("configuration") is not None and scope.get("strategy_config_hash"):
            self.store.save_strategy_identity(identity)
        return self.store.record_execution_evidence(event_id, payload=payload, scope=fields,
                                                    links=links, create_episode=opened)
