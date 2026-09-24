// Package nexus is a client for the TradeLogX Nexus public API (/v1).
//
//	client, err := nexus.New()            // reads NEXUS_API_KEY
//	it := client.Decisions.List(ctx, &nexus.DecisionQuery{Verdict: "rejected"})
//	for it.Next() {
//	    d := it.Value()
//	    if d.BlockedBy != nil { // optional fields are pointers
//	        fmt.Println(d.Symbol, "blocked by", *d.BlockedBy)
//	    }
//	}
//	if err := it.Err(); err != nil { log.Fatal(err) }
//
// Standard library only. Pages are followed automatically; 429 and temporary
// 5xx answers are retried with exponential backoff honouring Retry-After;
// writes are retried only on 429, when the server says it did not process them.
package nexus

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// APIVersion is the Nexus-Version this client speaks.
const APIVersion = "2026-09-24"

const defaultBase = "https://trade-logx.com"

// Error is an error answer from the API: HTTP status, a stable code and a message.
type Error struct {
	Status  int
	Code    string
	Message string
}

func (e *Error) Error() string { return fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Message) }

// Client talks to one deployment with one API key.
type Client struct {
	APIKey     string
	BaseURL    string
	Version    string
	MaxRetries int
	Backoff    time.Duration
	HTTP       *http.Client

	Strategies *StrategiesService
	Decisions  *DecisionsService
	Positions  *PositionsService
	Backtests  *BacktestsService
}

// New builds a client from NEXUS_API_KEY and, optionally, NEXUS_API_BASE.
func New() (*Client, error) {
	key := os.Getenv("NEXUS_API_KEY")
	if key == "" {
		return nil, errors.New("nexus: no API key; set NEXUS_API_KEY or use NewWithKey")
	}
	base := os.Getenv("NEXUS_API_BASE")
	if base == "" {
		base = defaultBase
	}
	return NewWithKey(key, base), nil
}

// NewWithKey builds a client for an explicit key and base URL.
func NewWithKey(key, baseURL string) *Client {
	c := &Client{APIKey: key, BaseURL: strings.TrimRight(baseURL, "/"), Version: APIVersion,
		MaxRetries: 3, Backoff: 500 * time.Millisecond, HTTP: &http.Client{Timeout: 30 * time.Second}}
	c.Strategies = &StrategiesService{c}
	c.Decisions = &DecisionsService{c}
	c.Positions = &PositionsService{c}
	c.Backtests = &BacktestsService{c}
	return c
}

func retryable(method string, status int) bool {
	if status == http.StatusTooManyRequests {
		return true
	}
	return method == http.MethodGet && (status == 502 || status == 503 || status == 504)
}

// Do performs one API call, decoding a JSON answer into out (which may be nil).
func (c *Client) Do(ctx context.Context, method, path string, query url.Values, body, out any) error {
	u := c.BaseURL + "/v1" + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	var payload []byte
	if body != nil {
		var err error
		if payload, err = json.Marshal(body); err != nil {
			return err
		}
	}
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, method, u, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
		req.Header.Set("Nexus-Version", c.Version)
		req.Header.Set("Accept", "application/json")
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := c.HTTP.Do(req)
		if err != nil {
			if method == http.MethodGet && attempt < c.MaxRetries {
				sleep(ctx, c.Backoff<<attempt)
				continue
			}
			return &Error{Status: 0, Code: "network_error", Message: err.Error()}
		}
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode < 300 {
			if out == nil || len(raw) == 0 {
				return nil
			}
			return json.Unmarshal(raw, out)
		}
		if retryable(method, resp.StatusCode) && attempt < c.MaxRetries {
			wait := c.Backoff << attempt
			if s, err := strconv.Atoi(resp.Header.Get("Retry-After")); err == nil {
				wait = time.Duration(s) * time.Second
			}
			sleep(ctx, wait)
			continue
		}
		var envelope struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		if json.Unmarshal(raw, &envelope) == nil && envelope.Error.Code != "" {
			return &Error{Status: resp.StatusCode, Code: envelope.Error.Code, Message: envelope.Error.Message}
		}
		return &Error{Status: resp.StatusCode, Code: "http_error", Message: strings.TrimSpace(string(raw))}
	}
}

