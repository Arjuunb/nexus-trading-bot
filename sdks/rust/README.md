# tradelogx-nexus (Rust)

A blocking client for the TradeLogX Nexus public API (`/v1`), on `ureq`.

```toml
[dependencies]
tradelogx-nexus = { git = "https://github.com/Arjuunb/nexus-trading-bot" }
```

```rust
let client = tradelogx_nexus::Client::from_env()?; // NEXUS_API_KEY, optional NEXUS_API_BASE
for d in client.decisions(DecisionQuery { verdict: Some("rejected".into()), limit: Some(20), ..Default::default() }) {
    let d = d?;
    println!("{} {:?} {:?}", d.symbol, d.quality_score, d.blocked_by);
}
```

Errors are `tradelogx_nexus::Error { status, code, message }`.
