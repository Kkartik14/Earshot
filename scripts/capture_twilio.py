"""Capture one real Twilio call and its recording for Month 3 WS0 (FR-4 AC4.1).

No webhooks and no tunnel: the call is placed with inline TwiML and the REST API is
polled. Raw output goes to ``.earshot/raw-captures/twilio/`` (git-ignored); the callee
may be a human, so the raw audio never leaves that folder.

``preflight``
    Free GETs: the account (type, status) and whether ``TWILIO_FROM_NUMBER`` is an
    incoming number of the account. Prints those facts only.

``call``
    The one owner-approved call: ``TWILIO_FROM_NUMBER`` -> ``TWILIO_TO_NUMBER`` with
    ``Record=true``, ``RecordingChannels=dual``, ``Timeout=40`` and ``TimeLimit=60``
    and the fixed ``<Say>`` script below. Polls until the call is final, then fetches
    the Call resource, the Recordings lists and the media (default, dual, mono).

``refetch``
    Free GETs of the Call and Recording resources again (``price`` fills in late).

Keys come from ``--env`` (default ``../TVIC/.env``): ``TWILIO_ACCOUNT_SID``,
``TWILIO_AUTH_TOKEN``, ``TWILIO_FROM_NUMBER``, ``TWILIO_TO_NUMBER``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_capture import (
    DEFAULT_ENV,
    RAW_ROOT,
    HttpError,
    basic_auth,
    load_keys,
    redact,
    request,
    write_json,
    write_response,
)

API = "https://api.twilio.com/2010-04-01"
RAW = RAW_ROOT / "twilio" / "call-run"
KEYS = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER"]
# Three sentences separated by 3 s pauses: the pattern identifies the <Say> channel.
SAY_SCRIPT = (
    "This is an Earshot test call. It checks how call recordings are stored.",
    "No reply is needed. The test plays three short sentences.",
    "That is the end of the test. Thank you, and goodbye.",
)
PAUSE_S = 3
FINAL_CALL_STATES = {"completed", "busy", "failed", "no-answer", "canceled"}


def twiml() -> str:
    pause = f'<Pause length="{PAUSE_S}"/>'
    says = [f"<Say>{escape(line)}</Say>" for line in SAY_SCRIPT]
    return "<Response>" + pause.join(says) + "</Response>"


class Twilio:
    def __init__(self, keys: dict[str, str]) -> None:
        self.sid = keys["TWILIO_ACCOUNT_SID"]
        self.auth = basic_auth(self.sid, keys["TWILIO_AUTH_TOKEN"])
        self.base = f"{API}/Accounts/{urllib.parse.quote(self.sid, safe='')}"

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return request("GET", self.base + path, headers={"Authorization": self.auth})

    def get_json(self, path: str) -> Any:
        return json.loads(self.get(path)[2])

    def save(self, path: str, name: str) -> bytes:
        status, headers, body = self.get(path)
        write_response(RAW, name, status, headers, body)
        return body


def preflight(api: Twilio, keys: dict[str, str]) -> None:
    account = json.loads(request("GET", api.base + ".json", headers={"Authorization": api.auth})[2])
    print(f"account: type={account.get('type')} status={account.get('status')}")
    number = urllib.parse.quote(keys["TWILIO_FROM_NUMBER"], safe="")
    owned = api.get_json(f"/IncomingPhoneNumbers.json?PhoneNumber={number}")
    print(f"from number owned by the account: {bool(owned.get('incoming_phone_numbers'))}")
    if account.get("type") == "Trial":
        verified = api.get_json(
            "/OutgoingCallerIds.json?PhoneNumber="
            + urllib.parse.quote(keys["TWILIO_TO_NUMBER"], safe="")
        )
        print(f"trial account; to number verified: {bool(verified.get('outgoing_caller_ids'))}")


def _recording_sids(api: Twilio, call_sid: str) -> list[dict[str, Any]]:
    cid = urllib.parse.quote(call_sid, safe="")
    return api.get_json(f"/Calls/{cid}/Recordings.json").get("recordings", [])


def call(api: Twilio, keys: dict[str, str]) -> None:
    if (RAW / "create.json").exists():
        raise SystemExit("a call was already placed; the owner approved exactly one")
    form = {
        "To": keys["TWILIO_TO_NUMBER"],
        "From": keys["TWILIO_FROM_NUMBER"],
        "Twiml": twiml(),
        "Record": "true",
        "RecordingChannels": "dual",
        "Timeout": "40",
        "TimeLimit": "60",
    }
    RAW.mkdir(parents=True, exist_ok=True)
    started = time.time()
    status, headers, body = request(
        "POST",
        api.base + "/Calls.json",
        headers={
            "Authorization": api.auth,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=urllib.parse.urlencode(form).encode(),
    )
    write_response(RAW, "create.json", status, headers, body)
    call_sid = json.loads(body)["sid"]
    write_json(
        RAW / "run.json", {"twiml": twiml(), "started_unix": started, "form_keys": sorted(form)}
    )
    cid = urllib.parse.quote(call_sid, safe="")
    seen: list[str] = []
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = api.get_json(f"/Calls/{cid}.json").get("status")
        if not seen or seen[-1] != state:
            seen.append(state)
            print(f"call status: {state}")
        if state in FINAL_CALL_STATES:
            break
        time.sleep(2)
    run = json.loads((RAW / "run.json").read_text())
    run.update({"status_sequence": seen, "final_unix": time.time()})
    write_json(RAW / "run.json", run)
    recordings: list[dict[str, Any]] = []
    for _ in range(30):
        recordings = _recording_sids(api, call_sid)
        if recordings and all(r.get("status") == "completed" for r in recordings):
            break
        time.sleep(3)
    print(f"recordings: {len(recordings)} statuses={[r.get('status') for r in recordings]}")
    _fetch_all(api, call_sid, recordings, media=True)


def _fetch_all(api: Twilio, call_sid: str, recordings: list[dict[str, Any]], media: bool) -> None:
    cid = urllib.parse.quote(call_sid, safe="")
    api.save(f"/Calls/{cid}.json", "call.json")
    api.save(f"/Calls/{cid}/Recordings.json", "call_recordings.json")
    api.save(f"/Recordings.json?CallSid={cid}", "recordings_by_callsid.json")
    api.save(f"/Calls/{cid}/Events.json", "call_events.json")
    api.save(f"/Calls/{cid}/Notifications.json", "call_notifications.json")
    for index, rec in enumerate(recordings):
        rid = urllib.parse.quote(rec["sid"], safe="")
        api.save(f"/Recordings/{rid}.json", f"recording_{index}.json")
        if not media:
            continue
        for label, query in (
            ("default", ""),
            ("dual", "?RequestedChannels=2"),
            ("mono", "?RequestedChannels=1"),
        ):
            try:
                audio = api.save(f"/Recordings/{rid}.wav{query}", f"recording_{index}.{label}.wav")
                print(f"recording {index} {label}: {len(audio)} bytes")
            except HttpError as exc:
                print(f"recording {index} {label}: {exc}")


def refetch(api: Twilio) -> None:
    call_sid = json.loads((RAW / "create.json").read_text())["sid"]
    recordings = _recording_sids(api, call_sid)
    _fetch_all(api, call_sid, recordings, media=False)
    call_resource = json.loads((RAW / "call.json").read_text())
    print(f"call price={call_resource.get('price')} unit={call_resource.get('price_unit')}")
    for index in range(len(recordings)):
        rec = json.loads((RAW / f"recording_{index}.json").read_text())
        print(f"recording {index} price={rec.get('price')} unit={rec.get('price_unit')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("command", choices=["preflight", "call", "refetch"])
    args = parser.parse_args()
    keys = load_keys(args.env, KEYS)
    api = Twilio(keys)
    try:
        if args.command == "preflight":
            preflight(api, keys)
        elif args.command == "call":
            call(api, keys)
        else:
            refetch(api)
    except Exception as exc:
        message = redact(str(exc))
        for value in keys.values():
            message = message.replace(value, "[REDACTED]")
        raise SystemExit(f"{type(exc).__name__}: {message}") from None


if __name__ == "__main__":
    main()
