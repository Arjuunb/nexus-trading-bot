"""The post-deploy status script, exercised against a stubbed docker.

This script exists because hand-written command blocks kept failing on the
environment rather than the logic: a guessed repository path, a shell variable
lost with a dropped SSH session, a laptop command pasted into a server. Those
are all testable, so they are tested here -- with a fake `docker` on PATH, so
the control flow can be checked without a running container.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pa_status.sh"

STUB = r"""#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
case "$1 $2" in
  "compose ps")   echo "fake-container-id" ;;
  "compose exec")
      shift 3                       # compose exec -T
      service="$1"; shift
      case "$1" in
        printenv) echo "${FAKE_RUNNING_COMMIT:-deadbeef}" ;;
        test)     [ "${FAKE_AUDIT_EXISTS:-1}" = "1" ] || exit 1 ;;
        python)   echo "ran $*" ;;
      esac ;;
  "cp "*) printf 'copied' > "$3" ;;          # docker cp SRC DST
  "inspect -f") echo "nexus-trading-bot-app:fake" ;;
esac
exit 0
"""


@pytest.fixture()
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    home = tmp_path / "home"
    home.mkdir()

    def _run(**env):
        environment = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "HOME": str(home),
            "REVIEW_OUT": str(home / "review_2025.html"),
            **env,
        }
        proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                              text=True, env=environment)
        calls = log.read_text() if log.exists() else ""
        return proc, calls, home
    return _run


def test_it_runs_the_three_checks_and_copies_the_review_out(run):
    proc, calls, home = run()
    assert proc.returncode == 0, proc.stderr

    assert "pa_journal_guard_check.py" in calls
    assert "pa_journal_churn.py" in calls
    assert "pa_rulebook_review.py" in calls and "--text" in calls
    assert "docker cp fake-container-id:" in calls
    assert (home / "review_2025.html").exists()


def test_it_needs_no_path_argument_and_no_shell_variable(run):
    """The two failures that cost a round trip each: a guessed repo path and a
    $REPO that did not survive a dropped SSH session. The script derives its own
    root, so running it at all means the path is right."""
    proc, _, _ = run()
    assert proc.returncode == 0
    assert "null directory" not in proc.stdout + proc.stderr
    assert "No such file or directory" not in proc.stderr


def test_the_laptop_command_is_separated_from_the_server_commands(run):
    """The third failure: an scp line meant for a Mac, pasted into the VPS."""
    proc, _, _ = run()
    out = proc.stdout
    assert "Run this on YOUR OWN computer, not here" in out
    assert "type 'exit' first" in out
    assert out.index("YOUR OWN computer") > out.index("Journal guard")
    assert "scp -i" in out and "open ~/Desktop/" in out


def test_a_stale_running_image_is_called_out(run):
    proc, _, _ = run(FAKE_RUNNING_COMMIT="0000000000000000000000000000000000000000")
    assert "the running image is not this checkout" in proc.stdout
    assert "scripts/deploy.sh" in proc.stdout


def test_a_missing_audit_says_so_instead_of_rendering_nothing(run):
    """A review rendered from no audit would be an empty page that looks fine."""
    proc, calls, home = run(FAKE_AUDIT_EXISTS="0")
    assert proc.returncode == 0
    assert "the replay has not produced one yet" in proc.stdout
    assert "pa_rulebook_review.py" not in calls
    assert not (home / "review_2025.html").exists()
    assert "pa_rulebook_replay.py" in proc.stdout     # tells you how to start it


def test_it_stops_with_a_clear_message_when_docker_is_absent(tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    for tool in ("bash", "git", "cd", "tr", "awk", "hostname", "basename", "dirname"):
        found = shutil.which(tool)
        if found:
            (empty / tool).symlink_to(found)
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env={**os.environ, "PATH": str(empty)})
    assert proc.returncode == 1
    assert "docker is not on PATH" in proc.stderr
