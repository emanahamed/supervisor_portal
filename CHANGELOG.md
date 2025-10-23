# Changelog

This project maintains its version history in `VERSION.md` (reverse chronological).

Why `VERSION.md` and not `CHANGELOG.md`?

- Early releases adopted `VERSION.md` as the canonical changelog, and in-app readers and APIs reference it directly.
- To avoid duplication and drift, `CHANGELOG.md` simply points to `VERSION.md`.

## Where to read the changelog

- In the app: /version-history (renders a friendly view)
- Raw file: VERSION.md
- API (JSON):
  - GET /api/version — current version and entry
  - GET /api/changelog?limit=5 — parsed entries (newest first)

## How to bump versions

- Edit `version_info.py` → update `VERSION` (semantic MAJOR.MINOR.PATCH)
- Append a new section to `VERSION.md` at the top (newest first). Follow the existing style (headline + bullets; optional notes/migrations/tests).
- Optionally, summarize highlights in README under “Release Notes (Latest Highlights)”.
- Git commit the changes and (optionally) tag the release: `git tag vX.Y.Z && git push --tags`.
