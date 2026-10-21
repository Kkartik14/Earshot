"""Shared plumbing for the hosted-provider capture scripts (Month 3 WS0).

Only the capture scripts (``capture_elevenlabs.py``, ``capture_twilio.py``) and the
scrubber (``scrub_provider_capture.py``) use this. Rules it enforces:

* Keys are read from an env file in-process; values are never printed or logged.
* Raw captures go under ``.earshot/raw-captures/<provider>/`` (git-ignored), never
  into ``fixtures/``.
* HTTP is stdlib ``urllib`` over https with a fixed provider host.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / ".earshot" / "raw-captures"
DEFAULT_ENV = ROOT.parent / "TVIC" / ".env"
USER_AGENT = "earshot-capture/0.1"
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_keys(env_file: Path, names: list[str]) -> dict[str, str]:
    """Return the named keys from ``env_file``; raise naming missing keys only."""
    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        match = _ENV_LINE.match(line)
        if not match or match.group(1) not in names:
            continue
        raw = match.group(2)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        values[match.group(1)] = raw
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise SystemExit(f"missing env keys: {', '.join(missing)}")
    return values


_REDACT = (
    (re.compile(r"\b(?:AC|CA|RE|PN|SK)[0-9a-fA-F]{32}\b"), "[SID]"),
    (re.compile(r"\+?\d[\d\s().-]{6,}\d"), "[NUMBER]"),
    (re.compile(r"(?:conv|agent|user|sk)_[A-Za-z0-9]{8,}"), "[ID]"),
    (re.compile(r"[A-Za-z0-9_-]{32,}"), "[TOKEN]"),
)


def redact(text: str) -> str:
    """Strip SIDs, phone-like numbers, provider IDs and long tokens from a message."""
    for pattern, placeholder in _REDACT:
        text = pattern.sub(placeholder, text)
    return text


class HttpError(RuntimeError):
    """An HTTP failure whose message carries the status and a redacted path and body."""


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise HttpError("refusing a non-https URL")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        raise HttpError(redact(f"{method} {parsed.path} -> HTTP {exc.code}: {detail}")) from None


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_response(
    directory: Path, name: str, status: int, headers: dict[str, str], body: bytes
) -> None:
    """Store one raw response: the body as-is plus a sidecar with status and safe headers."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(body)
    keep = {k: v for k, v in headers.items() if k in {"content-type", "content-length", "date"}}
    write_json(directory / f"{name}.meta.json", {"status": status, "headers": keep})


# --- Leak rules (T4(c)): the one authority for the scrubber and the integrity tests. ---

# Placeholders are format-valid IDs whose body is zeros plus a small hex counter.
_PLACEHOLDER_BODY = re.compile(rb"^0+[0-9a-f]{1,4}$")
_TWILIO_SID = re.compile(rb"\b(AC|CA|RE|PN|SK|MG)([0-9a-f]{32})\b")
_ELEVENLABS_ID = re.compile(rb"\b(agent|conv|agtbrch|agtvrsn|user|seng)_([0-9a-z]{20,})\b")
_E164 = re.compile(rb"(?:\+|%2B)(\d{10,15})\b")
LEAK_PATTERNS: dict[str, re.Pattern[bytes]] = {
    "email": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}"),
    "jwt": re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "sk_key": re.compile(rb"\bsk_[A-Za-z0-9]{20,}"),
    "bearer": re.compile(rb"Bearer\s+[A-Za-z0-9._-]{12,}"),
    "basic_auth": re.compile(rb"Basic\s+[A-Za-z0-9+/]{20,}={0,2}"),
    "signed_url": re.compile(rb"conversation_signature=|[?&](?:token|signature|X-Amz-Signature)="),
}


def find_leaks(data: bytes) -> list[str]:
    """Names of the leak rules that ``data`` breaks. Never returns the matched values."""
    hits = [name for name, pattern in LEAK_PATTERNS.items() if pattern.search(data)]
    if any(not _PLACEHOLDER_BODY.match(m.group(2)) for m in _TWILIO_SID.finditer(data)):
        hits.append("twilio_sid")
    if any(not _PLACEHOLDER_BODY.match(m.group(2)) for m in _ELEVENLABS_ID.finditer(data)):
        hits.append("elevenlabs_id")
    if any(not m.group(1).startswith(b"1555") for m in _E164.finditer(data)):
        hits.append("e164_outside_555")
    return hits


def secret_variants(value: str) -> set[str]:
    """The spellings of one secret or phone number that could appear in a payload."""
    variants = {value}
    digits = re.sub(r"\D", "", value)
    if value.startswith("+") and len(digits) >= 8:
        variants |= {digits, digits[-10:], "%2B" + digits, "%2b" + digits}
    return {v for v in variants if len(v) >= 8}


def env_secret_values(env_files: list[Path]) -> dict[str, str]:
    """Secret-bearing values from the env files: keys, tokens, SIDs, numbers, voice IDs."""
    secret_name = re.compile(r"(_API_KEY|_TOKEN|_SID|_NUMBER|_VOICE_ID|_SECRET|_URL)$")
    values: dict[str, str] = {}
    for env_file in env_files:
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            match = _ENV_LINE.match(line)
            if not match or not secret_name.search(match.group(1)):
                continue
            raw = match.group(2).strip("'\"")
            if len(raw) >= 8:
                values.setdefault(match.group(1), raw)
    return values


def sha256_prefixes(values: dict[str, str]) -> dict[str, str]:
    """T4(c): the 16-hex sha256 prefix of each secret, so a hashed leak is caught too."""
    import hashlib

    return {name: hashlib.sha256(v.encode()).hexdigest()[:16] for name, v in values.items()}
