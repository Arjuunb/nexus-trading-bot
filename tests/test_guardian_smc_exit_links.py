"""Exact retained IDs associate history; they never certify journal exits."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
import subprocess
import sys

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.lab_fill_history import digest
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore

NOW = datetime.now(timezone.utc)
READ_KEY = "independent-exit-links-reader-123456789"
SOURCE_KEY = "independent-exit-links-source-123456789"


@pytest.fixture
def store(tmp_path):
    from tradexa.guardian.smc_exit_links import PROBES
    result = GuardianStore(tmp_path / "guardian.db")
    for probe in PROBES: result.record_heartbeat(probe, "HEALTHY", observed_at=NOW)
    return result


def intent(store, *, key="decision-1", order="entry-1", trade="trade-1", state="COMPLETE",
           origin="a"*64, session="session-1", symbol="BTCUSDT", timeframe="5m"):
    from tradexa.guardian.smc_intent_history import FIELDS, PROBE, COMPONENT, SCOPE
    value = dict.fromkeys(FIELDS)
    value.update(source_sequence=store.count()+1, id=f"intent-event-{store.count()}", execution_key=key,
        broker_order_id=order, trade_id=trade, state=state, created_at=NOW.isoformat(),
        intent_id="intent-1", session_id=session, symbol=symbol, timeframe=timeframe,
        candle_time=NOW.isoformat(), proposal_id=key, error_recorded=False)
    event = GuardianEvent(source_service=PROBE, source_component=COMPONENT,
        event_type="smc_intent_transition_observed", timestamp=NOW,
        event_id=digest(["smc-intent-event-v1", origin, value["id"]]),
        lab_id="SMC", agent_id="smc_agent", execution_id=key, order_id=order, session_id=session,
        symbol=symbol, timeframe=timeframe, state_after=state, reason="RECORDED_EXECUTION_INTENT_EVENT_ONLY",
        evidence={"transition":value, "journal_origin":origin, "broker_execution_verified":False,
                  "journal_trade_verified":False, "position_lifecycle_verified":False},
        metadata={"coverage":SCOPE, "paper_only":True})
    store.append(event)
    return event


def origin_position(*, key="decision-1", position="position-1", order="entry-1", timeframe="5m", size=1):
    return dict(position_id=position, entry_execution_key=key, entry_order_id=order,
                entry_timeframe=timeframe, side="long", size=size, entry_price=100)


def transition(store, *, n=1, before=None, after=None, account="account-1", order="entry-1",
               side="buy", quantity=1, price=100, reduce=False, effect="OPEN", symbol="BTCUSDT"):
    from tradexa.guardian.smc_fill_positions import PROBE, COMPONENT, SCOPE, PAYLOAD_SCOPE
    payload = dict(schema_version=1, scope=PAYLOAD_SCOPE, account_id=account, fill_id=f"fill-{n}",
        order_id=order, symbol=symbol, side=side, quantity=quantity, price=price, reduce_only=reduce,
        persisted_order=not order.startswith("protective-"), before=before, after=after, effect=effect)
    fill = dict(source_sequence=n, fill_id=f"fill-{n}", order_id=order, symbol=symbol, side=side,
        quantity=quantity, price=price, timestamp=NOW.isoformat(), transition=payload,
        capture_state="RECORDED_SOURCE_TRANSITION")
    event = GuardianEvent(source_service=PROBE, source_component=COMPONENT, event_type="smc_fill_position_observed",
        timestamp=NOW, event_id=digest(["smc-fill-position-event-v1", account, fill["fill_id"]]),
        lab_id="SMC", symbol=symbol, order_id=order, reason="RECORDED_PAPER_POSITION_TRANSITION_ONLY",
        evidence={"account_id":account, "account_type":"SMC_LAB", "fill":fill},
        metadata={"coverage":SCOPE, "paper_only":True, "full_lifecycle_verified":False})
    store.append(event)
    return event


def exit_fact(store, *, n=2, position=None, account="account-1", price=90, quantity=1,
              order="protective-2", symbol="BTCUSDT", kind="POSITION_STOP_LOSS", capture=True):
    from tradexa.guardian.smc_exit_fills import PROBE, COMPONENT, SCOPE, PAYLOAD_SCOPE
    pos = position or origin_position()
    payload = dict(schema_version=1, scope=PAYLOAD_SCOPE, account_id=account, fill_id=f"fill-{n}",
        order_id=order, symbol=symbol, side="sell", quantity=quantity, closed_quantity=min(quantity,pos["size"]),
        price=price, raw_reference_price=price, reduce_only=kind!="NETTING_FILL",
        persisted_order=not order.startswith("protective-"), position=pos,
        protection=dict(stop_loss=90,take_profit=120,trailing_offset=None,peak_price=None),
        order=dict(type="market",limit_price=None,stop_price=None,trailing_offset=None),
        trigger_kind=kind,trigger_price=90 if kind=="POSITION_STOP_LOSS" else None,
        effective_stop=90,effective_peak=None,fill_source="CANDLE",
        observation=dict(timestamp=None,quote_event_id=None,open=100,high=101,low=80,close=90,bid=None,ask=None))
    fill = dict(source_sequence=n,fill_id=f"fill-{n}",order_id=order,symbol=symbol,side="sell",
        quantity=quantity,price=price,timestamp=NOW.isoformat(),exit_evidence=payload if capture else None,
        capture_state="RECORDED_SOURCE_EXIT" if capture else "UNVERIFIED_NO_EXIT_CAPTURE")
    event = GuardianEvent(source_service=PROBE,source_component=COMPONENT,event_type="smc_exit_evidence_observed",
        timestamp=NOW,event_id=digest(["smc-exit-fill-event-v1",account,fill["fill_id"]]),lab_id="SMC",
        order_id=order,symbol=symbol,reason="RECORDED_PAPER_EXIT_EVIDENCE_ONLY",
        evidence={"account_id":account,"account_type":"SMC_LAB","fill":fill},
        metadata={"coverage":SCOPE,"paper_only":True,"full_lifecycle_verified":False})
    store.append(event)
    return event


def journal_close(store, *, trade="trade-1", order="entry-1", symbol="BTCUSDT", timeframe="5m",
                  direction="long", origin="c"*64):
    from tradexa.guardian.smc_journal_history import FIELDS, PROBE, COMPONENT, SCOPE, project_trade
    value = dict.fromkeys(FIELDS)
    value.update(source_sequence=store.count()+1,id=trade,decision_id="journal-decision-not-execution-key",
        order_id=order,symbol=symbol,timeframe=timeframe,direction=direction,entry=100,stop=90,target=120,
        planned_rr=2,size=1,size_capped=0,opened_at=(NOW-timedelta(seconds=5)).isoformat(),
        closed_at=NOW.isoformat(),exit_price=90,realised_r=-1,result="LOSS",close_reason="recorded close")
    value = project_trade(value)
    event = GuardianEvent(source_service=PROBE,source_component=COMPONENT,event_type="smc_closed_journal_observed",
        timestamp=NOW,event_id=digest(["smc-closed-journal-v1",origin,trade]),lab_id="SMC",agent_id="smc_agent",
        order_id=order,symbol=symbol,timeframe=timeframe,correlation_id=value["decision_id"],
        state_after="JOURNAL_CLOSED",reason="RECORDED_JOURNAL_CLOSE_ONLY",
        evidence={"trade":value,"journal_origin":origin,"broker_execution_verified":False,
            "position_lifecycle_verified":False,"net_pnl_verified":False,"currency_verified":False},
        metadata={"coverage":SCOPE,"paper_only":True})
    store.append(event)
    return event


def complete(store, *, with_journal=True):
    intent(store)
    pos = origin_position()
    transition(store, after=pos)
    transition(store,n=2,before=pos,order="protective-2",side="sell",price=90,reduce=True,effect="CLOSE")
    exit_fact(store,position=pos)
    if with_journal: journal_close(store)


def view(store, key="decision-1"):
    from tradexa.guardian.smc_exit_links import smc_exit_links_view
    return smc_exit_links_view(store,key,now=NOW)


def request(store, *, query="execution_key=decision-1", key=READ_KEY, method="GET"):
    app = getattr(store,"_fixture_app",None)
    if app is None:
        app=GuardianService(store,source_keys={"guardian_probe":SOURCE_KEY},read_key=READ_KEY,
                            required_components=("guardian",))
        store._fixture_app=app
    result={}
    def respond(status,headers): result.update(status=int(status.split()[0]),headers=dict(headers))
    raw=b"".join(app(dict(REQUEST_METHOD=method,PATH_INFO="/v1/smc-exit-links",QUERY_STRING=query,
        HTTP_X_GUARDIAN_KEY=key,CONTENT_LENGTH="0",**{"wsgi.input":io.BytesIO()}),respond))
    return result["status"],json.loads(raw),result["headers"]


def test_exact_exit_position_and_journal_entry_ids_never_certify_a_journal_exit(store):
    complete(store)
    result=view(store)
    assert result["link_state"]=="EXPLICIT_EXIT_POSITION_LINKS_OBSERVED"
    assert result["observed_exit_fill_ids"]==["fill-2"]
    assert result["origin_position_ids"]==["position-1"] and result["paper_account_ids"]==["account-1"]
    assert result["recorded_order_ids"]==["entry-1"] and result["recorded_trade_ids"]==["trade-1"]
    assert result["exit_links"][0]["link_state"]=="EXPLICIT_ACCOUNT_POSITION_FILL_IDS_OBSERVED"
    assert result["closed_journal_links"][0]["link_state"]=="EXPLICIT_ENTRY_ORDER_TRADE_IDS_OBSERVED"
    assert "JOURNAL_EXIT_IDS_NOT_CAPTURED" in result["findings"]
    assert all(result[k] is False for k in result if k.endswith("_verified"))
    assert result["guardian_snapshot_atomic"] is True and result["cross_database_atomic"] is False


@pytest.mark.parametrize("missing",["intent","entry","transition","exit","journal"])
def test_missing_evidence_is_not_reconstructed_or_no_order(store,missing):
    if missing!="intent": intent(store)
    if missing!="entry": transition(store,after=origin_position())
    if missing!="transition": transition(store,n=2,before=origin_position(),order="protective-2",side="sell",price=90,reduce=True,effect="CLOSE")
    if missing!="exit": exit_fact(store)
    if missing!="journal": journal_close(store)
    result=view(store)
    assert result["link_state"]==("EXPLICIT_EXIT_POSITION_LINKS_OBSERVED" if missing=="journal" else "INSUFFICIENT_EVIDENCE")
    assert result["journal_close_verified"] is False
    assert "MISSED" not in str(result) and "No order was placed" not in str(result)


@pytest.mark.parametrize("damage,expected",[
    ("origin_order","ORIGIN_ORDER_LINK_CONFLICT"),("market","ORIGIN_MARKET_CONFLICT"),
    ("session","INTENT_IDENTITY_CONFLICT"),("intent_origin","MULTIPLE_JOURNAL_ORIGINS"),
    ("trade","MULTIPLE_RECORDED_TRADE_IDS"),("failed","FAILED_INTENT_WITH_RECORDED_FILL"),
    ("exit_price","EXIT_TRANSITION_EVIDENCE_CONFLICT"),("exit_position","EXIT_TRANSITION_EVIDENCE_CONFLICT"),
    ("exit_sequence","EXIT_TRANSITION_EVIDENCE_CONFLICT"),("journal_order","JOURNAL_ORDER_LINK_CONFLICT"),
    ("journal_trade","JOURNAL_TRADE_LINK_CONFLICT"),("journal_market","JOURNAL_MARKET_CONFLICT"),
    ("journal_side","JOURNAL_DIRECTION_CONFLICT"),("account","MULTIPLE_PAPER_ACCOUNTS"),
])
def test_logical_conflicts_never_become_verified_links(store,damage,expected):
    intent(store,order="other-entry" if damage=="origin_order" else "entry-1",
        symbol="ETHUSDT" if damage=="market" else "BTCUSDT",
        state="EXECUTION_FAILED" if damage=="failed" else "COMPLETE")
    if damage=="session": intent(store,session="other-session")
    elif damage=="intent_origin": intent(store,origin="b"*64)
    elif damage=="trade": intent(store,trade="other-trade")
    pos=origin_position()
    transition(store,after=pos)
    transition(store,n=2,before=pos,order="protective-2",side="sell",price=90,reduce=True,effect="CLOSE")
    altered=deepcopy(pos)
    if damage=="exit_position": altered["entry_price"]=101
    event=exit_fact(store,position=altered,price=89 if damage=="exit_price" else 90)
    if damage=="exit_sequence":
        raw=json.loads(event.canonical_json()); raw["evidence"]["fill"]["source_sequence"]=3
        corrupt(store,event,json.dumps(raw))
    if damage=="account":
        transition(store,n=1,after=pos,account="account-2")
        exit_fact(store,n=2,account="account-2")
    journal_close(store,order="other-entry" if damage=="journal_order" else "entry-1",
        trade="other-trade" if damage=="journal_trade" else "trade-1",
        symbol="ETHUSDT" if damage=="journal_market" else "BTCUSDT",
        direction="short" if damage=="journal_side" else "long")
    result=view(store)
    assert result["link_state"]=="CONFLICTING_EVIDENCE" and expected in result["findings"]
    assert result["observed_exit_fill_ids"]==[] and result["journal_close_verified"] is False


def corrupt(store,event,raw):
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TRIGGER IF EXISTS events_no_update")
        db.execute("UPDATE events SET payload_json=? WHERE event_id=?",(raw,event.event_id)); db.commit()


def test_same_time_price_and_market_never_link_foreign_account_or_position(store):
    complete(store)
    exit_fact(store,n=99,account="foreign-account",position=origin_position(key="other",position="position-1",order="other-entry"))
    exit_fact(store,n=100,position=origin_position(key="other",position="foreign-position",order="other-entry"))
    result=view(store)
    assert result["observed_exit_fill_ids"]==["fill-2"] and len(result["exit_links"])==1


def test_uncertain_execution_remains_uncertain_despite_recorded_exit(store):
    complete(store)
    intent(store,state="EXECUTION_UNCERTAIN")
    result=view(store)
    assert result["latest_recorded_state"]=="EXECUTION_UNCERTAIN"
    assert "EXECUTION_UNCERTAIN_RECORDED" in result["findings"]
    assert result["current_execution_state_verified"] is False


@pytest.mark.parametrize("missing",["key","order","position","timeframe","capture"])
def test_nullable_legacy_origin_or_capture_is_not_invented(store,missing):
    intent(store)
    transition(store,after=origin_position())
    legacy=origin_position()
    if missing!="capture": legacy[{"key":"entry_execution_key","order":"entry_order_id","position":"position_id","timeframe":"entry_timeframe"}[missing]]=None
    transition(store,n=2,before=legacy,order="protective-2",side="sell",price=90,reduce=True,effect="CLOSE")
    exit_fact(store,position=legacy,capture=missing!="capture")
    result=view(store)
    assert result["link_state"]=="INSUFFICIENT_EVIDENCE" and result["observed_exit_fill_ids"]==[]


@pytest.mark.parametrize("probe_index",range(4))
@pytest.mark.parametrize("state",["FAILED","DEGRADED","stale","missing"])
def test_probe_health_masks_current_claims_but_keeps_historical_facts(store,probe_index,state):
    from tradexa.guardian.smc_exit_links import PROBES
    complete(store)
    probe=PROBES[probe_index]
    if state=="missing":
        with closing(sqlite3.connect(store.path)) as db: db.execute("DELETE FROM heartbeats WHERE component=?",(probe,)); db.commit()
    else: store.record_heartbeat(probe,"HEALTHY" if state=="stale" else state,
        observed_at=NOW-timedelta(seconds=100) if state=="stale" else NOW)
    result=view(store)
    if probe_index<3: assert result["link_state"]=="UNKNOWN" and result["observed_exit_fill_ids"]==[]
    else: assert result["closed_journal_links"][0]["observation_state"]=="UNKNOWN"
    assert len(result["exit_links"])==1 and result["journal_close_verified"] is False


def test_partial_exits_and_netting_reversal_keep_original_origin_distinct(store):
    intent(store)
    pos=origin_position()
    remaining=origin_position(size=.6)
    transition(store,after=pos)
    transition(store,n=2,before=pos,after=remaining,order="protective-2",side="sell",quantity=.4,price=90,reduce=True,effect="REDUCE")
    exit_fact(store,position=pos,quantity=.4)
    newpos=origin_position(key="next-decision",position="new-position",order="reverse-order",size=.4)
    newpos["side"]="short"
    transition(store,n=3,before=remaining,after=newpos,order="reverse-order",side="sell",quantity=1,price=90,effect="REVERSE")
    exit_fact(store,n=3,position=remaining,quantity=1,order="reverse-order",kind="NETTING_FILL")
    result=view(store)
    assert result["observed_exit_fill_ids"]==["fill-2","fill-3"]
    assert result["origin_position_ids"]==["position-1"]
    assert [e["exit_evidence"]["closed_quantity"] for e in result["exit_links"]]==pytest.approx([.4,.6])
    assert result["position_lifecycle_verified"] is False


def test_total_row_limit_masks_truncated_links_and_old_keys_use_exact_indexes(store):
    complete(store)
    for n in range(6,180):
        transition(store,n=n,before=origin_position(),order=f"protective-{n}",side="sell",price=90,reduce=True,effect="CLOSE")
    result=view(store)
    assert result["truncated"] is True and result["link_state"]=="UNKNOWN"
    assert result["observed_exit_fill_ids"]==[] and "LINK_EVIDENCE_TRUNCATED" in result["findings"]
    assert result["evidence_rows_loaded"]<=128


@pytest.mark.parametrize("damage",["duplicate_json","secret","identity","oversized","wrong_lab"])
def test_invalid_cache_returns_sanitized_503(store,damage):
    intent(store); transition(store,after=origin_position())
    event=exit_fact(store)
    raw=json.loads(event.canonical_json())
    if damage=="secret": raw["metadata"]["api_key"]="never-expose"
    elif damage=="identity": raw["event_id"]="wrong-event"
    elif damage=="oversized": raw["padding"]="x"*17000
    elif damage=="wrong_lab": raw["lab_id"]="PRICE_ACTION"
    text=json.dumps(raw)
    if damage=="duplicate_json": text='{"lab_id":"PRICE_ACTION",'+text[1:]
    corrupt(store,event,text)
    assert request(store)[:2]==(503,{"error":"EXIT_LINK_EVIDENCE_UNAVAILABLE"})


def test_http_auth_queries_lock_retry_missing_db_and_100_reads_are_read_only(store):
    complete(store)
    for key in ("",SOURCE_KEY): assert request(store,key=key)[0]==401
    for query in ("","execution_key=","execution_key=a/b","execution_key=a&execution_key=b",
                  "execution_key=a&limit=10","execution_key="+"a"*257):
        assert request(store,query=query)[0]==400
    assert request(store,method="POST")[0]==405
    assert request(store)[0]==200 and request(store)[2]["Cache-Control"]=="no-store"
    with closing(sqlite3.connect(store.path)) as db:
        before=list(db.iterdump())
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE heartbeats SET reason='uncommitted'")
        for _ in range(100): assert request(store)[0]==200
        db.rollback()
        assert list(db.iterdump())==before
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        for _ in range(2): assert request(store)[:2]==(503,{"error":"PERSISTENCE_UNAVAILABLE"})
        db.rollback()
    assert request(store)[0]==200
    store.path.unlink()
    assert request(store)[0]==503 and not store.path.exists()


def test_standalone_import_never_loads_source_or_trading_modules():
    result=subprocess.run([sys.executable,"-c","import sys; import tradexa.guardian.smc_exit_links; "
        "assert not any(k.startswith(('execution.', 'services.', 'bot.')) for k in sys.modules)"],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_reversal_exit_is_not_assigned_to_the_new_incoming_execution(store):
    intent(store, key="next-decision", order="reverse-order", trade="next-trade")
    old = origin_position()
    new = origin_position(key="next-decision", position="new-position", order="reverse-order", size=.5)
    new["side"]="short"
    transition(store,n=2,before=old,after=new,order="reverse-order",side="sell",quantity=1.5,price=90,effect="REVERSE")
    exit_fact(store,n=2,position=old,quantity=1.5,order="reverse-order",kind="NETTING_FILL")
    result=view(store,"next-decision")
    assert result["link_state"]=="INSUFFICIENT_EVIDENCE"
    assert result["origin_position_ids"]==["new-position"]
    assert result["observed_exit_fill_ids"]==[] and result["exit_links"]==[]


def test_old_decision_is_not_lost_outside_a_recent_event_window(store):
    complete(store)
    for n in range(10,230):
        transition(store,n=n,account="foreign-account",order=f"entry-{n}",
            after=origin_position(key=f"other-{n}",position=f"position-{n}",order=f"entry-{n}"))
    result=view(store)
    assert result["link_state"]=="EXPLICIT_EXIT_POSITION_LINKS_OBSERVED" and not result["truncated"]
    assert result["evidence_rows_loaded"]==5 and result["observed_exit_fill_ids"]==["fill-2"]


@pytest.mark.parametrize("stream",[0,1,2,3])
@pytest.mark.parametrize("damage",["extra_authority","claimed_verified","wrong_timestamp","wrong_component"])
def test_each_retained_stream_revalidates_its_exact_collector_contract(store,stream,damage):
    events=[intent(store),transition(store,after=origin_position()),exit_fact(store),journal_close(store)]
    event=events[stream]
    raw=json.loads(event.canonical_json())
    if damage=="extra_authority": raw["instance_id"]="a-global-instance"
    elif damage=="claimed_verified": raw["metadata"]["execution_integrity_verified"]=True
    elif damage=="wrong_timestamp": raw["timestamp"]=(NOW-timedelta(seconds=1)).isoformat()
    else: raw["source_component"]="different_component"
    corrupt(store,event,json.dumps(raw))
    assert request(store)[:2]==(503,{"error":"EXIT_LINK_EVIDENCE_UNAVAILABLE"})


def test_read_queries_use_fixed_exact_partial_indexes_not_recent_scans(store,monkeypatch):
    complete(store)
    original=sqlite3.connect
    queries=[]
    def connect(*args,**kwargs):
        db=original(*args,**kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(sqlite3,"connect",connect)
    result=view(store)
    selects=[q for q in queries if "FROM events" in q]
    assert len(selects)>8 and all("INDEXED BY smc_" in q and "LIMIT 129" in q for q in selects)
    assert all("DESC" not in q for q in selects)
    assert result["evidence_rows_loaded"]==5


def test_deduplicated_rows_do_not_consume_total_budget_twice(store):
    complete(store)
    snapshot=store.smc_exit_link_snapshot("decision-1")
    assert snapshot["evidence_rows_loaded"]==5
    assert sum(len(snapshot[k]) for k in ("intent_events","position_events","exit_events","journal_events"))==5


def test_byte_budget_is_shared_by_all_queries(store,monkeypatch):
    import tradexa.guardian.smc_exit_links as module
    complete(store)
    monkeypatch.setattr(module,"MAX_BYTES",3000)
    result=view(store)
    assert result["truncated"] and result["link_state"]=="UNKNOWN" and not result["observed_exit_fill_ids"]


def test_foreign_origin_reusing_original_position_id_is_a_conflict_not_an_exit_link(store):
    complete(store)
    pos=origin_position(key="other-key")
    transition(store,n=3,before=pos,order="protective-3",side="sell",price=90,reduce=True,effect="CLOSE")
    exit_fact(store,n=3,position=pos)
    result=view(store)
    assert result["link_state"]=="CONFLICTING_EVIDENCE"
    assert "POSITION_EXECUTION_KEY_CONFLICT" in result["findings"] and result["observed_exit_fill_ids"]==[]


def test_snapshot_wall_deadline_fails_closed_even_between_small_queries(store,monkeypatch):
    import time
    complete(store)
    calls=iter((0,2))
    monkeypatch.setattr(time,"monotonic",lambda: next(calls,2))
    assert request(store)[:2]==(503,{"error":"PERSISTENCE_UNAVAILABLE"})
