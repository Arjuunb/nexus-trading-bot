"""The optional Compose overlay must not inherit trading authority or data."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_guardian_image_contains_only_guardian_python_package():
    dockerfile = (ROOT / "Dockerfile.guardian").read_text()
    assert "FROM python:3.11-slim" in dockerfile
    assert "COPY --chown=guardian:guardian tradexa/guardian/" in dockerfile
    assert "USER 10001:10001" in dockerfile
    for forbidden in ("COPY . .", "automation-hub/", "bot/", "HUB_CONTROL_KEY",
                      "HUB_ENABLE_EXTERNAL_LIVE"):
        assert forbidden not in dockerfile


def test_guardian_overlay_is_optional_and_has_no_trading_mount_or_credentials():
    base = (ROOT / "compose.yaml").read_text()
    overlay = (ROOT / "compose.guardian.yaml").read_text()
    assert "guardian:" not in base
    assert "Dockerfile.guardian" in overlay
    assert "env_file: .env.guardian" in overlay
    assert '"127.0.0.1:8765:8765"' in overlay
    assert "read_only: true" in overlay
    assert "no-new-privileges:true" in overlay
    assert "cap_drop:" in overlay and "- ALL" in overlay
    assert "GUARDIAN_DATA_PATH" in overlay
    for forbidden in ("env_file: .env\n", "tradexa-data:", "HUB_CONTROL_KEY:",
                      "HUB_EXCHANGE_API_KEY:", "HUB_ENABLE_EXTERNAL_LIVE:"):
        assert forbidden not in overlay


def test_example_uses_placeholder_keys_not_real_credentials():
    sample = (ROOT / "guardian.env.example").read_text()
    assert "REPLACE_WITH_" in sample
    assert "HUB_CONTROL_KEY=" not in sample
    assert "HUB_EXCHANGE_API_KEY=" not in sample
