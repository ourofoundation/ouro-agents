# Publishing

`make release`, then commit and push to `main`. GitHub Actions publishes that
version to PyPI if it is not already there. No tag required.

```bash
make release              # bump patch in pyproject.toml
make release minor        # bump minor
make release major        # bump major

git add pyproject.toml uv.lock
git commit -m "Bump version"
git push
```