func sleep(ctx context.Context, d time.Duration) {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

// Strategy is one strategy the platform can run.
type Strategy struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	Version     string   `json:"version"`
	Lifecycle   string   `json:"lifecycle"`
	Mode        string   `json:"mode"`
	Timeframes  []string `json:"timeframes"`
	Markets     []string `json:"markets"`
	Description string   `json:"description"`
}

// StrategiesService covers /v1/strategies.
type StrategiesService struct{ c *Client }

// List returns every strategy.
func (s *StrategiesService) List(ctx context.Context) ([]Strategy, error) {
	var out struct {
		Data []Strategy `json:"data"`
	}
	err := s.c.Do(ctx, http.MethodGet, "/strategies", nil, nil, &out)
	return out.Data, err
}

// Promote asks for mode "paper" or "live". Live is refused with code
// live_routing_locked while live routing is locked.
func (s *StrategiesService) Promote(ctx context.Context, id, mode string) error {
	return s.c.Do(ctx, http.MethodPost, "/strategies/"+url.PathEscape(id)+"/promote", nil,
		map[string]string{"mode": mode}, nil)
}

// Decision is one evaluation, accepted or rejected.
type Decision struct {
	ID           string         `json:"id"`
	TS           string         `json:"ts"`
	Symbol       string         `json:"symbol"`
	Timeframe    *string        `json:"timeframe"`
	Strategy     *string        `json:"strategy"`
	Side         *string        `json:"side"`
	Regime       *string        `json:"regime"`
	Verdict      string         `json:"verdict"`
	QualityScore *float64       `json:"quality_score"`
	BlockedBy    *string        `json:"blocked_by"`
	Reason       *string        `json:"reason"`
	Executed     bool           `json:"executed"`
	InstanceID   *string        `json:"instance_id"`
	Components   map[string]any `json:"components"`
}

// DecisionQuery filters a decision listing. Limit 0 walks every page.
type DecisionQuery struct {
	Verdict  string
	Symbol   string
	Since    string
	Limit    int
	PageSize int
}

// DecisionsService covers /v1/decisions.
type DecisionsService struct{ c *Client }

// DecisionIterator walks pages of decisions: for it.Next() { it.Value() }; then it.Err().
type DecisionIterator struct {
	ctx    context.Context
	c      *Client
	q      DecisionQuery
	buf    []Decision
	cur    Decision
	cursor string
	seen   int
	done   bool
	err    error
}

// List returns an iterator over every matching decision, newest first.
func (s *DecisionsService) List(ctx context.Context, q *DecisionQuery) *DecisionIterator {
	it := &DecisionIterator{ctx: ctx, c: s.c}
	if q != nil {
		it.q = *q
	}
	if it.q.PageSize <= 0 {
		it.q.PageSize = 100
	}
	return it
}

// Next advances to the next decision, fetching a page when needed.
func (it *DecisionIterator) Next() bool {
	if it.err != nil || (it.q.Limit > 0 && it.seen >= it.q.Limit) {
		return false
	}
	if len(it.buf) == 0 {
		if it.done {
			return false
		}
		size := it.q.PageSize
		if it.q.Limit > 0 && it.q.Limit-it.seen < size {
			size = it.q.Limit - it.seen
		}
		v := url.Values{"limit": {strconv.Itoa(size)}}
		for k, val := range map[string]string{"verdict": it.q.Verdict, "symbol": it.q.Symbol,
			"since": it.q.Since, "cursor": it.cursor} {
			if val != "" {
				v.Set(k, val)
			}
		}
		var page struct {
			Data       []Decision `json:"data"`
			NextCursor *string    `json:"next_cursor"`
		}
		if it.err = it.c.Do(it.ctx, http.MethodGet, "/decisions", v, nil, &page); it.err != nil {
			return false
		}
		it.buf = page.Data
		if page.NextCursor == nil || *page.NextCursor == "" {
			it.done = true
		} else {
			it.cursor = *page.NextCursor
		}
		if len(it.buf) == 0 {
			return false
		}
	}
	it.cur, it.buf = it.buf[0], it.buf[1:]
	it.seen++
	return true
}

// Value is the current decision.
func (it *DecisionIterator) Value() Decision { return it.cur }

// Err is the error that stopped iteration, if any.
func (it *DecisionIterator) Err() error { return it.err }

