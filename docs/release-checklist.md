# Release checklist

The owner's steps for one release. [`release.md`](release.md) explains the packaging and
the automated workflow; `.github/workflows/release.yml` is the only publication path, so
nothing here uploads from a workstation.

One tag publishes four things:

| Registry | Artifact                                 | Job                 | Credential                                 |
| -------- | ---------------------------------------- | ------------------- | ------------------------------------------ |
| PyPI     | `earshot-observability` wheel and sdist  | `publish-pypi`      | OIDC trusted publisher, environment `pypi` |
| GHCR     | `ghcr.io/kkartik14/earshot:<version>`    | `publish-container` | `GITHUB_TOKEN` (`packages: write`)         |
| npm      | `@earshot/browser`, `@earshot/viewer-ui` | `publish-npm`       | npm trusted publishing, environment `npm`  |
| GitHub   | Release with the Python files and notes  | `github-release`    | `GITHUB_TOKEN`                             |

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
   `packages/browser/package.json` and `packages/viewer-ui/package.json`, and add the
   `CHANGELOG.md` entry. The `validate` job rejects a tag that differs from
   `pyproject.toml`, and `build-npm` rejects a package that differs from it.
2. Wait for the comprehensive CI run on `main` to pass. Then start the `ci` workflow
   manually on `main` and wait for its `coverage` job to enforce the combined SQLite
   and Postgres branch-coverage floor.
3. Optional dry run: start the `release` workflow by hand on `main`. It builds and
   inspects the wheel, sdist and npm tarballs, and publishes nothing.
4. Tag and push, as in `release.md`. Approve the `pypi` and `npm` deployments when
   asked. A version containing `-` is published to npm under the `next` dist-tag.

## After publishing

```bash
version=0.2.0   # the released version
npm view "@earshot/viewer-ui@$version" dist.attestations --json
npm view "@earshot/browser@$version" dist.attestations --json
gh attestation verify "oci://ghcr.io/kkartik14/earshot:$version" --repo Kkartik14/Earshot
docker run --rm -p 127.0.0.1:4319:4319 -e EARSHOT_HOST=0.0.0.0 \
  -e EARSHOT_TRUST_LOCAL_NETWORK=true "ghcr.io/kkartik14/earshot:$version"
curl --fail http://127.0.0.1:4319/readyz   # from a second terminal
```

npm and PyPI never accept a version twice, and a pushed image tag must not be moved. If
something is wrong, fix it, increment the version and release again.
