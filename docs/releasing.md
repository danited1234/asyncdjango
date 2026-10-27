# Releasing

`.github/workflows/release.yml` runs when a tag starting with `v` is pushed.
It runs the full test matrix, checks that the tag equals `v` plus the
version in `pyproject.toml`, builds the sdist and wheel, and attaches them to
a GitHub release. Tags with `a`, `b`, `rc`, or `dev` are marked as
pre-releases.

## Cutting a release

1. Set `version` in `pyproject.toml` (for example `0.1.0a1`) and commit.
2. Tag and push:

   ```console
   git tag v0.1.0a1
   git push origin v0.1.0a1
   ```

A PyPI version can never be uploaded twice, even after deleting it. If a
release job fails after the upload, bump the version instead of retagging.

## Publishing to PyPI

The `pypi` job is skipped unless the repository variable `PYPI_PUBLISH` is
`true`. Before turning it on:

1. **Note that the source becomes public.** The sdist on PyPI contains the
   whole package (BSD-3-Clause, `LICENSE`), even while this GitHub
   repository is private.
2. **Register a trusted publisher** on pypi.org (Account settings →
   Publishing → Add a pending publisher): project `asyncdjango`, owner
   `danited1234`, repository `asyncdjango`, workflow `release.yml`,
   environment `pypi`. No API token is stored in GitHub.
3. **Create the `pypi` environment** in the GitHub repository settings.
   Adding yourself as a required reviewer makes every upload wait for
   approval.
4. Set the repository variable `PYPI_PUBLISH` to `true`.

To try the process first, register the same publisher on test.pypi.org and
add `repository-url: https://test.pypi.org/legacy/` to the publish step.

## Installing in a project

From PyPI, pinned to one release:

```text
asyncdjango[drf]==0.1.0a1
```

Without PyPI, from the GitHub release. While the repository is private, the
installing machine needs a token with read access to it:

```text
asyncdjango[drf] @ git+https://github.com/danited1234/asyncdjango.git@v0.1.0a1
```
