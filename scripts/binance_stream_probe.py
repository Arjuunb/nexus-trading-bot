#!/usr/bin/env python3
"""Count Binance USD-M stream messages from ANY machine. Stdlib only.

    python3 scripts/binance_stream_probe.py [seconds]

No pip, no venv, no container: the point of this script is to run somewhere
the application does not, so its result can be compared against the VPS.

On 2026-09-21 the deployment's feed was dead while every socket connected in
0.7s and the venue acknowledged every subscription. bookTicker delivered
10,841 messages in 25 seconds on the same connection that delivered zero
klines, zero aggTrades and zero markPrice updates -- a book updating 434
times a second alongside zero trades, which cannot both be true of a live
venue. That is a serving condition at Binance, and the only way to tell a
venue-wide incident from something specific to one host's egress is to run
the identical probe from a different network. Every dependency this script
does not have is one that cannot stop that comparison happening.

It opens sockets and reads. It places no order, sends no credential, and
writes nothing.
"""
import base64, os, socket, ssl, struct, sys, time
from urllib.parse import urlsplit


def handshake(sock, host, path):
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode("latin1")
    if "101" not in status:
        raise ConnectionError(f"handshake refused: {status}")
    return rest


class Frames:
    """Minimal RFC 6455 reader. Server->client frames are never masked."""

    def __init__(self, sock, buffered=b""):
        self.sock, self.buf = sock, buffered

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def next(self):
        b0, b1 = self._need(2)
        opcode, length = b0 & 0x0F, b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._need(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._need(8))[0]
        mask = self._need(4) if b1 & 0x80 else None
        payload = self._need(length) if length else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload

    def pong(self, payload):
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        header = bytes([0x8A, 0x80 | len(payload)]) if len(payload) < 126 else None
        if header is None:
            raise ValueError("oversized ping")
        self.sock.sendall(header + mask + masked)


def probe(stream, seconds=20, base="wss://fstream.binance.com"):
    url = f"{base}/stream?streams={stream}"
    parts = urlsplit(url)
    host, path = parts.hostname, (parts.path or "/") + (
        "?" + parts.query if parts.query else "")
    started, count, first = time.monotonic(), 0, None
    try:
        raw = socket.create_connection((host, parts.port or 443), timeout=15)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        frames = Frames(sock, handshake(sock, host, path))
        opened = time.monotonic() - started
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                opcode, payload = frames.next()
            except (socket.timeout, TimeoutError):
                break
            if opcode == 0x9:
                frames.pong(payload)
                continue
            if opcode == 0x8:
                print(f"  {stream:22} server CLOSED the socket"); return
            if opcode in (0x1, 0x2):
                count += 1
                if first is None:
                    first = time.monotonic() - started - opened
        sock.close()
    except Exception as exc:
        print(f"  {stream:22} ERROR {type(exc).__name__}: {str(exc)[:60]}")
        return
    print(f"  {stream:22} connect {opened:5.2f}s   msgs {count:6d}   first "
          + (f"+{first:.2f}s" if first is not None else "   NEVER"))


if __name__ == "__main__":
    print(f"from THIS machine ({socket.gethostname()}), {sys.argv[1] if len(sys.argv)>1 else 20}s window:")
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    for s in ["btcusdt@bookTicker", "btcusdt@kline_5m",
              "btcusdt@markPrice@1s", "btcusdt@aggTrade"]:
        probe(s, secs)
