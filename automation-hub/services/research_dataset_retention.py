"""Bounded content-addressed input snapshots in the EXISTING research journal.

Retained input is not a completed validation result or evidence of profitability.
No provider re-fetch is described as reconstruction of an unavailable revision.
"""
from datetime import datetime,timezone
import hashlib
import json

from bot.types import Bar
from data.historical import _canonical_candle
from services.shadow_router import utc
from services.strategy_identity import canonical_configuration_json as encoded


class ResearchDatasetRetention:
    max_payload_bytes=4*1024*1024
    max_total_bytes=64*1024*1024
    max_owner_bytes=32*1024*1024
    max_owner_references=1000
    max_candles=15000

    def __init__(self,context): self.context=context

    def retain(self,job,manifest,bars):
        if not bars or len(bars)>self.max_candles: raise ValueError("DATASET_RETENTION_LIMIT")
        canonical=[_canonical_candle(b,manifest["timeframe"]) for b in bars]
        if any(a["timestamp"]>b["timestamp"] for a,b in zip(canonical,canonical[1:])): raise ValueError("INVALID_CANDLE_ORDER")
        # Preserve exact chronological simulator input, including warm-up and
        # any identical duplicates; never sort/drop input after execution.
        payload=json.dumps(canonical,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False)
        content=hashlib.sha256(payload.encode()).hexdigest()
        sid="retained-input:"+content
        size=len(payload.encode())
        if size>self.max_payload_bytes: raise ValueError("DATASET_RETENTION_LIMIT")
        clean={k:v for k,v in manifest.items() if k!="retention"}
        with self.context.journal.transaction():
            db=self.context.journal._c
            current=self.context.get_research_job(job["job_id"],owner_id=job["owner_id"])
            if current is None: raise KeyError(job["job_id"])
            old=db.execute("SELECT * FROM retained_research_dataset_refs WHERE job_id=? AND owner_id=?",(job["job_id"],job["owner_id"])).fetchone()
            if old:
                if old["snapshot_id"]!=sid or old["manifest_json"]!=encoded(clean): raise ValueError("DATASET_RETENTION_CONFLICT")
                retained_at=old["retained_at"]
            else:
                total=db.execute("SELECT COALESCE(SUM(payload_bytes),0) FROM retained_research_datasets").fetchone()[0]
                count=db.execute("SELECT COUNT(*),COALESCE(SUM(d.payload_bytes),0) FROM retained_research_dataset_refs r JOIN retained_research_datasets d ON d.snapshot_id=r.snapshot_id WHERE r.owner_id=?",(job["owner_id"],)).fetchone()
                exists=db.execute("SELECT candles_json FROM retained_research_datasets WHERE snapshot_id=?",(sid,)).fetchone()
                if count[0]>=self.max_owner_references or count[1]+size>self.max_owner_bytes or total+(0 if exists else size)>self.max_total_bytes: raise ValueError("DATASET_RETENTION_LIMIT")
                if exists and exists[0]!=payload: raise ValueError("DATASET_RETENTION_CONFLICT")
                if not exists: db.execute("INSERT INTO retained_research_datasets VALUES (?,?,?,?,?)",(sid,content,payload,size,len(canonical)))
                retained_at=datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO retained_research_dataset_refs VALUES (?,?,?,?,?)",(job["job_id"],job["owner_id"],sid,encoded(clean),retained_at))
        return {"snapshot_id":sid,"content_hash":content,"status":"RETAINED","reconstruction_status":"RECONSTRUCTABLE","retained_at":retained_at,"input_candle_count":len(canonical),"scope":"EXACT_RESEARCH_INPUT_NOT_FORWARD_EVIDENCE"}

    def load(self,job_id,*,owner_id):
        with self.context.journal._lock:
            row=self.context.journal._c.execute("SELECT d.*,r.manifest_json,r.retained_at FROM retained_research_dataset_refs r JOIN retained_research_datasets d ON d.snapshot_id=r.snapshot_id WHERE r.job_id=? AND r.owner_id=?",(job_id,owner_id)).fetchone()
        if row is None: return None
        if hashlib.sha256(row["candles_json"].encode()).hexdigest()!=row["content_hash"] or row["snapshot_id"]!="retained-input:"+row["content_hash"]: raise ValueError("DATASET_REVISION_MISMATCH")
        material=json.loads(row["candles_json"])
        if len(material)!=row["candle_count"] or len(material)>self.max_candles: raise ValueError("DATASET_REVISION_MISMATCH")
        bars=[Bar(utc(r["timestamp"]),*(r[k] for k in ("open","high","low","close","volume"))) for r in material]
        return {"bars":bars,"manifest":json.loads(row["manifest_json"]),"snapshot_id":row["snapshot_id"],"content_hash":row["content_hash"],"status":"HASH_VERIFIED","reconstruction_status":"RECONSTRUCTABLE","retained_at":row["retained_at"]}
