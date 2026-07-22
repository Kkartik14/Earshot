# Python release packaging

The installable distribution and Python import intentionally have different names:

```text
distribution: earshot-observability
import:       earshot
CLI:          earshot
```

The [PyPI project named `earshot`](https://pypi.org/project/earshot/) is an unrelated
Rust-backed VAD package. It has already published overlapping versions and also
installs an `earshot` import package. Never upload this repository under that
distribution name, and do not install both projects into one environment because
their import namespaces collide.

The release publishes both a wheel and source distribution. Both artifacts carry the
same compiled viewer produced immediately before the Python build. Hatch has explicit
wheel and sdist artifact rules, and CI rejects either archive when the viewer, generated
protobuf module, package entry point, or required source files are absent.

The base install is the SDK and evidence core only:

```bash
python -m pip install earshot-observability
```

Install the local API, CLI server command, and bundled viewer with the server extra:

```bash
python -m pip install 'earshot-observability[server]'
```

`earshot-observability` returned no public PyPI project when this decision was made
on 2026-07-13. A missing project is not a reservation: an owner must reserve the name
before release, and the release operator must recheck ownership immediately before
uploading. If the name cannot be reserved, stop and make an explicit distribution
rename; do not silently fall back to `earshot`.

## Reproducible release check

Run the same gates as CI from a clean checkout. The viewer, wheel, and sdist are one
artifact graph; do not build or upload the Python archives without the viewer bundle
step:

```bash
python scripts/generate_contract.py --check
python scripts/generate_openapi.py --check
python scripts/check_semconv.py
ruff check packages/sdk-python/src packages/sdk-python/tests apps/ingest scripts examples
pytest --cov=earshot --cov-report=term-missing -q
pnpm test
pnpm typecheck
pnpm build
pnpm format:check

DIST_DIR=$(mktemp -d /tmp/earshot-dist.XXXXXX)
python -m pip install 'build>=1.2,<2'
pnpm --filter @earshot/viewer bundle
python -m build --wheel --sdist --outdir "$DIST_DIR"
python scripts/check_wheel.py "$DIST_DIR"/*.whl
python scripts/check_sdist.py "$DIST_DIR"/*.tar.gz
python scripts/smoke_artifact.py "$DIST_DIR"/*.whl
python scripts/smoke_artifact.py "$DIST_DIR"/*.tar.gz
```

Each smoke command creates an isolated environment, installs the base artifact, proves
FastAPI and Uvicorn are absent, exercises the public SDK and CLI, installs the same
artifact with `[server]`, then starts the real server and fetches the API, viewer index,
and one compiled viewer asset. For an sdist this also proves that its isolated wheel
build retains the viewer.

Inspect the built metadata before upload:

```bash
unzip -p "$DIST_DIR"/earshot_observability-*.whl \
  'earshot_observability-*.dist-info/METADATA' | sed -n '1,40p'
```

The metadata `Name` must be `earshot-observability`; FastAPI and Uvicorn must appear
only behind extras including `server`; and the wheel must contain the `earshot/` import
directory and `earshot` console entry point. Publication itself requires the team's
package-index credentials and is deliberately not performed by the build or test
workflow.

## Supported versions and deprecation policy

This release tests CPython 3.11, 3.12, and 3.13. Linux is the primary full-suite target;
the compatibility workflow also runs a framework smoke on macOS. The `Requires-Python`
floor remains `>=3.11`; newer interpreters outside the tested matrix are unverified, not
prohibited. Pipecat support is `>=1.5.0,<1.6`, LiveKit Agents support is
`>=1.6.5,<1.7`, and standalone OpenTelemetry API/SDK support is `>=1.30,<2`. CI tests
the exact Pipecat/LiveKit lower bounds and the newest resolver-selected versions within
those ranges in separate jobs. Framework dependencies can impose a higher effective
OpenTelemetry floor than the standalone extra. A release must not claim compatibility
from a developer's single local environment alone.

Windows is unverified in `0.1.0` and is not a supported durable-spool target. Directory
fsync, process creation, filesystem permissions, and service behavior require a dedicated
compatibility lane before that claim can change.

Dropping a tested Python version or raising a framework minimum is a public support
change. It must be announced in release notes and deprecated for at least one minor
release before removal; a security or upstream end-of-life exception must be called out
explicitly. Public imports, accepted adapter input shapes, persisted canonical bundles,
semantic codes, and privacy defaults must not be silently removed or repurposed. A
deprecated API emits `DeprecationWarning` with its replacement and planned removal
release, remains covered by compatibility tests during the window, and is removed only
in a declared breaking release. Newer readers must continue to decode supported
historical bundles; schema evolution uses an explicit version/normalizer rather than
rewriting stored evidence in place.
