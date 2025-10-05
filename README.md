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
- Issue Tracking module (status, criticality, urgency, branch, metrics dashboard)
- Meetings module (agenda scheduling + analytics, modal create/edit)
- Tailwind (CDN) + Soft UI styling
- Public appointment booking portal with bilingual (English/Bangla) copy, email confirmations, and 12-hour reminders (`/booking`)

### Tutor Availability Module (since 0.3.0)

### Issue Tracking Module (since 0.4.0)

### Meetings Module (since 0.5.0)

Route: `/meetings`

Key capabilities:

- CRUD for meetings with fields: Participant (another user), Agenda/Reason, Date, Time, optional Student Name, Parent Name, Outcome, and implicit Booked By (current user).
- Analytics cards summarising: Today (All), Week (All), Today (You), Week (You), Total.
- Filters: Participant, Booked By, Date Range (start/end), text search (agenda substring).
- Modal-based create/edit (AJAX). Full-page fallback retained for accessibility or direct navigation.
- Backfill-safe SQLite schema tweaks for newly added columns (student_name, parent_name, outcome) without external migrations.
- Shared lightweight table sorter & pagination (page-size + sorting) via `static/js/tables.js`.
- Clean separation of form partial (`meetings/partials/_form_inner.html`) enabling reuse in modal and full page.

Route: `/issues`

Key capabilities:

- CRUD for issues with fields: Title, Details, Status (Pending / In Progress / Resolved), Criticality (Minor / Significant / Medium / Critical), Urgency (Low / Medium / High), Branch, Creator metadata.
- Multi-select real-time filters (status, criticality, urgency, branches) + debounced text search.
- Metrics cards: Total, Open, Resolved, Critical Open, High Urgency Open.
- Soft UI + shared table sorter/paginator.
- Badge colour accents for status (Resolved = green, In Progress = amber).

Route: `/availability`

Key capabilities:

- Real-time remote JSON fetch every page view with safe upsert (name + department key) and graceful fallback if remote fails.
- Multi-select dropdown filters: Departments, Branches, Subjects, Days + debounced text search.
- Instant (auto) filter application – no Apply button – minimal reload churn.
- Custom client-side pagination & sorting (shared utility in `static/js/tables.js`) with page-size selector (10/25/50/100/250/All) persisted via `localStorage`.
- Canonical branch normalization (Whitechapel, East Ham, Stratford, Docklands) appended if absent in data so UI options stay consistent.
- Clean Soft UI dropdown pattern reused from Staff page for reliability.

### Versioning

### Tasks Module (since 0.6.0)

Route: `/todos`

Key capabilities:

- CRUD for tasks with fields: Description, Notes, Actions Taken, Criticality (Minor / Significant / Medium / Critical), Urgency (Low / Medium / High), Status (Pending / Done), Due Date, Created By, Assigned To.
- Metrics cards (scoped to current/selected assignee): Open, Done, Overdue, Due in 3 Days, Total.
- Filtering: Assigned To (superadmin can change; others locked to self), single dropdowns for Status, Criticality, Urgency, plus text search.
- Sorting prioritises: Pending first, then higher criticality & urgency, then earliest due date, then newest created.
- Inline AJAX status update (select) + full modal create/edit (AJAX) with graceful full-page fallback.
- Overdue highlighting (row tint) and due soon calculation (<=3 days left).
- Access control: Only creator, assignee, or superadmin can edit/delete; visibility limited for non-superadmin users to their own assignments.

Current version (0.6.0) lives in `version_info.py` (`VERSION` constant) and changelog entries in `VERSION.md` are displayed in-app via the version modal.

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
