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

The `0.1.0` publication is wheel-only. Do not upload an sdist until Hatch has an
explicit sdist include/exclude policy and CI proves the source archive is reproducible
from both a clean tree and a post-build tree.

`earshot-observability` returned no public PyPI project when this decision was made
on 2026-07-13. A missing project is not a reservation: an owner must reserve the name
before release, and the release operator must recheck ownership immediately before
uploading. If the name cannot be reserved, stop and make an explicit distribution
rename; do not silently fall back to `earshot`.

## Reproducible release check

Run the same gates as CI from a clean checkout, then build and install the wheel into
a fresh environment:

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
VENV_DIR=$(mktemp -d /tmp/earshot-venv.XXXXXX)
python -m pip wheel --no-deps . --wheel-dir "$DIST_DIR"
python -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install "$DIST_DIR"/earshot_observability-*.whl
"$VENV_DIR/bin/python" -c "import earshot; from earshot.api import create_app; assert create_app()"
"$VENV_DIR/bin/earshot" validate fixtures/valid/minimal.json
```

Inspect the built metadata before upload:

```bash
unzip -p "$DIST_DIR"/earshot_observability-*.whl \
  'earshot_observability-*.dist-info/METADATA' | sed -n '1,40p'
```

The metadata `Name` must be `earshot-observability`, while the wheel must contain the
`earshot/` import directory and the `earshot` console entry point. Publication itself
requires the team's package-index credentials and is deliberately not performed by
the build or test workflow.
