# Task 6: compatible CCXT and patched Certifi

The release at `4ed5f35d95895f966506654d0957e664e2eaf65e` failed dependency
installation before its image build. Its API lock required both
`ccxt==4.5.85` and `certifi==2026.10.19`, but the published CCXT wheel requires
`certifi==2026.6.17`. The older CA bundle was the critical finding in the
preceding image scan. Restoring that version or overriding package metadata
would not resolve the release requirement.

Regenerate both locks using CCXT 4.5.87, whose published normal requirements
accept `certifi>=2026.6.17`. Retain Certifi 2026.10.19 and the worker's existing
patched pin. CCXT also requires multidict 6.9.1, so that is the only other
package version changed. Build inputs now state the compatible CCXT minimum
and patched Certifi minimum explicitly. Artifact hashes come from the resolver
and official PyPI distributions.

## Verification

- Reproduced the original incompatible constraints with the resolver.
- Regenerated API and uv locks; `uv lock --check` passes.
- Complete API plus CI locks resolve for Linux x86-64 Python 3.11 and 3.12,
  and the Python 3.11 musllinux target.
- Pip's hash-required binary-only musllinux installation dry run resolves all
  75 packages using the supported musllinux 1.1 and 1.2 wheel tags.
- Installed all 75 locked packages into a new local Python 3.12 environment;
  dependency consistency check passes. Imported CCXT 4.5.87, Certifi
  2026.10.19 and multidict 6.9.1; the patched CA bundle loads through SSL.
- Offline Binance, Bybit and Kraken adapter construction/timeframe parsing
  passes without exchange requests or credentials.
- Existing focused release, wheel-platform and exchange tests: 123 passed in
  that freshly installed environment.

Application, strategy, execution, migration and historical financial-freeze
files are unchanged from the preceding candidate. Security thresholds, hash
checks, publication approval and staging isolation are unchanged. These local
checks do not claim a successful new image scan or target acceptance. Fresh
Linux CI, the actual image regression matrix and image security scans remain
required. No VPS deployment, financial write or new execution is performed by
this dependency fix.
