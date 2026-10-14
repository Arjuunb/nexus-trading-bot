"""Observer callbacks preserve existing delivery and financial sink semantics."""
from datetime import datetime,timezone
import pytest
from services.price_action_stream import PriceActionPublicStream
from test_shadow_router import frame


def test_finalized_delivery_preserves_origin_and_original_sink_result():
    bar=frame()['context']['5m'][-1];seen=[];original=[]
    stream=PriceActionPublicStream(lambda *a,**k:[],bar_sink=original.append)
    stream.symbol='XRPUSDT';stream.timeframe='5m';stream.candle_observation_sink=seen.append
    assert stream._deliver_closed_bar(bar,origin='LIVE_WEBSOCKET') is True
    assert original==[bar]
    assert seen[0]['origin']=='LIVE_WEBSOCKET'
    assert seen[0]['bar']==bar and seen[0]['finalized'] is True


def test_rest_repair_cannot_be_presented_as_websocket_delivery():
    bar=frame()['context']['5m'][-1];seen=[]
    stream=PriceActionPublicStream(lambda *a,**k:[])
    stream.symbol='XRPUSDT';stream.timeframe='5m';stream.candle_observation_sink=seen.append
    stream._deliver_closed_bar(bar,origin='REST_REPAIR')
    assert seen[0]['origin']=='REST_REPAIR'


def test_observer_failure_does_not_change_the_original_candle_sink():
    original=[];stream=PriceActionPublicStream(lambda *a,**k:[],bar_sink=original.append)
    def failed(_):raise RuntimeError('observer unavailable')
    stream.candle_observation_sink=failed
    bar=frame()['context']['5m'][-1]
    assert stream._deliver_closed_bar(bar,origin='LIVE_WEBSOCKET') is True
    assert original==[bar]
    assert stream.observation_notice_failures==1
