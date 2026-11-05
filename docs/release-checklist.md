# Release checklist

The owner's steps for one release. [`release.md`](release.md) explains the packaging and
the automated workflow; `.github/workflows/release.yml` is the only publication path, so
nothing here uploads from a workstation.

One tag publishes four things:

| Registry | Artifact                                | Job                 | Credential                                 |
| -------- | --------------------------------------- | ------------------- | ------------------------------------------ |
| PyPI     | `earshot-observability` wheel and sdist | `publish-pypi`      | OIDC trusted publisher, environment `pypi` |
| GHCR     | `ghcr.io/kkartik14/earshot:<version>`   | `publish-container` | `GITHUB_TOKEN` (`packages: write`)         |
| npm      | `earshot-observability`                 | `publish-npm`       | npm trusted publishing, environment `npm`  |
| GitHub   | Release with the Python files and notes | `github-release`    | `GITHUB_TOKEN`                             |

## Once, before the first release

- **PyPI:** create the pending trusted publisher described in `release.md`.
- **npm:** configure Trusted Publishing for `earshot-observability` with owner
  `Kkartik14`, repository `Earshot`, workflow `release.yml` and environment `npm`. Enable
  direct publishing for the publisher. Use two-factor authentication and do not create a
  publishing token for GitHub Actions. Complete the first trusted publish within two days
  of configuring the publisher; then enable npm's “Require two-factor authentication and
  disallow tokens” package setting.
- **GitHub environments:** give `pypi` and `npm` a required reviewer, and allow only
  `v*` tags to deploy to them. Set the `npm` environment URL to
  `https://www.npmjs.com/package/earshot-observability`.
- **Release tags:** create two active GitHub tag rulesets targeting `v*`. In one, restrict
  tag creation and allow only the release maintainer to create tags. In the other, restrict
  tag updates and deletions with no bypass actors. Do not combine these rules: a creation
  bypass must not authorize moving or deleting an existing release tag. See
  [GitHub's tag ruleset controls](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
- **GHCR:** restrict package write access to release maintainers and the audited release
  workflow. After the first push, link the package to the repository and make it public
  before documenting anonymous `docker pull`.

## Every release

1. Set the release version in `pyproject.toml`, `packages/sdk-python/src/earshot/versions.py`,
   `packages/browser/package.json`, `packages/viewer-ui/package.json` and
   `packages/node/package.json`, regenerate `uv.lock` so its root package entry stays
   aligned, and add the `CHANGELOG.md` entry. Only `earshot-observability` is published to
   npm; its name and version are checked against the Python project version.
2. Wait for the comprehensive CI run on `main` to pass. Then start the `ci` workflow
   manually on `main` and wait for its `coverage` job to enforce the combined SQLite
   and Postgres branch-coverage floor.
3. Run the release gates CI cannot run, and record their results:
   - the real-provider e2e, one real Pipecat call and one real LiveKit 1.8 call
     (`EARSHOT_LIVEKIT_PYTHON` names a venv with `examples/livekit_otlp/requirements.txt`
     and Earshot installed); see [development](./development.md#run-tests):

     ```bash
     (set -a; . ./.env; set +a; EARSHOT_REQUIRE_REAL_PROVIDERS=1 \
       EARSHOT_LIVEKIT_PYTHON=/path/to/livekit-venv/bin/python pytest -q -m real_providers)
     ```

   - the player in Chrome and in Safari: `python scripts/viewer_smoke.py --keep-serving`
     serves both replayed calls and prints their URLs. On each, the lanes render,
     playback starts, a transcript click seeks within 100 ms and the highlight follows
     playback. The LiveKit call checks Safari with Ogg Opus.
4. Optional dry run: dispatch the `release` workflow on a branch such as `main`. It builds
   and inspects the wheel, sdist and Node npm tarball, and publishes nothing. Dispatching
   on a tag runs the publishing jobs, so never use a tag dispatch as a dry run.
5. Tag and push, as in `release.md`. Approve the `pypi` and `npm` deployments when
   asked. A version containing `-` is published to npm under the `next` dist-tag.
   Set the shell variable `version` to the exact value from step 1. Before creating the
   tag, confirm the GitHub tag rulesets and both registry version preflights:

   ```sh
   npm view --prefer-online --registry=https://registry.npmjs.org \
     "earshot-observability@$version" version
   ```

   npm must return `E404` for that exact version (not a network or authentication error),
   and the PyPI version must have no files attached. The `v*` tag must not already exist.

## After publishing

```bash
version=0.4.0   # replace with the released version
npm view "earshot-observability@$version" dist.attestations --json
gh attestation verify "oci://ghcr.io/kkartik14/earshot:$version" --repo Kkartik14/Earshot
docker run --rm -p 127.0.0.1:4319:4319 -e EARSHOT_HOST=0.0.0.0 \
  -e EARSHOT_TRUST_LOCAL_NETWORK=true "ghcr.io/kkartik14/earshot:$version"
curl --fail http://127.0.0.1:4319/readyz   # from a second terminal
```

An npm package version cannot be reused. PyPI may accept additional files for an existing
version, but the supported recovery is a same-run retry of the inspected files described
above; do not add or replace release files manually. Version-specific GHCR tags (for
example, `0.4.0`) must not be retargeted. The workflow's `major.minor` alias (for
example, `0.4`) and `latest` tag intentionally advance on stable releases; prereleases
do not move those aliases. If published version-specific bytes are wrong, fix them and
release a new version.