// Get fetches one decision by id (e.g. "dec_42").
func (s *DecisionsService) Get(ctx context.Context, id string) (Decision, error) {
	var d Decision
	err := s.c.Do(ctx, http.MethodGet, "/decisions/"+url.PathEscape(id), nil, nil, &d)
	return d, err
}

// Position is one open paper position.
type Position struct {
	ID            string   `json:"id"`
	InstanceID    string   `json:"instance_id"`
	Symbol        string   `json:"symbol"`
	Side          string   `json:"side"`
	Size          float64  `json:"size"`
	Entry         float64  `json:"entry"`
	Mark          *float64 `json:"mark"`
	MarkAvailable bool     `json:"mark_available"`
	UnrealizedPnL *float64 `json:"unrealized_pnl"`
	RMultiple     *float64 `json:"r_multiple"`
	Protective    struct {
		Stop      *float64 `json:"stop"`
		Target    *float64 `json:"target"`
		ManagedBy string   `json:"managed_by"`
	} `json:"protective"`
	OpenedAt *string `json:"opened_at"`
	Mode     string  `json:"mode"`
}

// PositionsService covers /v1/positions.
type PositionsService struct{ c *Client }

// List returns the open paper positions.
func (s *PositionsService) List(ctx context.Context) ([]Position, error) {
	var out struct {
		Data []Position `json:"data"`
	}
	err := s.c.Do(ctx, http.MethodGet, "/positions", nil, nil, &out)
	return out.Data, err
}

// Close closes one paper position at the current observed mark (control scope).
func (s *PositionsService) Close(ctx context.Context, id, reason string) error {
	return s.c.Do(ctx, http.MethodPost, "/positions/"+url.PathEscape(id)+"/close", nil,
		map[string]string{"reason": reason}, nil)
}

// Figures are the headline numbers of a backtest, in R.
type Figures struct {
	Trades       int     `json:"trades"`
	WinRate      float64 `json:"win_rate"`
	ExpectancyR  float64 `json:"expectancy_r"`
	NetR         float64 `json:"net_r"`
	ProfitFactor float64 `json:"profit_factor"`
	MaxDrawdownR float64 `json:"max_drawdown_r"`
}

// Backtest is a queued, running or finished backtest.
type Backtest struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Request struct {
		Strategy  string `json:"strategy"`
		Symbol    string `json:"symbol"`
		Timeframe string `json:"timeframe"`
		Bars      int    `json:"bars"`
	} `json:"request"`
	Result *struct {
		Gross *Figures `json:"gross"`
		Net   *Figures `json:"net"`
		Costs *struct {
			CostPctPerSide float64 `json:"cost_pct_per_side"`
			NetRDrag       float64 `json:"net_r_drag"`
		} `json:"costs"`
		Error string `json:"error"`
	} `json:"result"`
}

// BacktestRequest describes one backtest.
type BacktestRequest struct {
	Strategy  string `json:"strategy"`
	Symbol    string `json:"symbol,omitempty"`
	Timeframe string `json:"timeframe,omitempty"`
	Bars      int    `json:"bars,omitempty"`
}

// BacktestsService covers /v1/backtests.
type BacktestsService struct{ c *Client }

// Create queues a backtest (control scope).
func (s *BacktestsService) Create(ctx context.Context, r BacktestRequest) (Backtest, error) {
	var b Backtest
	err := s.c.Do(ctx, http.MethodPost, "/backtests", nil, r, &b)
	return b, err
}

// Get fetches a backtest's status and result.
func (s *BacktestsService) Get(ctx context.Context, id string) (Backtest, error) {
	var b Backtest
	err := s.c.Do(ctx, http.MethodGet, "/backtests/"+url.PathEscape(id), nil, nil, &b)
	return b, err
}

// Run queues a backtest and polls until it finishes or ctx ends.
func (s *BacktestsService) Run(ctx context.Context, r BacktestRequest, poll time.Duration) (Backtest, error) {
	b, err := s.Create(ctx, r)
	if err != nil {
		return b, err
	}
	for {
		if b, err = s.Get(ctx, b.ID); err != nil {
			return b, err
		}
		switch b.Status {
		case "complete":
			return b, nil
		case "failed":
			msg := ""
			if b.Result != nil {
				msg = b.Result.Error
			}
			return b, &Error{Status: 200, Code: "backtest_failed", Message: msg}
		}
		select {
		case <-ctx.Done():
			return b, ctx.Err()
		case <-time.After(poll):
		}
	}
}
