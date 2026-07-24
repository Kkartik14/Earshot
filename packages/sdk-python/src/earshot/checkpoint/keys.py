"""At-rest key resolution shared by the durable spool and the checkpoint journal.

Both surfaces write governed evidence to a private directory and both offer the
same opt-in AES-256-GCM envelope. Keeping one resolver means the precedence
rules, the file-permission refusal, and the "32 bytes, raw or base64" coercion
cannot drift between them.

This module deliberately imports nothing from the rest of the package so the
exporter can reuse it without creating an import cycle through
``earshot.checkpoint``.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

AT_REST_KEY_BYTES = 32
AT_REST_NONCE_BYTES = 12


def import_aesgcm() -> type:
    """Return the AES-GCM primitive, importing ``cryptography`` lazily.

    Isolated in its own function so the base install never imports the optional
    dependency, and so tests can simulate the "requested but not installed"
    condition by patching this symbol to raise ``ImportError``.
    """

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM


def coerce_at_rest_key(value: bytes | str, *, label: str = "spool key") -> bytes:
    """Normalize a configured key to raw 32 AES-256 bytes.

    A ``bytes`` value of length 32 is treated as a raw key; any other ``bytes``
    value and every ``str`` value is interpreted as base64 that must decode to
    exactly 32 bytes (a 32-byte key is 44 base64 characters).
    """

    if isinstance(value, str):
        candidate = value.strip().encode("ascii")
    else:
        value = bytes(value)
        if len(value) == AT_REST_KEY_BYTES:
            return value
        candidate = value.strip()
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except ValueError:
        raise ValueError(f"{label} must be 32 raw bytes or a base64 encoding of 32 bytes") from None
    if len(decoded) != AT_REST_KEY_BYTES:
        raise ValueError(f"{label} must decode to 32 bytes for AES-256")
    return decoded


def read_at_rest_key_file(path: Path, *, variable: str, label: str = "spool key") -> bytes:
    """Load a key from a file reference (raw or base64, mode 0600)."""

    if path.is_symlink():
        raise ValueError(f"{variable} must not be a symbolic link")
    if not path.is_file():
        raise ValueError(f"{variable} must reference a regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{variable} must not be accessible by group or other users (chmod 600)")
    return coerce_at_rest_key(path.read_bytes(), label=label)


def resolve_at_rest_key(
    explicit: bytes | str | None,
    *,
    env_var: str,
    env_file_var: str,
    label: str = "spool key",
    fallback: tuple[str, str] | None = None,
) -> bytes | None:
    """Resolve an at-rest key by precedence, or ``None`` for plaintext.

    Precedence: the explicit argument, then ``env_var`` (base64), then
    ``env_file_var``. ``fallback`` names a second ``(env_var, env_file_var)``
    pair consulted last, which is how the journal inherits the spool's key
    without forcing an operator to configure the same secret twice. When nothing
    is configured the surface stays plaintext and behavior is unchanged.
    """

    if explicit is not None:
        return coerce_at_rest_key(explicit, label=label)
    candidates = [(env_var, env_file_var)]
    if fallback is not None:
        candidates.append(fallback)
    for inline_var, file_var in candidates:
        inline = os.environ.get(inline_var)
        if inline:
            return coerce_at_rest_key(inline, label=label)
        key_file = os.environ.get(file_var)
        if key_file:
            return read_at_rest_key_file(Path(key_file), variable=file_var, label=label)
    return None


def prepare_private_directory(path: Path, *, label: str) -> None:
    """Create or adopt an owner-private ``0700`` directory, refusing symlinks.

    A symlink is refused rather than followed because the whole point of the
    directory is that its permissions are the storage boundary; a link can move
    that boundary somewhere the operator never inspected.
    """

    if path.exists() and path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{label} must not be accessible by group or other users")
