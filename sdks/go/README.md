# nexus (Go)

A standard-library-only client for the TradeLogX Nexus public API (`/v1`).

```sh
go get github.com/Arjuunb/nexus-trading-bot/sdks/go
```

```go
client, err := nexus.New() // NEXUS_API_KEY, and optionally NEXUS_API_BASE
if err != nil { log.Fatal(err) }
it := client.Decisions.List(ctx, &nexus.DecisionQuery{Verdict: "rejected", Limit: 20})
for it.Next() {
    d := it.Value()
    fmt.Println(d.Symbol, d.QualityScore, d.BlockedBy)
}
if err := it.Err(); err != nil { log.Fatal(err) }
```

Errors are `*nexus.Error` with `Status`, `Code` and `Message`.
