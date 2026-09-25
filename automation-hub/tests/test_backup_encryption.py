"""Sealed backups (services/backup.py, services/backup_crypto.py)."""
import io
import json
import os
import shutil
import tarfile
from datetime import datetime, timezone

import pytest

from services.backup import backup_now, list_backups, restore_check, restore_to
from services.backup_crypto import CHUNK, BackupSealError, open_file, seal_file

NOW = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)
SECRET_LINE = "discord-webhook-https://example.invalid/hooks/SENSITIVE-TOKEN-123"


@pytest.fixture()
def key():
    return os.urandom(32)


@pytest.fixture()
def data(tmp_path):
    from data.ledger import SqliteLedger
    led = SqliteLedger(str(tmp_path / "ledger.db"))
    led.log(level="info", stage="t", message="a ledger line worth keeping")
    (tmp_path / "alert_channels.json").write_text(json.dumps({"discord": SECRET_LINE}))
    return tmp_path


def test_sealed_snapshot_leaves_no_plaintext_and_restores_exactly(data, key, tmp_path_factory):
    result = backup_now(str(data), now=NOW, master_key=key)
    assert result["ok"] and result["encrypted"]
    root = data / "backups"
    assert [p.name for p in root.iterdir() if p.is_dir()] == []  # no plaintext folder left
    archive = root / f"{result['snapshot']}.tlxb"
    raw = archive.read_bytes()
    assert b"SENSITIVE-TOKEN" not in raw and b"ledger line" not in raw
    side = json.loads((root / f"{result['snapshot']}.tlxb.json").read_text())
    assert set(side["files"]) == {"ledger.db", "alert_channels.json"} and side["encrypted"]

    out = tmp_path_factory.mktemp("restored")
    back = restore_to(str(data), result["snapshot"], str(out), master_key=key)
    assert back["ok"] and back["encrypted"]
    assert json.loads((out / "alert_channels.json").read_text())["discord"] == SECRET_LINE
    chk = restore_check(str(data), result["snapshot"], master_key=key)
    assert chk["ok"] and chk["databases"]["ledger.db"]["ok"]
    assert list_backups(str(data))["backups"][0]["encrypted"] is True


def test_without_a_master_key_backups_say_they_are_not_encrypted(data):
    result = backup_now(str(data), now=NOW, master_key=None)
    assert result["ok"] and result["encrypted"] is False and "HUB_MASTER_KEY" in result["warning"]


def test_the_wrong_master_key_restores_nothing(data, key, tmp_path_factory):
    stamp = backup_now(str(data), now=NOW, master_key=key)["snapshot"]
    out = tmp_path_factory.mktemp("restored")
    back = restore_to(str(data), stamp, str(out), master_key=os.urandom(32))
    assert not back["ok"] and "different master key" in back["error"]
    assert list(out.iterdir()) == []


def test_a_renamed_archive_is_refused(data, key, tmp_path_factory):
    stamp = backup_now(str(data), now=NOW, master_key=key)["snapshot"]
    root = data / "backups"
    shutil.copy(root / f"{stamp}.tlxb", root / "20200101T000000Z.tlxb")
    back = restore_to(str(data), "20200101T000000Z", str(tmp_path_factory.mktemp("r")), master_key=key)
    assert not back["ok"] and "different snapshot" in back["error"]


def test_restoring_over_the_live_data_directory_is_refused(data, key):
    stamp = backup_now(str(data), now=NOW, master_key=key)["snapshot"]
    assert restore_to(str(data), stamp, str(data), master_key=key)["ok"] is False


# ------------------------------------------------------------ archive format
@pytest.fixture()
def big(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(os.urandom(int(CHUNK * 2.5)))  # three chunks
    return path


def test_round_trip_across_chunks(big, key, tmp_path):
    sealed, opened = tmp_path / "a.tlxb", tmp_path / "out.bin"
    assert seal_file(big, sealed, key, context="s1")["chunks"] == 3
    open_file(sealed, opened, key, context="s1")
    assert opened.read_bytes() == big.read_bytes()


def _frames(path):
    raw = path.read_bytes()
    header_len = int.from_bytes(raw[8:12], "big")
    pos = 12 + header_len
    starts = []
    while pos < len(raw):
        starts.append(pos)
        pos += 4 + 12 + int.from_bytes(raw[pos:pos + 4], "big")
    return raw, starts


@pytest.mark.parametrize("damage", ["flip", "drop_last_chunk", "swap_chunks", "append"])
def test_any_alteration_fails_and_leaves_no_output(big, key, tmp_path, damage):
    sealed, opened = tmp_path / "a.tlxb", tmp_path / "out.bin"
    seal_file(big, sealed, key, context="s1")
    raw, starts = _frames(sealed)
    if damage == "flip":
        b = bytearray(raw); b[starts[1] + 40] ^= 1; raw = bytes(b)
    elif damage == "drop_last_chunk":  # truncated exactly on a chunk boundary
        raw = raw[:starts[-1]]
    elif damage == "swap_chunks":
        c0, c1 = raw[starts[0]:starts[1]], raw[starts[1]:starts[2]]
        raw = raw[:starts[0]] + c1 + c0 + raw[starts[2]:]
    elif damage == "append":
        raw = raw + raw[starts[0]:starts[1]]
    sealed.write_bytes(raw)
    with pytest.raises(BackupSealError):
        open_file(sealed, opened, key, context="s1")
    assert not opened.exists()


def test_an_archive_with_a_path_escaping_entry_is_refused(data, key, tmp_path_factory):
    root = data / "backups"
    root.mkdir()
    stamp = "20260924T030000Z"
    tar_path = tmp_path_factory.mktemp("t") / "evil.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        payload = b"owned"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    seal_file(tar_path, root / f"{stamp}.tlxb", key, context=stamp)
    out = tmp_path_factory.mktemp("restore")
    back = restore_to(str(data), stamp, str(out), master_key=key)
    assert not back["ok"] and "unexpected entry" in back["error"]
    assert not (out.parent / "escaped.txt").exists()


def test_after_a_rotation_old_snapshots_restore_with_the_previous_key(data, key, tmp_path_factory, monkeypatch):
    import base64
    new = os.urandom(32)
    old_snap = backup_now(data, master_key=key)["snapshot"]
    monkeypatch.setenv("HUB_MASTER_KEY", base64.b64encode(new).decode())
    monkeypatch.delenv("HUB_MASTER_KEY_PREVIOUS", raising=False)
    refused = restore_to(data, old_snap, str(tmp_path_factory.mktemp("r1")))
    assert not refused["ok"] and "HUB_MASTER_KEY_PREVIOUS" in refused["error"]
    monkeypatch.setenv("HUB_MASTER_KEY_PREVIOUS", base64.b64encode(key).decode())
    assert restore_to(data, old_snap, str(tmp_path_factory.mktemp("r2")))["ok"]
    assert restore_check(data, old_snap)["ok"]
