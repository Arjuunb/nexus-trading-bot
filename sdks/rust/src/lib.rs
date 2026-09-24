//! Client for the TradeLogX Nexus public API (`/v1`).
//!
//! ```no_run
//! let client = tradelogx_nexus::Client::from_env()?;
//! for d in client.decisions(tradelogx_nexus::DecisionQuery { verdict: Some("rejected".into()), limit: Some(20), ..Default::default() }) {
//!     let d = d?;
//!     println!("{} {:?} {:?}", d.symbol, d.quality_score, d.blocked_by);
//! }
//! # Ok::<(), tradelogx_nexus::Error>(())
//! ```
//!
//! Blocking, on `ureq`. Pages are followed automatically; 429 and temporary
//! 5xx answers are retried with exponential backoff honouring Retry-After;
//! writes are retried only on 429, when the server says it did not process them.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// The `Nexus-Version` this client speaks.
pub const API_VERSION: &str = "2026-09-24";
const DEFAULT_BASE: &str = "https://trade-logx.com";

/// An error answer from the API (status, stable code, message), or a transport failure.
#[derive(Debug, Clone, PartialEq)]
pub struct Error {
    pub status: u16,
    pub code: String,
    pub message: String,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}: {}", self.status, self.code, self.message)
    }
}
impl std::error::Error for Error {}

