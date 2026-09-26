# Contributing

Small and specific beats large and speculative. A failing test that reproduces
a bug is the most useful thing you can send.

## What is where

| Path | What it is |
| --- | --- |
| `automation-hub/` | The platform: FastAPI app, strategies, risk gates, paper execution, backtests, the `/v1` API, webhooks, audit log, key vault, backups |
| `automation-hub-dashboard/` | The operator dashboard (React) |
| `tradexa-landing/` | The public site (React, pre-rendered) |
| `sdks/` | Python, TypeScript, Go and Rust clients for the `/v1` API |
| `bot/`, `trading_bot/`, `tests/` | The original standard-library backtesting engine and its tests |
| `nginx/`, `scripts/`, `compose.yaml` | Deployment |

## Getting set up

```bash
git clone https://github.com/Arjuunb/nexus-trading-bot
cd nexus-trading-bot
python -m pip install -e ".[dev]" -r automation-hub/requirements.txt

python -m pytest -q tests                        # engine
(cd automation-hub && python -m pytest -q)       # platform
(cd automation-hub-dashboard && npm ci && npm run build)
(cd tradexa-landing && npm ci && npm run build)
```

Each SDK has its own tests; the commands are in `.github/workflows/ci.yml`,
which runs all of the above on every push.

## Pull requests

1. Open an issue first for anything larger than a fix; agreeing on the approach
   costs a comment and saves a rewrite.
2. One change per pull request. Formatting changes go in their own commit.
3. Add a test that fails before the change and passes after.
4. CI must be green. Do not skip, disable or loosen a test to get there.
5. Keep secrets out of commits, issues and logs.

Changes that are not accepted:

- enabling live exchange order routing, or weakening the checks that keep it off;
- changing a strategy's decision logic without a backtest and forward test that
  shows the effect, stated as a hypothesis rather than a result;
- relaxing market-data freshness checks to make a warning go away.

Contributions are made under the repository's [MIT licence](LICENSE). There is
no CLA.

Security problems never go in a public issue — see [SECURITY.md](SECURITY.md).
