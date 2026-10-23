# Production stand-in: answers /health and /status/public like the real app.
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
BODY = {
    "/health": {"status": "ok", "commit_short": "215c96c"},
    "/status/public": {"overall": "operational", "components": [
        {"id": "api", "state": "operational", "detail": "Answering requests"},
        {"id": "workers", "state": "operational", "detail": "3 of 3 running"},
        {"id": "market_data", "state": "operational", "detail": "Candles current"}]},
}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps(BODY.get(self.path, {})).encode()
        self.send_response(200 if self.path in BODY else 404)
        self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
