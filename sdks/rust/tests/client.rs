use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tradelogx_nexus::{BacktestRequest, Client, DecisionQuery};

/// A tiny HTTP fake of the API, enough for the client's calls.
fn fake(fail_once: Option<&'static str>) -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", listener.local_addr().unwrap());
    let log = Arc::new(Mutex::new(Vec::new()));
    let log2 = log.clone();
    let failed = Arc::new(Mutex::new(false));
    thread::spawn(move || {
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let target = line.split_whitespace().nth(1).unwrap_or("/").to_string();
            let (mut auth, mut version, mut len) = (String::new(), String::new(), 0usize);
            loop {
                let mut h = String::new();
                reader.read_line(&mut h).unwrap();
                if h == "\r\n" || h.is_empty() { break; }
                let lower = h.to_ascii_lowercase();
                if lower.starts_with("authorization:") { auth = h[14..].trim().to_string(); }
                if lower.starts_with("nexus-version:") { version = h[14..].trim().to_string(); }
                if lower.starts_with("content-length:") { len = h[15..].trim().parse().unwrap_or(0); }
            }
            let mut body = vec![0u8; len];
            reader.read_exact(&mut body).unwrap();
            log2.lock().unwrap().push(format!("{target} {version}"));
            let path = target.split('?').next().unwrap().to_string();
            let query = target.split('?').nth(1).unwrap_or("").to_string();
            let (code, json, extra) = if auth != "Bearer nxs_test_key" {
                (401, r#"{"error":{"code":"unauthenticated","message":"no"}}"#.to_string(), "")
            } else if Some(path.as_str()) == fail_once && !*failed.lock().unwrap() {
                *failed.lock().unwrap() = true;
                (429, r#"{"error":{"code":"rate_limited","message":"slow"}}"#.to_string(), "Retry-After: 0\r\n")
            } else if path == "/v1/decisions" {
                let all = ["dec_5", "dec_4", "dec_3", "dec_2", "dec_1"];
                let limit: usize = query.split('&').find_map(|p| p.strip_prefix("limit=")).unwrap_or("50").parse().unwrap();
                let start = query.split('&').find_map(|p| p.strip_prefix("cursor="))
                    .map(|c| all.iter().position(|d| *d == c).unwrap() + 1).unwrap_or(0);
                let end = (start + limit).min(all.len());
                let items: Vec<String> = all[start..end].iter()
                    .map(|id| format!(r#"{{"id":"{id}","symbol":"BTCUSDT","verdict":"rejected"}}"#)).collect();
                let next = if end - start == limit && end < all.len() { format!(r#""{}""#, all[end - 1]) } else { "null".into() };
                (200, format!(r#"{{"data":[{}],"next_cursor":{}}}"#, items.join(","), next), "")
            } else if path == "/v1/strategies/decision_brain/promote" {
                (409, r#"{"error":{"code":"live_routing_locked","message":"locked"}}"#.to_string(), "")
            } else if path == "/v1/backtests" {
                (202, r#"{"id":"bt_1","status":"queued","result":null}"#.to_string(), "")
            } else if path == "/v1/backtests/bt_1" {
                (200, r#"{"id":"bt_1","status":"complete","result":{"net":{"trades":1,"win_rate":1,"expectancy_r":0.5,"net_r":0.5,"profit_factor":2,"max_drawdown_r":0}}}"#.to_string(), "")
            } else {
                (404, r#"{"error":{"code":"not_found","message":"x"}}"#.to_string(), "")
            };
            let reply = format!("HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n{extra}Content-Length: {}\r\nConnection: close\r\n\r\n{json}", json.len());
            stream.write_all(reply.as_bytes()).unwrap();
        }
    });
    (addr, log)
}

#[test]
fn decisions_follow_cursors_and_respect_limit() {
    let (base, log) = fake(None);
    let client = Client::new("nxs_test_key", &base);
    let ids: Vec<String> = client.decisions(DecisionQuery { page_size: Some(2), ..Default::default() })
        .map(|d| d.unwrap().id).collect();
    assert_eq!(ids, vec!["dec_5", "dec_4", "dec_3", "dec_2", "dec_1"]);
    assert!(log.lock().unwrap()[0].ends_with("2026-09-24"));
    let three = client.decisions(DecisionQuery { limit: Some(3), page_size: Some(2), ..Default::default() }).count();
    assert_eq!(three, 3);
}

#[test]
fn errors_carry_the_code() {
    let (base, _) = fake(None);
    let err = Client::new("nxs_test_key", &base).promote("decision_brain", "live").unwrap_err();
    assert_eq!((err.status, err.code.as_str()), (409, "live_routing_locked"));
    let err = Client::new("nxs_wrong", &base).strategies().unwrap_err();
    assert_eq!(err.code, "unauthenticated");
}

#[test]
fn retries_429_and_runs_backtests() {
    let (base, _) = fake(Some("/v1/decisions"));
    let mut client = Client::new("nxs_test_key", &base);
    client.backoff = Duration::from_millis(0);
    assert_eq!(client.decisions(DecisionQuery { limit: Some(1), ..Default::default() }).count(), 1);
    let job = client.run_backtest(&BacktestRequest { strategy: "decision_brain".into(), ..Default::default() },
                                  Duration::from_millis(0)).unwrap();
    assert_eq!(job.status, "complete");
    assert_eq!(job.result.unwrap().net.unwrap().net_r, 0.5);
}
