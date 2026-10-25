"""Provider recordings fetched into the blob store: the fetch jobs, their runner, their job.

**Jobs.** A connector delivery that holds a provider recording queues its fetch in
the same mutation (``recording_fetches``, written by ``storage_deliveries``). Leases
follow the generic leased-job helper (``leased_jobs``) on that table; a lease is
the fetch's whole time limit plus ``LEASE_MARGIN_SECONDS``, so a fetch cannot be
leased twice while it may still run.

**Where the bytes come from.** A ``provider_api`` fetch builds its URL from the
provider call ID on the provider's own API host (``PROVIDER_APIS``) and sends the
connector's API key (its ``api_key_ref``) to that origin only; the ID must be a
plain ID (``PROVIDER_ID``) and is percent-encoded, or nothing is fetched. A
``payload_url`` fetch uses the URL the delivery carried, and every hop must name a
host its provider's allowlist names (``PAYLOAD_HOSTS``, extended by the operator's
``EARSHOT_FETCH_ALLOWED_HOSTS``). ``recording_fetcher`` holds the network guard.

**Storing.** The response streams into the recordings store's own upload attempt
(``begin_upload`` journals its key before any byte; the call holds while it
streams; ``finish_upload`` stores the recording and dirties its call, so the closer
makes a revision of it). A WAV, FLAC or Ogg body must start with its declared
format's header.

**Retries.** A failure that may pass (DNS, connecting, TLS, a timeout, a broken
body, 429 or 5xx, the blob store, a missing API key) is retried after 1, 4 and 16
minutes, then is terminal ``failed`` with its last code. A retry that would start
after a payload URL's known expiry ends ``expired`` instead, and so does a payload
URL that answers 403, 404 or 410; a provider API's 403, 404 or 410 is retried (the
recording may not be ready yet). Anything else is terminal at once. Retries go on
past the recording's pending deadline: the recordings sweep never fails a
recording whose fetch is live, the closer's hold still ends at the deadline (the
call's revision closes without the audio), and a later success is the next
revision (``late_data``).

**Terminal.** A fetch that ends without the recording fails the recording in the
same transaction; its code stays on the fetch. Every terminal state sets the URL to
NULL: a payload URL is a bearer capability, kept only while the job can use it.
Logs and errors name the host only.

**Purge.** A purged call's recordings and fetches go together (``ON DELETE
CASCADE``); a fetch in flight then stores nothing (``finish_upload`` finds no
pending recording) and deletes its own object.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import quote

from .leased_jobs import FAILED, JobLease, LeasedJobs
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .recording_fetcher import (
    FETCH_NOT_AUDIO,
    FETCH_URL_INVALID,
    GONE,
    RETRY,
    Credential,
    FetchError,
    FetchLimits,
    FetchRequest,
    FetchResponse,
    FetchStopped,
    HostAllowlist,
    RecordingFetcher,
)
from .recordings import RecordingHeaderError, media_inspector
from .storage import StorageBusyError

if TYPE_CHECKING:
    from .connectors.types import SecretResolver
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection
    from .storage_recordings import RecordingStore

_LOGGER = logging.getLogger(__name__)
_SECOND = 1_000_000_000
LEASE_MARGIN_SECONDS = 60
FETCH_CONCURRENCY = 2
# How long a stopping server waits for fetches in flight to notice it.
CLOSE_GRACE_SECONDS = 10.0
STORED = "stored"
EXPIRED = "expired"
PROVIDER_API = "provider_api"
PAYLOAD_URL = "payload_url"
ALLOWED_HOSTS_ENVIRONMENT = "EARSHOT_FETCH_ALLOWED_HOSTS"
# A provider call ID that may go into a provider API's URL path.
PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

FETCH_ID_INVALID = "EARSHOT_FETCH_ID_INVALID"
FETCH_SOURCE_UNSUPPORTED = "EARSHOT_FETCH_SOURCE_UNSUPPORTED"
FETCH_KEY_MISSING = "EARSHOT_FETCH_KEY_MISSING"
FETCH_URL_EXPIRED = "EARSHOT_FETCH_URL_EXPIRED"
FETCH_PRESET_FORBIDS_MEDIA = "EARSHOT_FETCH_PRESET_FORBIDS_MEDIA"
FETCH_STORE_FAILED = "EARSHOT_FETCH_STORE_FAILED"
FETCH_RECORDING_FAILED = "EARSHOT_FETCH_RECORDING_FAILED"

FETCH_JOBS = LeasedJobs("recording_fetches", ("project_id", "recording_id"))


@dataclass(frozen=True, slots=True)
class ProviderApi:
    """Where a provider's API serves a call's recording by its ID."""

    host: str
    # Track -> path; ``{id}`` is the percent-encoded provider call ID.
    paths: Mapping[str, str]
    header: str
    # The header's value; ``{key}`` is the API key.
    value: str


PROVIDER_APIS: Mapping[str, ProviderApi] = MappingProxyType(
    {
        # docs.vapi.ai/assistants/retrieve-call-artifacts: each answers 302 to a
        # short-lived signed URL on another host.
        "vapi": ProviderApi(
            "api.vapi.ai",
            MappingProxyType(
                {
                    "customer": "/call/{id}/customer-recording",
                    "assistant": "/call/{id}/assistant-recording",
                    "mixed": "/call/{id}/mono-recording",
                }
            ),
            "Authorization",
            "Bearer {key}",
        ),
        # Verified on a real conversation: audio/mpeg, no redirect.
        "elevenlabs": ProviderApi(
            "api.elevenlabs.io",
            MappingProxyType({"mixed": "/v1/convai/conversations/{id}/audio"}),
            "xi-api-key",
            "{key}",
        ),
    }
)

# Unverified provider hosts require an operator allowlist via ``EARSHOT_FETCH_ALLOWED_HOSTS``.
PAYLOAD_HOSTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "elevenlabs": (),
        "retell": ("retellai.s3.us-west-2.amazonaws.com",),
        "ringg": (),
        "vapi": ("storage.vapi.ai",),
    }
)


def payload_allowlists(extra: str | None) -> dict[str, HostAllowlist]:
    """Each provider's payload-URL allowlist: its seeded hosts plus ``extra``, the
    value of ``EARSHOT_FETCH_ALLOWED_HOSTS`` (comma-separated ``provider:host``,
    where a host may be ``*.domain``)."""

    hosts = {provider: list(seeded) for provider, seeded in PAYLOAD_HOSTS.items()}
    for item in (extra or "").split(","):
        if not item.strip():
            continue
        provider, separator, host = item.strip().partition(":")
        if not separator or provider.strip().lower() not in hosts:
            raise ValueError(
                f"{ALLOWED_HOSTS_ENVIRONMENT} entries are provider:host, with a provider "
                f"of {', '.join(sorted(hosts))}: {item.strip()!r}"
            )
        hosts[provider.strip().lower()].append(host.strip())
    try:
        return {provider: HostAllowlist(listed) for provider, listed in hosts.items()}
    except ValueError as error:
        raise ValueError(f"{ALLOWED_HOSTS_ENVIRONMENT}: {error}") from None


@dataclass(frozen=True, slots=True)
class FetchClaim:
    """One leased fetch and what it needs."""

    lease: JobLease
    project_id: str
    recording_id: str
    provider: str
    source_kind: str
    provider_call_id: str
    track: str
    url: str | None = field(repr=False)
    url_expires_unix_nano: int | None
    api_key_ref: str | None
    content_type: str


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What one ``run_next`` did: ``stored``, ``pending`` (tried again later),
    ``failed``, ``expired``, or ``lost`` (the fetch is gone: its call was purged)."""

    project_id: str
    recording_id: str
    result: str
    error_code: str | None = None


