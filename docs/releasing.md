# Releasing wisen

Pushing a tag like `v0.2.0` runs `.github/workflows/release.yml`. It:

1. fails unless the tag is exactly `v` + the `version` in `pyproject.toml`;
2. builds the sdist and wheel and runs `twine check`;
3. publishes them to PyPI with trusted publishing (no API token is stored anywhere);
4. creates a GitHub Release for the tag with auto-generated notes and the built files attached.

## One-time setup (owner only)

Do both of these once, before the first release.

**PyPI: add a pending trusted publisher.** The `wisen` project doesn't exist on PyPI
yet, so register it as a *pending* publisher: sign in to PyPI, open
<https://pypi.org/manage/account/publishing/>, and fill in:

| Field | Value |
| --- | --- |
| PyPI project name | `wisen` |
| Owner | `Vela3202019` |
| Repository name | `wisen` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The first successful release creates the project and turns the pending publisher
into a normal one.

**GitHub: create the `pypi` environment.** In the repo, go to
Settings → Environments → New environment and name it `pypi`. Optionally add
yourself as a required reviewer so every publish waits for your approval, and
limit deployment to tags matching `v*`.

## Each release

```bash
# 1. bump the version in pyproject.toml, e.g. 0.1.0 -> 0.2.0, and merge that to main
git checkout main && git pull
# 2. tag the merged commit and push the tag
git tag v0.2.0
git push origin v0.2.0
```

Watch the run under the repo's Actions tab. If the version check fails, delete the
tag (`git push origin :refs/tags/v0.2.0 && git tag -d v0.2.0`), fix the version, and
tag again. PyPI never accepts the same version twice, so a version that was
published can't be re-uploaded — bump to the next one instead.

To check a build locally before tagging:

```bash
pip install build twine
python -m build && twine check dist/*
```
