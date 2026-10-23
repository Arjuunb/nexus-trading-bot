#!/usr/bin/env python3
"""Run the unchanged dashboard browser suite and the reviewed clipboard harness with its isolated test API.

This only starts disposable localhost fixtures. It cannot connect to a VPS,
publish images, start staging workers or change production state.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
HARNESS = {
    "playwright.config.cjs": "8471eb93a117695112478fd84ed1d9dc3e0b345e32e4faa1d1be70a3b38094de",
    "clipboard.spec.cjs": "44442aec9d596abfdb966d62b59fe306306a78a6293cb8163efe0582af4c187c",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = Path(args.config).resolve()
    if config.name != "playwright.config.cjs":
        raise ValueError("REVIEWED_CONFIG_REQUIRED")
    for name, expected in HARNESS.items():
        path = config.parent / name
        if not path.is_file() or path.stat().st_size > 64 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("REVIEWED_HARNESS_HASH_MISMATCH")
    output = Path(args.output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    dashboard = ROOT / "automation-hub-dashboard"
    env = {**os.environ, "NEXUS_RESEARCH_E2E": "1", "NEXUS_RESEARCH_E2E_EXTERNAL": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.setdefault("PLAYWRIGHT_JUNIT_OUTPUT_FILE", str(output / "browser-tests.xml"))
    with (output / "fixture.log").open("w") as log:
        fixture = subprocess.Popen([sys.executable, "e2e/fixtures/strategy_api.py"],
                                   cwd=dashboard, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while True:
                if fixture.poll() is not None:
                    raise RuntimeError("RESEARCH_API_FIXTURE_EXITED; inspect fixture.log")
                try:
                    with urlopen("http://127.0.0.1:18005/__acceptance__/state", timeout=2) as response:
                        state = json.load(response)
                    if (state.get("actual_episode_count") != 1
                            or not all(state.get(key) is True for key in
                                ("financial_records_unchanged", "original_events_unchanged", "validation_records_unchanged"))):
                        raise RuntimeError("RESEARCH_API_FIXTURE_NOT_READY")
                    break
                except (URLError, TimeoutError, ConnectionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("RESEARCH_API_FIXTURE_STARTUP_TIMEOUT") from None
                    time.sleep(.2)
            return subprocess.run([str(dashboard / "node_modules/.bin/playwright"), "test", "--config", str(config),
                "--reporter=list,junit", "--output=" + str(output / "browser-output")], cwd=dashboard, env=env).returncode
        finally:
            if fixture.poll() is None:
                fixture.terminate()
                try:
                    fixture.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    fixture.kill()
                    fixture.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