_CLAIM_COLUMNS = (
    "f.source_kind, f.provider_call_id, f.track, f.url, f.url_expires_unix_nano, "
    "r.state AS recording_state, r.content_type, c.provider, c.config_json"
)


class FetchStore:
    """Fetch jobs on one ``IncidentStore``'s catalog, locks and clock."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store

    def _now(self, now_unix_nano: int | None) -> int:
        return self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano

    def due(self, *, now_unix_nano: int | None = None) -> bool:
        """Whether a fetch is due (a read; no write lock)."""

        with self.store._connect() as connection:
            return bool(FETCH_JOBS.due(connection, self._now(now_unix_nano)))

    def claim(
        self,
        *,
        lease_nano: int,
        now_unix_nano: int | None = None,
        budget_s: float | None = None,
    ) -> FetchClaim | FetchOutcome | None:
        """Lease the next due fetch; ``None`` when none is due. A fetch with nothing
        left to do (its recording is no longer pending, or its URL has expired) is
        ended in the same mutation, and its outcome returned instead."""

        now = self._now(now_unix_nano)
        if not self.due(now_unix_nano=now):
            return None
        with (
            self.store._mutation("claim_recording_fetch", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            lease = FETCH_JOBS.claim(connection, now, lease_nano=lambda _key: lease_nano)
            if lease is None:
                connection.commit()
                return None
            project_id, recording_id = lease.key
            row = connection.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM recording_fetches f "
                "JOIN recordings r ON r.project_id = f.project_id "
                "AND r.recording_id = f.recording_id "
                "JOIN connectors c ON c.endpoint_id = f.endpoint_id "
                "WHERE f.project_id = ? AND f.recording_id = ?",
                (project_id, recording_id),
            ).fetchone()
            outcome: FetchOutcome | None = None
            if row["recording_state"] != "pending":
                outcome = self._settle(connection, lease, now, row["recording_state"])
            elif (
                row["source_kind"] == PAYLOAD_URL
                and row["url_expires_unix_nano"] is not None
                and now >= int(row["url_expires_unix_nano"])
            ):
                outcome = self._end(connection, lease, now, EXPIRED, FETCH_URL_EXPIRED)
            connection.commit()
        if outcome is not None:
            return outcome
        return FetchClaim(
            lease=lease,
            project_id=project_id,
            recording_id=recording_id,
            provider=row["provider"],
            source_kind=row["source_kind"],
            provider_call_id=row["provider_call_id"],
            track=row["track"],
            url=row["url"],
            url_expires_unix_nano=(
                None if row["url_expires_unix_nano"] is None else int(row["url_expires_unix_nano"])
            ),
            api_key_ref=_api_key_ref(row["config_json"]),
            content_type=row["content_type"],
        )

    def complete_stored(self, claim: FetchClaim, *, budget_s: float | None = None) -> bool:
        """The recording is stored: end the fetch (``False``: it was gone)."""

        with (
            self.store._mutation("finish_recording_fetch", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            done = FETCH_JOBS.complete(
                connection, claim.lease, self.store._clock.unix_nano(), state=STORED,
                columns={"url": None},
            )  # fmt: skip
            connection.commit()
        return done

    def settle(self, claim: FetchClaim, *, budget_s: float | None = None) -> FetchOutcome:
        """End a fetch whose recording stopped being pending while it ran."""

        with (
            self.store._mutation("settle_recording_fetch", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            row = connection.execute(
                "SELECT state FROM recordings WHERE project_id = ? AND recording_id = ?",
                (claim.project_id, claim.recording_id),
            ).fetchone()
            outcome = self._settle(
                connection, claim.lease, self.store._clock.unix_nano(),
                None if row is None else row["state"],
            )  # fmt: skip
            connection.commit()
        return outcome

    def fail(
        self,
        claim: FetchClaim,
        code: str,
        *,
        state: str,
        budget_s: float | None = None,
    ) -> FetchOutcome:
        """Record a failed attempt: ``state`` ``retry`` (tried again after its delay,
        ``failed`` past the last one, ``expired`` if the URL expires first), or a
        terminal ``failed`` or ``expired``. A terminal fetch fails its recording."""

        with (
            self.store._mutation("fail_recording_fetch", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            now = self.store._clock.unix_nano()
            if state != RETRY:
                outcome = self._end(connection, claim.lease, now, state, code)
            else:
                delays = FETCH_JOBS.retry_delays_seconds
                attempt = claim.lease.attempts + 1
                retry_at = now + delays[attempt - 1] * _SECOND if attempt <= len(delays) else None
                if (
                    retry_at is not None
                    and claim.source_kind == PAYLOAD_URL
                    and claim.url_expires_unix_nano is not None
                    and retry_at >= claim.url_expires_unix_nano
                ):
                    outcome = self._end(connection, claim.lease, now, EXPIRED, FETCH_URL_EXPIRED)
                else:
                    retried = FETCH_JOBS.retry(
                        connection, claim.lease, now, error_code=code, columns={"url": None}
                    )
                    if retried == FAILED:
                        self._fail_recording(connection, claim.lease)
                    outcome = _outcome(claim.lease, retried, code)
            connection.commit()
        return outcome

    def release_leases(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> int:
        """Return every running fetch to pending: at a server's start none survives."""

        with (
            self.store._mutation("release_recording_fetch_leases", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            released = FETCH_JOBS.release(connection, self._now(now_unix_nano))
            connection.commit()
        return released

    # --- helpers ------------------------------------------------------------------------

    @staticmethod
    def _fail_recording(connection: DatabaseConnection, lease: JobLease) -> None:
        connection.execute(
            "UPDATE recordings SET state = 'failed' "
            "WHERE project_id = ? AND recording_id = ? AND state = 'pending'",
            lease.key,
        )

    def _end(
        self, connection: DatabaseConnection, lease: JobLease, now: int, state: str, code: str
    ) -> FetchOutcome:
        """End the fetch in a terminal ``state`` with ``code``, and fail its recording."""

        ended = FETCH_JOBS.complete(
            connection, lease, now, state=state, error_code=code, columns={"url": None}
        )
        if ended:
            self._fail_recording(connection, lease)
        return _outcome(lease, state if ended else None, code)

    def _settle(
        self, connection: DatabaseConnection, lease: JobLease, now: int, recording: str | None
    ) -> FetchOutcome:
        if recording == STORED:
            ended = FETCH_JOBS.complete(connection, lease, now, state=STORED, columns={"url": None})
            return _outcome(lease, STORED if ended else None, None)
        if recording == FAILED:
            return self._end(connection, lease, now, FAILED, FETCH_RECORDING_FAILED)
        return _outcome(lease, None, None)  # the recording is gone, and its fetch with it


def _outcome(lease: JobLease, state: str | None, code: str | None) -> FetchOutcome:
    project_id, recording_id = lease.key
    return FetchOutcome(project_id, recording_id, "lost" if state is None else state, code)


def _api_key_ref(config_json: str | None) -> str | None:
    from .connectors.config import connector_config

    try:
        return connector_config(config_json).api_key_ref
    except ValueError:
        return None


class FetchRunner:
    """Run fetch jobs: claim, fetch under the guard, stream into the blob store."""

    def __init__(
        self,
        fetches: FetchStore,
        *,
        recordings: RecordingStore,
        fetcher: RecordingFetcher | None = None,
        secrets: SecretResolver | None = None,
        allowlists: Mapping[str, HostAllowlist] | None = None,
    ) -> None:
        from .connectors.kernel import EnvironmentSecretResolver

        if recordings.blobs is None:
            raise ValueError("fetching recordings needs a blob store")
        self.fetches = fetches
        self.recordings = recordings
        self.fetcher = fetcher or RecordingFetcher()
        self.secrets = secrets or EnvironmentSecretResolver()
        self.allowlists = (
            dict(allowlists)
            if allowlists is not None
            else payload_allowlists(os.environ.get(ALLOWED_HOSTS_ENVIRONMENT))
        )

    @property
    def lease_nano(self) -> int:
        return round((self.fetcher.limits.total_timeout_seconds + LEASE_MARGIN_SECONDS) * _SECOND)

    def run_next(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> FetchOutcome | None:
        """Claim and run one fetch; ``None`` when none is due. ``FetchStopped`` when
        the runner is closed meanwhile (the next start releases the lease)."""

        claimed = self.fetches.claim(
            lease_nano=self.lease_nano, now_unix_nano=now_unix_nano, budget_s=budget_s
        )
        if not isinstance(claimed, FetchClaim):
            return claimed
        claim = claimed
        try:
            return self._fetch(claim, self._request(claim), budget_s)
        except FetchError as error:
            state = error.outcome
            if state == GONE:
                state = RETRY if claim.source_kind == PROVIDER_API else EXPIRED
            outcome = self.fetches.fail(claim, error.code, state=state, budget_s=budget_s)
            _LOGGER.log(
                logging.INFO if outcome.result == "pending" else logging.WARNING,
                "EARSHOT_RECORDING_FETCH_FAILED: a recording fetch from %s failed with %s (%s)",
                error.host or "no host",
                outcome.error_code,
                outcome.result,
            )
            return outcome

    def close(self) -> None:
        """Stop the fetches in flight (the server is stopping)."""

        self.fetcher.close()

    # --- helpers ------------------------------------------------------------------------

    def _request(self, claim: FetchClaim) -> FetchRequest:
        if claim.source_kind == PROVIDER_API:
            api = PROVIDER_APIS.get(claim.provider)
            path = None if api is None else api.paths.get(claim.track)
            if api is None or path is None:
                raise FetchError(FETCH_SOURCE_UNSUPPORTED, FAILED, None, "no API for it")
            if not PROVIDER_ID.fullmatch(claim.provider_call_id):
                raise FetchError(FETCH_ID_INVALID, FAILED, api.host, "not a plain ID")
            keys = () if claim.api_key_ref is None else self.secrets.resolve(claim.api_key_ref)
            if not keys:
                raise FetchError(FETCH_KEY_MISSING, RETRY, api.host, "no API key is set")
            return FetchRequest(
                f"https://{api.host}{path.format(id=quote(claim.provider_call_id, safe=''))}",
                allowed_hosts=None,
                credential=Credential(api.host, api.header, api.value.format(key=keys[0])),
            )
        if claim.url is None:
            raise FetchError(FETCH_URL_INVALID, FAILED, None, "no URL")
        allowed = self.allowlists.get(claim.provider) or HostAllowlist(())
        return FetchRequest(claim.url, allowed_hosts=allowed)

    def _fetch(
        self, claim: FetchClaim, request: FetchRequest, budget_s: float | None
    ) -> FetchOutcome:
        from .storage_recordings import (
            PresetForbidsMediaError,
            RecordingConflictError,
            RecordingNotFoundError,
        )

        recordings = self.recordings
        project_id, recording_id = claim.project_id, claim.recording_id
        with self.fetcher.open(request) as response:
            try:
                attempt = recordings.begin_upload(project_id, recording_id, budget_s=budget_s)
            except PresetForbidsMediaError:
                raise FetchError(
                    FETCH_PRESET_FORBIDS_MEDIA, FAILED, response.host, "metadata_only"
                ) from None
            except (RecordingNotFoundError, RecordingConflictError):
                return self.fetches.settle(claim, budget_s=budget_s)
            except StorageBusyError:
                raise FetchError(FETCH_STORE_FAILED, RETRY, response.host, "busy") from None
            if attempt.blob_key is None:  # already stored
                return self.fetches.settle(claim, budget_s=budget_s)
            blob_key = attempt.blob_key
            stored = None
            try:
                sha256, size, duration_ms = self._stream(response, blob_key, claim.content_type)
                try:
                    stored = recordings.finish_upload(
                        project_id,
                        recording_id,
                        blob_key,
                        sha256=sha256,
                        size=size,
                        duration_ms=duration_ms,
                        budget_s=budget_s,
                    )
                except StorageBusyError:
                    raise FetchError(FETCH_STORE_FAILED, RETRY, response.host, "busy") from None
            finally:
                if stored is None:
                    recordings.discard_upload(project_id, blob_key, budget_s=budget_s)
                recordings.end_upload(project_id, blob_key)
        if stored is None:  # the recording stopped being pending (purged, say)
            return self.fetches.settle(claim, budget_s=budget_s)
        if not self.fetches.complete_stored(claim, budget_s=budget_s):
            return _outcome(claim.lease, None, None)
        return _outcome(claim.lease, STORED, None)

    def _stream(
        self, response: FetchResponse, blob_key: str, content_type: str
    ) -> tuple[str, int, int | None]:
        """Stream the body into ``blob_key`` and commit it: ``(sha256, size, duration_ms)``."""

        assert self.recordings.blobs is not None
        inspector = media_inspector(content_type)
        host = response.host
        size = 0
        with self.recordings.blobs.begin_put(blob_key) as writer:
            for chunk in response.chunks():
                if inspector is not None:
                    try:
                        inspector.feed(chunk)
                    except RecordingHeaderError:
                        raise FetchError(FETCH_NOT_AUDIO, FAILED, host, "bad header") from None
                try:
                    writer.write(chunk)
                except Exception:
                    raise FetchError(FETCH_STORE_FAILED, RETRY, host, "blob write") from None
                size += len(chunk)
            if size == 0:
                raise FetchError(FETCH_NOT_AUDIO, FAILED, host, "an empty body")
            duration_ms = None
            if inspector is not None:
                try:
                    inspector.finish(size)
                except RecordingHeaderError:
                    raise FetchError(FETCH_NOT_AUDIO, FAILED, host, "bad header") from None
                duration_ms = inspector.duration_ms()
            try:
                sha256, stored_size = writer.commit()
            except Exception:
                raise FetchError(FETCH_STORE_FAILED, RETRY, host, "blob commit") from None
        return sha256, stored_size, duration_ms


# --- the maintenance job ------------------------------------------------------------------------


class _FetchJob:
    """``recording_fetch_job``'s job: up to ``FETCH_CONCURRENCY`` fetches at once,
    each on a worker thread, never under a store lock but their short mutations."""

    def __init__(self, runner: FetchRunner, *, concurrency: int = FETCH_CONCURRENCY) -> None:
        self.runner = runner
        self.concurrency = concurrency
        self._running: set[asyncio.Task[None]] = set()
        self._started = False

    async def __call__(self, now_unix_nano: int) -> float | None:
        fetches = self.runner.fetches
        try:
            if not self._started:
                await asyncio.to_thread(
                    fetches.release_leases,
                    now_unix_nano=now_unix_nano,
                    budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
                )
                self._started = True
            free = self.concurrency - len(self._running)
            if free <= 0 or not await asyncio.to_thread(fetches.due, now_unix_nano=now_unix_nano):
                return None
        except StorageBusyError:
            return None
        for _ in range(free):
            task = asyncio.create_task(self._run(now_unix_nano))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
        return None

    async def _run(self, now_unix_nano: int) -> None:
        try:
            await asyncio.to_thread(
                self.runner.run_next,
                now_unix_nano=now_unix_nano,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
        except (StorageBusyError, FetchStopped):
            return  # tried again later; a stopped fetch's lease is released at the next start
        except Exception as error:
            _LOGGER.error("a recording fetch failed (%s)", type(error).__name__)

    async def aclose(self) -> None:
        self.runner.close()
        if self._running:
            with contextlib.suppress(Exception):
                await asyncio.wait(set(self._running), timeout=CLOSE_GRACE_SECONDS)


def recording_fetch_job(
    store: IncidentStore, *, recordings: RecordingStore, max_bytes: int
) -> MaintenanceJob:
    """A maintenance job: fetch provider recordings into the blob store, two at a time.

    Its first run returns the leases a previous process left to pending. Each tick
    starts fetches while some are due and a slot is free; the loop's shutdown stops
    the fetches in flight (``aclose``), whose leases the next start releases.
    """

    runner = FetchRunner(
        FetchStore(store),
        recordings=recordings,
        fetcher=RecordingFetcher(limits=FetchLimits(max_bytes=max_bytes)),
    )
    return fetch_job(runner)


def fetch_job(runner: FetchRunner) -> MaintenanceJob:
    """The maintenance job ``recording_fetch_job`` describes, on ``runner``."""

    return _FetchJob(runner)
