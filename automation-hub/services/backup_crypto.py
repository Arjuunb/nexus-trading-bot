"""Sealed backup archives: the key vault's envelope scheme, applied to files.

A backup holds everything the platform knows -- the ledger, the journals, the
audit log, the alert-channel settings -- so a copy on disk must be as well
protected as the data it copies. Each archive gets its own random 256-bit data
key; the data key is stored only wrapped under the master key
(``HUB_MASTER_KEY``, the same one that protects exchange keys) and the file
body is sealed with AES-256-GCM in 1 MiB chunks.

Every chunk's authenticated data carries a hash of the header, the chunk's
position and whether it is the last one, so a chunk that is altered, dropped,
reordered or cut off -- including a file truncated exactly on a chunk
boundary -- fails to open instead of restoring something incomplete.

File layout::

    b"TLXBAK1\\n" | u32 header length | header JSON
    repeated:   u32 ciphertext length | 12-byte nonce | ciphertext
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TLXBAK1\n"
CHUNK = 1 << 20
_MAX_HEADER = 64 * 1024
_MAX_CT = CHUNK + 16


class BackupSealError(RuntimeError):
    """The archive could not be opened: wrong master key, or it was altered."""


def _kek_id(kek: bytes) -> str:
    from services.key_vault import kek_fingerprint
    return kek_fingerprint(kek)


def _aad(header_hash: bytes, index: int, final: bool) -> bytes:
    return header_hash + struct.pack(">QB", index, 1 if final else 0)


def seal_file(src: Path, dst: Path, kek: bytes, *, context: str) -> dict:
    """Encrypt ``src`` into ``dst``. ``context`` (e.g. the snapshot stamp) is
    bound into the wrapped data key, so an archive cannot be passed off as a
    different snapshot."""
    if len(kek) != 32:
        raise ValueError("master key must be 32 bytes")
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, b"tlx-backup|" + context.encode())
    header = json.dumps({
        "v": 1, "context": context, "kek_id": _kek_id(kek), "chunk": CHUNK,
        "wrap_nonce": base64.b64encode(wrap_nonce).decode(),
        "wrapped_dek": base64.b64encode(wrapped).decode(),
    }, sort_keys=True).encode()
    header_hash = hashlib.sha256(header).digest()
    aead = AESGCM(dek)
    chunks = 0
    with open(src, "rb") as fin, open(dst, "wb") as out:
        out.write(MAGIC + struct.pack(">I", len(header)) + header)
        buf = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            final = not nxt
            nonce = os.urandom(12)
            ct = aead.encrypt(nonce, buf, _aad(header_hash, chunks, final))
            out.write(struct.pack(">I", len(ct)) + nonce + ct)
            chunks += 1
            if final:
                break
            buf = nxt
    return {"chunks": chunks, "kek_id": _kek_id(kek), "context": context}


def read_header(path: Path) -> dict:
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupSealError("Not a sealed backup archive.")
        (n,) = struct.unpack(">I", f.read(4))
        if n > _MAX_HEADER:
            raise BackupSealError("Archive header is malformed.")
        return json.loads(f.read(n))


def open_file(src: Path, dst: Path, kek: bytes, *, context: str | None = None) -> dict:
    """Decrypt ``src`` into ``dst``, verifying every chunk. Raises
    ``BackupSealError`` (and leaves no partial ``dst``) on any failure."""
    try:
        return _open(src, dst, kek, context)
    except BackupSealError:
        Path(dst).unlink(missing_ok=True)
        raise
    except (InvalidTag, struct.error, ValueError, KeyError) as exc:
        Path(dst).unlink(missing_ok=True)
        raise BackupSealError(f"Archive failed verification ({type(exc).__name__}).") from None


def _open(src, dst, kek, context):
    with open(src, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupSealError("Not a sealed backup archive.")
        (n,) = struct.unpack(">I", f.read(4))
        if n > _MAX_HEADER:
            raise BackupSealError("Archive header is malformed.")
        header = f.read(n)
        meta = json.loads(header)
        if meta.get("kek_id") != _kek_id(kek):
            raise BackupSealError("This archive was sealed under a different master key.")
        if context is not None and meta.get("context") != context:
            raise BackupSealError("This archive belongs to a different snapshot.")
        dek = AESGCM(kek).decrypt(base64.b64decode(meta["wrap_nonce"]),
                                  base64.b64decode(meta["wrapped_dek"]),
                                  b"tlx-backup|" + meta["context"].encode())
        header_hash = hashlib.sha256(header).digest()
        aead = AESGCM(dek)

        def frame():
            raw = f.read(4)
            if not raw:
                return None
            if len(raw) != 4:
                raise BackupSealError("Archive is truncated.")
            (length,) = struct.unpack(">I", raw)
            if length > _MAX_CT:
                raise BackupSealError("Archive chunk is malformed.")
            nonce, ct = f.read(12), f.read(length)
            if len(nonce) != 12 or len(ct) != length:
                raise BackupSealError("Archive is truncated.")
            return nonce, ct

        index, current = 0, frame()
        if current is None:
            raise BackupSealError("Archive has no content.")
        with open(dst, "wb") as out:
            while current is not None:
                following = frame()
                final = following is None
                out.write(aead.decrypt(current[0], current[1], _aad(header_hash, index, final)))
                index, current = index + 1, following
    return {"chunks": index, "context": meta["context"]}
