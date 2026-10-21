# Release checklist

The owner's steps for one release. [`release.md`](release.md) explains the packaging and
the automated workflow; `.github/workflows/release.yml` is the only publication path, so
nothing here uploads from a workstation.

One tag publishes four things:

| Registry | Artifact                                                  | Job                 | Credential                                 |
| -------- | --------------------------------------------------------- | ------------------- | ------------------------------------------ |
| PyPI     | `earshot-observability` wheel and sdist                   | `publish-pypi`      | OIDC trusted publisher, environment `pypi` |
| GHCR     | `ghcr.io/kkartik14/earshot:<version>`                     | `publish-container` | `GITHUB_TOKEN` (`packages: write`)         |
| npm      | `@earshot/browser`, `@earshot/viewer-ui`, `@earshot/node` | `publish-npm`       | npm trusted publishing, environment `npm`  |
| GitHub   | Release with the Python files and notes                   | `github-release`    | `GITHUB_TOKEN`                             |

## Once, before the first release

- **PyPI:** create the pending trusted publisher described in `release.md`.
- **npm:** own the `@earshot` scope. Add a trusted publisher to each package with
  repository `Kkartik14/Earshot`, workflow `release.yml` and environment `npm`. If npm
  only allows that after a package exists, store a short-lived granular token with
  publish rights as the `NPM_TOKEN` secret for the first release. Configure trusted
  publishing afterwards and delete the secret.
- **GitHub environments:** give `pypi` and `npm` a required reviewer, and allow only
  `v*` tags to deploy to them.
- **GHCR:** after the first push, link the package to the repository and make it
  public before documenting anonymous `docker pull`.

## Every release

1. Set the same version in `pyproject.toml`, `packages/sdk-python/src/earshot/versions.py`,
   `packages/browser/package.json`, `packages/viewer-ui/package.json` and
   `packages/node/package.json`, and add the `CHANGELOG.md` entry. The `validate` job rejects a tag that differs from
   `pyproject.toml`, and `build-npm` rejects a package that differs from it.
2. Wait for CI on `main` to pass. That includes the `coverage` job, which enforces the
   branch-coverage floor on SQLite and Postgres combined.
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
4. Optional dry run: start the `release` workflow by hand on `main`. It builds and
   inspects the wheel, sdist and npm tarballs, and publishes nothing.
5. Tag and push, as in `release.md`. Approve the `pypi` and `npm` deployments when
   asked. A version containing `-` is published to npm under the `next` dist-tag.

## After publishing

```bash
version=0.2.0   # the released version
npm view "@earshot/viewer-ui@$version" dist.attestations --json
npm view "@earshot/browser@$version" dist.attestations --json
npm view "@earshot/node@$version" dist.attestations --json
gh attestation verify "oci://ghcr.io/kkartik14/earshot:$version" --repo Kkartik14/Earshot
docker run --rm -p 127.0.0.1:4319:4319 -e EARSHOT_HOST=0.0.0.0 \
  -e EARSHOT_TRUST_LOCAL_NETWORK=true "ghcr.io/kkartik14/earshot:$version"
curl --fail http://127.0.0.1:4319/readyz   # from a second terminal
```

npm and PyPI never accept a version twice, and a pushed image tag must not be moved. If
something is wrong, fix it, increment the version and release again.
