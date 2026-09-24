package nexus

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

var decisions = []map[string]any{
	{"id": "dec_5", "symbol": "BTCUSDT", "verdict": "rejected"},
	{"id": "dec_4", "symbol": "BTCUSDT", "verdict": "rejected"},
	{"id": "dec_3", "symbol": "BTCUSDT", "verdict": "rejected"},
	{"id": "dec_2", "symbol": "BTCUSDT", "verdict": "rejected"},
	{"id": "dec_1", "symbol": "BTCUSDT", "verdict": "rejected"},
}

func fake(t *testing.T, failOnce map[string]bool) (*httptest.Server, *[]http.Header) {
	var headers []http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers = append(headers, r.Header.Clone())
		send := func(code int, v any) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(code)
			json.NewEncoder(w).Encode(v)
		}
		if r.Header.Get("Authorization") != "Bearer nxs_test_key" {
			send(401, map[string]any{"error": map[string]string{"code": "unauthenticated", "message": "no"}})
			return
		}
		if failOnce[r.URL.Path] {
			delete(failOnce, r.URL.Path)
			w.Header().Set("Retry-After", "0")
			send(429, map[string]any{"error": map[string]string{"code": "rate_limited", "message": "slow"}})
			return
		}
		switch r.URL.Path {
		case "/v1/decisions":
			limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
			start := 0
			if c := r.URL.Query().Get("cursor"); c != "" {
				for i, d := range decisions {
					if d["id"] == c {
						start = i + 1
					}
				}
			}
			end := start + limit
			if end > len(decisions) {
				end = len(decisions)
			}
			page := decisions[start:end]
			var next any
			if len(page) == limit && end < len(decisions) {
				next = page[len(page)-1]["id"]
			}
			send(200, map[string]any{"data": page, "next_cursor": next})
		case "/v1/strategies/decision_brain/promote":
			send(409, map[string]any{"error": map[string]string{"code": "live_routing_locked", "message": "locked"}})
		case "/v1/backtests":
			send(202, map[string]any{"id": "bt_1", "status": "queued"})
		case "/v1/backtests/bt_1":
			send(200, map[string]any{"id": "bt_1", "status": "complete",
				"result": map[string]any{"net": map[string]any{"net_r": 0.5}}})
		default:
			send(404, map[string]any{"error": map[string]string{"code": "not_found", "message": r.URL.Path}})
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &headers
}

func TestDecisionsFollowCursors(t *testing.T) {
	srv, headers := fake(t, map[string]bool{})
	c := NewWithKey("nxs_test_key", srv.URL)
	it := c.Decisions.List(context.Background(), &DecisionQuery{PageSize: 2})
	var ids []string
	for it.Next() {
		ids = append(ids, it.Value().ID)
	}
	if it.Err() != nil || len(ids) != 5 || ids[0] != "dec_5" || ids[4] != "dec_1" {
		t.Fatalf("got %v err %v", ids, it.Err())
	}
	if (*headers)[0].Get("Nexus-Version") != APIVersion {
		t.Fatal("version header missing")
	}
	limited := c.Decisions.List(context.Background(), &DecisionQuery{Limit: 3, PageSize: 2})
	n := 0
	for limited.Next() {
		n++
	}
	if n != 3 {
		t.Fatalf("limit ignored: %d", n)
	}
}

func TestErrorsCarryTheCode(t *testing.T) {
	srv, _ := fake(t, map[string]bool{})
	err := NewWithKey("nxs_test_key", srv.URL).Strategies.Promote(context.Background(), "decision_brain", "live")
	var apiErr *Error
	if !errors.As(err, &apiErr) || apiErr.Status != 409 || apiErr.Code != "live_routing_locked" {
		t.Fatalf("got %v", err)
	}
	_, err = NewWithKey("nxs_wrong", srv.URL).Strategies.List(context.Background())
	if !errors.As(err, &apiErr) || apiErr.Code != "unauthenticated" {
		t.Fatalf("got %v", err)
	}
}

func TestRetryAndBacktestRun(t *testing.T) {
	srv, _ := fake(t, map[string]bool{"/v1/decisions": true})
	c := NewWithKey("nxs_test_key", srv.URL)
	c.Backoff = 0
	it := c.Decisions.List(context.Background(), &DecisionQuery{Limit: 1})
	if !it.Next() || it.Err() != nil {
		t.Fatalf("retry failed: %v", it.Err())
	}
	b, err := c.Backtests.Run(context.Background(), BacktestRequest{Strategy: "decision_brain"}, 0)
	if err != nil || b.Status != "complete" || b.Result.Net.NetR != 0.5 {
		t.Fatalf("got %+v err %v", b, err)
	}
}
