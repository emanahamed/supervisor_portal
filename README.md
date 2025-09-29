# Excel Tutors — Observation Tracker (Flask + Tailwind Soft UI)

A modern, responsive webapp to manage authentication (with superadmin approval), staff CRUD with import/export, observation cycles, and observation records grouped by cycle.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000/

**Superadmin (auto-created on first run):**

- Email: `@exceltutors.org.uksuperadmin`
- Password: `superadmin123`

## Features

- User registration; superadmin must approve accounts
- Password reset via email (uses constants in `email_utils.py`)
- Staff table with search/filter, CSV export, and guided import
- Observation cycles and per-cycle observation CRUD
- Per-tutor observation counts within a cycle to check "0/1/2/3 or more" quickly
- Tutor Availability module (real-time synced, multi-filter, custom pagination)
- Tailwind (CDN) + Soft UI styling

### Tutor Availability Module (since 0.3.0)

Route: `/availability`

Key capabilities:

- Real-time remote JSON fetch every page view with safe upsert (name + department key) and graceful fallback if remote fails.
- Multi-select dropdown filters: Departments, Branches, Subjects, Days + debounced text search.
- Instant (auto) filter application – no Apply button – minimal reload churn.
- Custom client-side pagination & sorting (shared utility in `static/js/tables.js`) with page-size selector (10/25/50/100/250/All) persisted via `localStorage`.
- Canonical branch normalization (Whitechapel, East Ham, Stratford, Docklands) appended if absent in data so UI options stay consistent.
- Clean Soft UI dropdown pattern reused from Staff page for reliability.

### Versioning

Current version lives in `version_info.py` (`VERSION` constant) and changelog entries in `VERSION.md` are displayed in-app via the version modal.

To bump version:

1. Update `VERSION` in `version_info.py`.
2. Add a new heading & bullet list to `VERSION.md`.
3. (Optional) Reference the new changes in README if it adds user-facing features.

### Tech Notes

- Database: SQLite (development). Migrate to Postgres/MySQL for production workloads.
- ORM: SQLAlchemy (classic) with simple session usage—no Alembic migrations yet.
- Auth: `flask-login`; approval gate for new accounts (`is_approved`).
- Styling: Tailwind CDN + light custom Soft UI utility classes (see `base.html`).
- Tables: Lightweight custom sorter + paginator (`static/js/tables.js`).
- Remote Sync (Availability): HTTP GET to external endpoint, 12s timeout, error captured and surfaced non-fatally.

### Roadmap Ideas

- Add server-side pagination for very large availability datasets.
- Introduce caching or ETag-based conditional remote sync.
- Subject taxonomy normalization & tagging UI.
- Export (CSV) for availability data.
- Role-based visibility rules for availability notes.

> For production, change `SECRET_KEY`, consider a real mail provider, and move to a managed DB.
