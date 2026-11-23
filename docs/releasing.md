# Releasing wisen

Pushing a tag like `v0.3.0` runs `.github/workflows/release.yml`. It:

1. fails unless the tag is exactly `v` + the `version` in `pyproject.toml`;
2. builds the sdist and wheel and runs `twine check`;
3. publishes them to PyPI with trusted publishing (no API token is stored anywhere);
4. creates a GitHub Release for the tag with auto-generated notes and the built files attached.

## Trusted publishing settings (owner only)

The [wisen project on PyPI](https://pypi.org/project/wisen/) already has a
trusted publisher configured. If you need to verify or restore it, check the
project's [publishing settings](https://pypi.org/manage/project/wisen/settings/publishing/)
against these values:

| Field | Value |
| --- | --- |
| PyPI project name | `wisen` |
| Owner | `Vela3202019` |
| Repository name | `wisen` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The workflow uses the `pypi` environment under the repository's
Settings → Environments. You can require a reviewer before publishing and
restrict deployments to tags matching `v*`.

## Each release

```bash
# 1. bump the version in pyproject.toml, wisen/__init__.py, plugin/.claude-plugin/plugin.json
#    and .claude-plugin/marketplace.json, e.g. 0.2.0 -> 0.3.0 (the release fails unless all four match
#    the tag), and merge that to main
git checkout main && git pull
# 2. tag the merged commit and push the tag
git tag v0.3.0
git push origin v0.3.0
```

Watch the run under the repo's Actions tab. If the version check fails, delete the
tag (`git push origin :refs/tags/v0.3.0 && git tag -d v0.3.0`), fix the version, and
tag again. PyPI never accepts the same version twice, so a version that was
published can't be re-uploaded — bump to the next one instead.

To check a build locally before tagging:

```bash
pip install build twine
python -m build && twine check dist/*
```
