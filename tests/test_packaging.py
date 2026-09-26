"""What actually ships in the deployed container.

This file exists because of a bug it would have caught. ``pyproject.toml``
listed only ``bot*`` under ``packages.find``, so ``pip install -e .`` — the one
install the Dockerfile runs — never made ``tradexa`` importable. The backend
starts from ``automation-hub/``, where the repo root is not on ``sys.path``, so
every ``import tradexa...`` in production failed:

    the risk-engine veto           → self.risk_engine = None, no veto applied
    the shared sizing arithmetic   → fell back to its inline copy
    the portfolio endpoints        → 500

None of it surfaced. The guarded imports are written to degrade quietly, which
is right for resilience and exactly wrong for noticing. And the whole test suite
passed, because ``conftest.py`` appends the repo root to ``sys.path`` — so every
one of those imports resolved under pytest and only under pytest.

The lesson generalises past this one bug: a test suite that manipulates the
import path is testing a different program from the one that deploys. These
tests read the packaging configuration instead, which is what the container
obeys.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Top-level packages the backend imports at runtime and therefore must be
#: installed, not merely present in the repository.
REQUIRED_PACKAGES = ("bot", "tradexa")

#: ``tomllib`` is 3.11+. The support matrix includes 3.10, where importing it at
#: module level took the whole file out at COLLECTION time — so the guard
#: against a packaging bug was itself a packaging-era mistake, and it turned one
#: third of CI red. Optional import, with a narrow fallback below.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    tomllib = None


def _pyproject_text() -> str:
    return (ROOT / "pyproject.toml").read_text()


def _include_via_toml(text: str) -> list[str]:
    find = (tomllib.loads(text).get("tool", {}).get("setuptools", {})
            .get("packages", {}).get("find", {}))
    return list(find.get("include") or [])


def _include_via_scan(text: str) -> list[str]:
    """Read the include list without a TOML parser.

    Deliberately narrow: it looks for one known key in one known section of one
    file in this repository. It is not a TOML parser and must never grow into
    one — ``test_both_readers_agree`` keeps it honest wherever ``tomllib``
    exists, which is everywhere except the oldest supported interpreter.
    """
    section = re.search(
        r"^\[tool\.setuptools\.packages\.find\]\s*$(.*?)(?=^\[|\Z)",
        text, re.MULTILINE | re.DOTALL)
    if not section:
        return []
    include = re.search(r"^\s*include\s*=\s*\[(.*?)\]", section.group(1),
                        re.MULTILINE | re.DOTALL)
    if not include:
        return []
    # Strip comments before harvesting strings: a commented-out pattern is not
    # installed, and counting it would make this test pass on a broken build.
    body = re.sub(r"#[^\n]*", "", include.group(1))
    return re.findall(r"[\"']([^\"']+)[\"']", body)


def _include_patterns() -> list[str]:
    text = _pyproject_text()
    return _include_via_toml(text) if tomllib else _include_via_scan(text)


@pytest.mark.skipif(tomllib is None, reason="no tomllib before Python 3.11")
def test_both_readers_agree():
    """The fallback cannot drift from the real parser unnoticed.

    Without this, the 3.10 path could quietly read nothing and every assertion
    below would pass vacuously on the one interpreter the original bug hit.
    """
    text = _pyproject_text()
    assert _include_via_scan(text) == _include_via_toml(text)


def test_the_reader_finds_something():
    """Guards the guard: an empty read makes every test below vacuous."""
    assert _include_patterns(), "could not read packages.find include"


def test_every_runtime_package_is_installed_by_the_build():
    """The regression itself."""
    patterns = _include_patterns()
    for package in REQUIRED_PACKAGES:
        assert any(p in (package, f"{package}*") for p in patterns), (
            f"'{package}' is imported by the backend but is not in "
            f"pyproject.toml packages.find include={patterns} — it will be "
            "missing from the deployed container while every test still passes")


def test_the_required_packages_exist_as_real_packages():
    """Guards the guard: an entry for a package that does not exist would make
    the test above pass while shipping nothing."""
    for package in REQUIRED_PACKAGES:
        assert (ROOT / package / "__init__.py").is_file(), (
            f"{package}/__init__.py is missing — the include pattern would "
            "match nothing")


def test_the_backend_imports_only_installed_top_level_packages():
    """Every repo-root package the backend imports must be in the install list.

    This is the general form of the bug: adding a new top-level package and
    importing it from automation-hub works locally, works under pytest, and
    fails in the container. Discovering it means reading the include list, which
    nobody does — so the test does.
    """
    import ast

    backend = ROOT / "automation-hub"
    # Directories at the repo root that are importable packages.
    root_packages = {p.name for p in ROOT.iterdir()
                     if p.is_dir() and (p / "__init__.py").is_file()}
    installed = {p.rstrip("*") for p in _include_patterns()}

    used: dict[str, str] = {}
    for path in backend.rglob("*.py"):
        if "/tests/" in str(path) or path.name == "conftest.py":
            continue          # tests legitimately extend sys.path
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:   # pragma: no cover - not this test's business
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                head = name.split(".")[0]
                if head in root_packages:
                    used.setdefault(head, str(path.relative_to(ROOT)))

    missing = {pkg: where for pkg, where in used.items() if pkg not in installed}
    assert not missing, (
        "the backend imports repo-root package(s) that the build does not "
        f"install: {missing} — add them to pyproject.toml packages.find include")


def test_conftest_extends_sys_path_and_that_is_why_this_file_exists():
    """States the trap in the place someone would look after this file fails.

    ``automation-hub/conftest.py`` appending the repo root is correct — the suite
    has to import both trees. It also means an import that only works because of
    that line looks healthy in CI. That gap is what these tests cover, and
    deleting them because "the imports obviously work" is how the bug comes back.
    """
    conftest = ROOT / "automation-hub" / "conftest.py"
    assert conftest.is_file(), "the backend conftest moved — re-check this trap"
    assert "sys.path" in conftest.read_text()


def test_vps_deploy_exports_runtime_revision_metadata():
    """The public health response must identify the checkout actually shipped.

    ``env_file`` values override image ``ENV`` values, including with an empty
    string.  Guard both halves of the fix: the deploy path derives authoritative
    values, and Compose passes them explicitly after loading ``.env``.
    """
    deploy = (ROOT / "scripts" / "deploy.sh").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    assert "git rev-parse HEAD" in deploy
    assert "git branch --show-current" in deploy
    assert "date -u +%Y-%m-%dT%H:%M:%SZ" in deploy
    assert "export GIT_COMMIT GIT_BRANCH DEPLOYED_AT" in deploy
    for name in ("GIT_COMMIT", "GIT_BRANCH", "DEPLOYED_AT"):
        assert f"{name}: ${{{name}:-unknown}}" in compose


def test_nginx_retains_only_required_startup_capabilities():
    """Nginx must initialize its tmpfs paths after the drop-all baseline.

    Removing these additions makes the production container restart with
    ``chown(... client_temp) failed (Operation not permitted)`` before it can
    create ``/run/nginx.pid`` or accept public HTTPS traffic.
    """
    compose = (ROOT / "compose.yaml").read_text()

    assert "cap_drop: [ALL]" in compose
    assert "cap_add: [CHOWN, DAC_OVERRIDE, SETGID, SETUID, NET_BIND_SERVICE]" in compose


def test_the_app_receives_dockers_stop_signal_and_has_time_to_shut_down():
    """``sh -c "uvicorn ..."`` left sh as PID 1 with uvicorn as its child. sh as
    PID 1 ignores the SIGTERM ``docker stop`` sends, so every deploy ended in
    SIGKILL: the shutdown hook never ran, entry gates were not checkpointed and
    every worker lease stayed held for the next container."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    [cmd] = [line for line in dockerfile.splitlines() if line.startswith("CMD ")]
    assert '"exec uvicorn app:app' in cmd
    assert "--timeout-graceful-shutdown" in cmd
    grace = re.search(r"stop_grace_period:\s*(\d+)s", compose)
    assert grace and int(grace.group(1)) >= 30

