# tradelogx-nexus (Python)

A standard-library-only client for the TradeLogX Nexus public API (`/v1`).

```sh
pip install "git+https://github.com/Arjuunb/nexus-trading-bot#subdirectory=sdks/python"
```

```python
from tradelogx_nexus import Client

client = Client()  # NEXUS_API_KEY, and optionally NEXUS_API_BASE
for d in client.decisions.list(verdict="rejected", limit=20):
    print(d["symbol"], d["quality_score"], d["blocked_by"], d["reason"])

job = client.backtests.run("decision_brain", symbol="BTCUSDT", timeframe="15m")
print(job["result"]["gross"]["net_r"], job["result"]["net"]["net_r"])
```

Create a key in the dashboard under Settings → Security → API keys.
Errors raise `NexusError` with `.status`, `.code` and `.message`.
