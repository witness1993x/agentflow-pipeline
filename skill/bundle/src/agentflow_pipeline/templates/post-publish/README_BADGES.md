# README badges

Paste the block below near the top of `README.md`, directly under the project title.

```markdown
[![CI](https://github.com/{{ github_owner }}/{{ repo_name }}/actions/workflows/ci.yml/badge.svg)](https://github.com/{{ github_owner }}/{{ repo_name }}/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/{{ github_owner }}/{{ repo_name }})](./LICENSE)
[![Stars](https://img.shields.io/github/stars/{{ github_owner }}/{{ repo_name }}?style=social)](https://github.com/{{ github_owner }}/{{ repo_name }}/stargazers)
[![Issues](https://img.shields.io/github/issues/{{ github_owner }}/{{ repo_name }})](https://github.com/{{ github_owner }}/{{ repo_name }}/issues)
[![Last commit](https://img.shields.io/github/last-commit/{{ github_owner }}/{{ repo_name }}/{{ default_branch }})](https://github.com/{{ github_owner }}/{{ repo_name }}/commits/{{ default_branch }})
```

Notes:
- The CI badge resolves once `.github/workflows/ci.yml` runs at least once on `{{ default_branch }}`.
- The license badge requires a `LICENSE` file at the repo root.
- Drop or reorder badges to match the project's tone; keep the set minimal.