#[derive(Debug, Clone, Deserialize)]
pub struct Strategy {
    pub id: String,
    pub name: String,
    pub version: String,
    pub lifecycle: String,
    pub mode: String,
    pub timeframes: Vec<String>,
    pub markets: Vec<String>,
    pub description: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Decision {
    pub id: String,
    pub ts: Option<String>,
    pub symbol: String,
    pub timeframe: Option<String>,
    pub strategy: Option<String>,
    pub side: Option<String>,
    pub regime: Option<String>,
    pub verdict: String,
    pub quality_score: Option<f64>,
    pub blocked_by: Option<String>,
    pub reason: Option<String>,
    #[serde(default)]
    pub executed: bool,
    pub instance_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Protective {
    pub stop: Option<f64>,
    pub target: Option<f64>,
    pub managed_by: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Position {
    pub id: String,
    pub instance_id: String,
    pub symbol: String,
    pub side: String,
    pub size: f64,
    pub entry: f64,
    pub mark: Option<f64>,
    pub mark_available: bool,
    pub unrealized_pnl: Option<f64>,
    pub r_multiple: Option<f64>,
    pub protective: Protective,
    pub mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Figures {
    pub trades: u32,
    pub win_rate: f64,
    pub expectancy_r: f64,
    pub net_r: f64,
    pub profit_factor: f64,
    pub max_drawdown_r: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BacktestResult {
    pub gross: Option<Figures>,
    pub net: Option<Figures>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Backtest {
    pub id: String,
    pub status: String,
    pub result: Option<BacktestResult>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct BacktestRequest {
    pub strategy: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub symbol: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeframe: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bars: Option<u32>,
}

/// Filters for [`Client::decisions`]. `limit: None` walks every page.
#[derive(Debug, Clone, Default)]
pub struct DecisionQuery {
    pub verdict: Option<String>,
    pub symbol: Option<String>,
    pub since: Option<String>,
    pub limit: Option<usize>,
    pub page_size: Option<usize>,
}

#[derive(Deserialize)]
struct Data<T> {
    data: T,
}

#[derive(Deserialize)]
struct Page {
    data: Vec<Decision>,
    next_cursor: Option<String>,
}

pub struct Client {
    api_key: String,
    base_url: String,
    pub version: String,
    pub max_retries: u32,
    pub backoff: Duration,
    agent: ureq::Agent,
}

impl Client {
    /// Reads `NEXUS_API_KEY` and, optionally, `NEXUS_API_BASE`.
    pub fn from_env() -> Result<Self, Error> {
        let key = std::env::var("NEXUS_API_KEY").map_err(|_| Error {
            status: 0,
            code: "no_api_key".into(),
            message: "set NEXUS_API_KEY".into(),
        })?;
        let base = std::env::var("NEXUS_API_BASE").unwrap_or_else(|_| DEFAULT_BASE.into());
        Ok(Self::new(&key, &base))
    }

    pub fn new(api_key: &str, base_url: &str) -> Self {
        Client {
            api_key: api_key.to_string(),
            base_url: base_url.trim_end_matches('/').to_string(),
            version: API_VERSION.to_string(),
            max_retries: 3,
            backoff: Duration::from_millis(500),
            agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(30)).build(),
        }
    }

    fn call(&self, method: &str, path: &str, query: &[(&str, String)], body: Option<serde_json::Value>)
        -> Result<serde_json::Value, Error> {
        let url = format!("{}/v1{}", self.base_url, path);
        let mut attempt = 0u32;
        loop {
            let mut req = self.agent.request(method, &url)
                .set("Authorization", &format!("Bearer {}", self.api_key))
                .set("Nexus-Version", &self.version)
                .set("Accept", "application/json");
            for (k, v) in query {
                req = req.query(k, v);
            }
            let result = match &body {
                Some(b) => req.send_json(b.clone()),
                None => req.call(),
            };
            match result {
                Ok(resp) => {
                    let text = resp.into_string().map_err(|e| net(e.to_string()))?;
                    return if text.is_empty() { Ok(serde_json::Value::Null) } else {
                        serde_json::from_str(&text).map_err(|e| net(e.to_string()))
                    };
                }
                Err(ureq::Error::Status(code, resp)) => {
                    let retryable = code == 429 || (method == "GET" && matches!(code, 502 | 503 | 504));
                    if retryable && attempt < self.max_retries {
                        let wait = resp.header("Retry-After").and_then(|s| s.parse::<u64>().ok())
                            .map(Duration::from_secs)
                            .unwrap_or(self.backoff * 2u32.pow(attempt));
                        std::thread::sleep(wait);
                        attempt += 1;
                        continue;
                    }
                    let text = resp.into_string().unwrap_or_default();
                    let parsed: serde_json::Value = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
                    let err = &parsed["error"];
                    return Err(Error {
                        status: code,
                        code: err["code"].as_str().unwrap_or("http_error").to_string(),
                        message: err["message"].as_str().unwrap_or(&text).to_string(),
                    });
                }
                Err(e) => {
                    if method == "GET" && attempt < self.max_retries {
                        std::thread::sleep(self.backoff * 2u32.pow(attempt));
                        attempt += 1;
                        continue;
                    }
                    return Err(net(e.to_string()));
                }
            }
        }
    }

    fn get<T: serde::de::DeserializeOwned>(&self, path: &str, query: &[(&str, String)]) -> Result<T, Error> {
        serde_json::from_value(self.call("GET", path, query, None)?).map_err(|e| net(e.to_string()))
    }

    pub fn strategies(&self) -> Result<Vec<Strategy>, Error> {
        Ok(self.get::<Data<Vec<Strategy>>>("/strategies", &[])?.data)
    }

    /// `mode` is "paper" or "live"; live is refused (`live_routing_locked`) while live routing is locked.
    pub fn promote(&self, strategy_id: &str, mode: &str) -> Result<serde_json::Value, Error> {
        self.call("POST", &format!("/strategies/{strategy_id}/promote"), &[], Some(serde_json::json!({ "mode": mode })))
    }

    /// Every matching decision, newest first, following pages lazily.
    pub fn decisions(&self, query: DecisionQuery) -> Decisions<'_> {
        Decisions { client: self, query, buf: Vec::new(), cursor: None, seen: 0, done: false }
    }

    pub fn decision(&self, id: &str) -> Result<Decision, Error> {
        self.get(&format!("/decisions/{id}"), &[])
    }

    pub fn positions(&self) -> Result<Vec<Position>, Error> {
        Ok(self.get::<Data<Vec<Position>>>("/positions", &[])?.data)
    }

    pub fn close_position(&self, id: &str, reason: &str) -> Result<serde_json::Value, Error> {
        self.call("POST", &format!("/positions/{id}/close"), &[], Some(serde_json::json!({ "reason": reason })))
    }

    pub fn create_backtest(&self, request: &BacktestRequest) -> Result<Backtest, Error> {
        let body = serde_json::to_value(request).map_err(|e| net(e.to_string()))?;
        serde_json::from_value(self.call("POST", "/backtests", &[], Some(body))?).map_err(|e| net(e.to_string()))
    }

    pub fn backtest(&self, id: &str) -> Result<Backtest, Error> {
        self.get(&format!("/backtests/{id}"), &[])
    }

    /// Queue a backtest and poll until it finishes.
    pub fn run_backtest(&self, request: &BacktestRequest, poll: Duration) -> Result<Backtest, Error> {
        let queued = self.create_backtest(request)?;
        loop {
            let job = self.backtest(&queued.id)?;
            match job.status.as_str() {
                "complete" => return Ok(job),
                "failed" => return Err(Error {
                    status: 200,
                    code: "backtest_failed".into(),
                    message: job.result.and_then(|r| r.error).unwrap_or_default(),
                }),
                _ => std::thread::sleep(poll),
            }
        }
    }
}

fn net(message: String) -> Error {
    Error { status: 0, code: "network_error".into(), message }
}

/// Iterator returned by [`Client::decisions`].
pub struct Decisions<'a> {
    client: &'a Client,
    query: DecisionQuery,
    buf: Vec<Decision>,
    cursor: Option<String>,
    seen: usize,
    done: bool,
}

impl Iterator for Decisions<'_> {
    type Item = Result<Decision, Error>;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(limit) = self.query.limit {
            if self.seen >= limit {
                return None;
            }
        }
        if self.buf.is_empty() {
            if self.done {
                return None;
            }
            let mut size = self.query.page_size.unwrap_or(100);
            if let Some(limit) = self.query.limit {
                size = size.min(limit - self.seen);
            }
            let mut q: Vec<(&str, String)> = vec![("limit", size.to_string())];
            for (k, v) in [("verdict", &self.query.verdict), ("symbol", &self.query.symbol),
                           ("since", &self.query.since), ("cursor", &self.cursor)] {
                if let Some(v) = v {
                    q.push((k, v.clone()));
                }
            }
            match self.client.get::<Page>("/decisions", &q) {
                Ok(page) => {
                    self.done = page.next_cursor.is_none();
                    self.cursor = page.next_cursor;
                    self.buf = page.data;
                    self.buf.reverse();
                }
                Err(e) => {
                    self.done = true;
                    return Some(Err(e));
                }
            }
            if self.buf.is_empty() {
                return None;
            }
        }
        self.seen += 1;
        self.buf.pop().map(Ok)
    }
}

/// Check a webhook's `Nexus-Signature` header against the raw request body.
/// Call it before parsing and reject the request when it returns false.
/// `now_unix` is the current time in seconds; `tolerance_s` how old the
/// signature's timestamp may be (300 is a sensible default).
pub fn verify_webhook(secret: &str, body: &[u8], header: &str, now_unix: i64, tolerance_s: i64) -> bool {
    use hmac::{Hmac, Mac};
    let mut t: Option<i64> = None;
    let mut sig: Option<&str> = None;
    for part in header.split(',') {
        match part.split_once('=') {
            Some(("t", v)) => t = v.parse().ok(),
            Some(("v1", v)) => sig = Some(v),
            Some(_) => {}
            None => return false,
        }
    }
    let (Some(t), Some(sig)) = (t, sig) else { return false };
    if (now_unix - t).abs() > tolerance_s {
        return false;
    }
    let Ok(mut mac) = Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes()) else { return false };
    mac.update(format!("{t}.").as_bytes());
    mac.update(body);
    let Some(expected) = (0..sig.len()).step_by(2)
        .map(|i| sig.get(i..i + 2).and_then(|h| u8::from_str_radix(h, 16).ok()))
        .collect::<Option<Vec<u8>>>() else { return false };
    mac.verify_slice(&expected).is_ok()
}
