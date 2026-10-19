#!/usr/bin/env python3
"""Explicitly authorized, finite public-only soak; never started by Compose."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation-hub'))


def main(argv=None):
    p=argparse.ArgumentParser(description='Plan by default; public network requires a separate explicit authorization')
    p.add_argument('--output',required=True);p.add_argument('--seconds',type=int,default=120)
    p.add_argument('--symbol',choices=('XRPUSDT','BTCUSDT','ETHUSDT'),default='XRPUSDT')
    p.add_argument('--authorize-public-feed',action='store_true')
    args=p.parse_args(argv)
    if not 1<=args.seconds<=300:raise ValueError('SOAK_DURATION_LIMIT')
    out=Path(args.output)
    if out.exists():raise ValueError('NEW_OWNED_SOAK_DIRECTORY_REQUIRED')
    if not args.authorize_public_feed:
        print(json.dumps({'status':'NOT_RUN','reason':'EXPLICIT_PUBLIC_NETWORK_AUTHORIZATION_REQUIRED',
            'maximum_seconds':args.seconds,'venue':'binance_usdm','market_type':'perpetual','execution_authorization':False}));return 0
    from services.research_worker_main import FORBIDDEN_CREDENTIALS
    import os
    if any(os.environ.get(k) for k in FORBIDDEN_CREDENTIALS):raise ValueError('FORBIDDEN_EXECUTION_CREDENTIAL')
    from data.market_data_v2 import MarketDataService
    from services.forward_paper_hub import ForwardPaperMarketDataHub
    from services.market_data_distribution import CandleJournal,JournalFeedPublisher,DistributedMarketDataHub
    from bot.data.resample import TF_SECONDS
    out.mkdir(parents=True,mode=0o700)
    journal=CandleJournal(out/'candles.sqlite',revision='binance-usdm-finalized.v1')
    publisher=JournalFeedPublisher(journal);public=MarketDataService(out/'public-cache')
    hub=ForwardPaperMarketDataHub(public.public_usdm_window);stop=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:stop.set());signal.signal(signal.SIGTERM,lambda *_:stop.set())
    readers=[DistributedMarketDataHub(journal.path,out/f'consumer-{n}.sqlite',revision=journal.revision) for n in range(2)]
    subs=[];probes=[];start=datetime.now(timezone.utc).isoformat();deadline=time.monotonic()+args.seconds
    result={'schema_version':'distributed-public-soak.v1','origin':'AUTHORIZED_PUBLIC_LIVE','status':'INCOMPLETE',
        'venue':'binance_usdm','market_type':'perpetual','symbol':args.symbol,'start':start,'execution_authorization':False,
        'consumer_process_boundary':'TWO_READERS_SAME_PROCESS; INDEPENDENT_PROCESSES_REQUIRE_SEPARATE_WORKERS',
        'exchange_to_system_latency':'UNKNOWN; LOCAL_RECEIPT_ONLY'}
    try:
        for tf in ('5m','15m','1h','4h'):
            original=publisher.attach(hub,symbol=args.symbol,timeframe=tf,authorized_public=True)
            # Bootstrap is context only. Never relabel it as live observation.
            received=datetime.now(timezone.utc)
            for bar in original.snapshot()['closed_bars']:
                if bar.timestamp.timestamp()+TF_SECONDS[tf]<=received.timestamp():
                    journal.append({'bar':bar,'symbol':args.symbol,'timeframe':tf,'venue':'binance_usdm','market_type':'perpetual',
                        'source':'BINANCE_USDM_PUBLIC','origin':'REST_BOOTSTRAP','finalized':True,'delivery_observed_at':received.isoformat()})
            pair=[]
            for n,reader in enumerate(readers):
                sub=reader.subscription(f'soak-{n}-{tf}');sub.start(args.symbol,tf,quotes=False);pair.append(sub)
            subs.append(pair)
        while time.monotonic()<deadline and not stop.is_set():
            publisher.drain()
            readings=[[s.snapshot()['connection'] for s in pair] for pair in subs]
            probes.append({'at':datetime.now(timezone.utc).isoformat(),'channels':hub.channel_report(),'consumer_readings':readings,
                'same_canonical_hash':all(pair[0].get('candle_hash')==pair[1].get('candle_hash') for pair in readings)})
            stop.wait(min(.5,max(0,deadline-time.monotonic())))
        result['status']='CANCELLED' if stop.is_set() else 'OBSERVED_BOUNDED; NOT_SUSTAINED_CERTIFICATION'
    except Exception as exc:
        result['status']='BLOCKED';result['error_category']=type(exc).__name__
    finally:
        publisher.stop();hub.stop()
        result.update(end=datetime.now(timezone.utc).isoformat(),journal=journal.health(),publisher=publisher.health(),probes=probes)
        (out/'soak.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({'status':result['status'],'report':'soak.json','execution_authorization':False}))
    return 2 if result['status']=='BLOCKED' else 0


if __name__=='__main__':raise SystemExit(main())
