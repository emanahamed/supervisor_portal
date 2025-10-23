# Excel Tutors Platform — Supervisor / Operations Portal

Comprehensive Flask-based operations portal for tutoring centres: observations & reporting, staff & availability management, issues & tasks, appointments, invoicing, permissions, and internal error reporting — all wrapped in a modern Tailwind Soft UI skin.

---

## 1. Quickstart

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py  # or: FLASK_APP=app.py flask run
```

Open: http://127.0.0.1:5000/

Default superadmin (auto-seeded if no users exist):

- Email: `superadmin@exceltutors.org.uk`
- Password: `superadmin123`

> Change this immediately in production (Profile → Update password) and set a secure `SECRET_KEY`.

---

## 2. High-Level Feature Matrix

| Domain                        | Key Capabilities                                                                                                     | Notable Details                                                                      |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Authentication & Users        | Registration + approval, profile pictures, password reset, per-user theme (light/dark/system)                        | Superadmin approval gate, caps-lock + visibility toggle UX, system theme auto-detect |
| Permissions & Roles           | Fine-grained permission table, role grants, per-user allow/deny overrides, audit log                                 | Superadmin bypass; audit via `PermissionAudit`; role taxonomy upgrades (0.9.x)       |
| Staff Management              | CRUD, CSV import (guided), CSV export, filtering/search                                                              | Import normalization (see `utils.py`), soft activation flag                          |
| Observation Cycles & Reports  | Cycles, extended observation detail with structured checklists, PDF & Email reporting                                | Normalized checklist keys, JSON storage, Timeslot metadata (0.9.6)                   |
| Availability                  | Remote sync + multi-filter (departments, branches, subjects, days), instant filtering, client pagination             | Canonical branch injection; debounced filter application                             |
| Issues (Tracker)              | CRUD, multi-select filters (status/criticality/urgency/branch), change log audit, modal create/edit                  | Recent change inline expansion, badge theming, AJAX modal (0.9.7 UI refresh)         |
| Tasks (To‑Dos)                | Assignee-focused dashboard metrics, filtering, modal create/edit, inline status update                               | Ordering heuristic (status → criticality → urgency → due date → recency)             |
| Meetings                      | Participant scheduling, analytics (today/week/user), modal CRUD                                                      | Auto backfill of schema; partial reuse for forms                                     |
| Appointments (Public + Admin) | Public bilingual booking, email confirmation + reminder, admin slot CRUD (single & bulk), cancellation flows         | APScheduler background reminder jobs; permission-gated admin UI                      |
| Invoicing                     | Invoice creation, print-friendly HTML → browser PDF, email invoice action, company aggregates                        | Inline print CSS, per-company stats, email reuse of HTML template                    |
| Error Reporting               | User/system issue capture, automatic 500 traceback reporting, screenshot upload, status workflow, dedupe fingerprint | Pagination, traceback truncation + expand, reporter notification on resolve (0.9.7)  |
| Email Utilities               | Task assignment, appointment confirmation/reminder, invoice dispatch                                                 | Plain HTML with consistent button + typography styling                               |
| Drag & Drop Enhancements      | Attendance fix upload, error screenshot                                                                              | Accessible, validation & preview feedback                                            |

---

## 3. Detailed Module Descriptions

### 3.1 Authentication & Account Lifecycle

- Registration produces unapproved user (`is_approved=False`).
- Superadmin dashboard surfaces pending approvals.
- Password reset: token signing via `itsdangerous`; forms in `templates/auth/*`.
- Theme modes: Light / Dark / System. Quick dropdown updates persist instantly (AJAX) while profile form stores preference server-side; `system` tracks OS setting live.

### 3.2 Permissions & Audit (since 0.9.0+)

- Data model: `Permission`, `RolePermission`, `UserPermission`, `PermissionAudit`.
- Hierarchy: superadmin → user override (deny > allow) → role grant → implicit deny.
- User overrides UI + Role matrix in Admin section.
- Audit records allow/deny/inherit changes & role add/remove.

### 3.3 Staff & Import/Export

- Import pipeline (attendance/availability style): Pandas ingest + normalization (`normalize_staff_dataframe`).
- CSV export retains stable column order for re-import diffing.

### 3.4 Observation & Extended Reports (0.7.0+; refined 0.9.6)

- `ObservationDetail` holds structured JSON checklists & narrative fields.
- Normalization centralised (`checklist_utils.py`) eliminating variant key drift.
- Report outputs: unified email & PDF (xhtml2pdf) with consistent tick/cross visuals; Timeslot added (0.9.6).
- Migration script `migrate_checklists.py` canonicalises historical data with backup.

### 3.5 Availability Module

- Multi-select filters with debounced apply; custom pagination & sort.
- Automatic remote sync each view (idempotent upsert) + graceful error messaging.

### 3.6 Issues Tracker (0.4.x → 0.9.7 modal refresh)

- Fields: Title, Details, Status, Criticality, Urgency, Branch, Action Taken.
- Inline recent change log (last 5 edits) via `IssueChange` rows.
- Responsive modal-based create/edit (AJAX) with accessible lifecycle (focus return, ESC close) added in 0.9.7.

### 3.7 Tasks / To‑Dos (0.6.0)

- Dashboard metrics highlight workload distribution & urgency.
- Inline status select fires small POST for immediate feedback.

### 3.8 Meetings (0.5.0)

- Combines participant & booked_by roles for accountability.
- Partial form reuse ensures consistent modal/full-page flows.

### 3.9 Appointments (0.9.5)

- Public booking localized to English/Bangla; reminder 12h pre-start via APScheduler.
- Admin: slot grid (upcoming vs past), bulk generation (duration slicing), overlap detection.

### 3.10 Invoicing (0.9.4)

- Print mode triggered by `?print=1` prompts browser PDF workflow.
- Company aggregates computed in-memory for filtered subset.

### 3.11 Error Reporting (0.9.7)

- Automatic capture for 500 errors (traceback + request context cached server-side).
- Manual user reports via global modal.
- SHA-256 fingerprint groups duplicates; reporter comment appended.
- Traceback truncated & expandable to preserve UI performance.

### 3.12 Attendance Fix Upload UX (0.9.7)

- Replaces basic file input with drag & drop + validation; blocks submit until valid file selected.

### 3.13 Theming & UI

- Tailwind CDN, soft cards, badge semantics for statuses & priorities.
- Centralized theme utility (`static/js/theme.js`) applies user/server/system preference with live OS change listener and smooth color transitions.
- Lightweight table sorter/paginator (no heavy DataTables dependency).

### 3.14 Time & Timezone Handling

- Transition underway to `datetime.now(timezone.utc)` for awareness; coercion added to appointment comparisons to avoid naive/aware errors (post 0.9.7 patch).

---

## 4. Release Notes (Latest Highlights)

The full changelog lives in `VERSION.md` and is rendered inside the app (Profile → View Changelog). Key recent releases:

### 0.9.9 – Students Module & List UX

- New Students module (CRUD + import/export) with field-level audit log (`StudentChange`).
- Bulk activate / inactivate, pagination, server-side multi-key sorting (Active first → primary → ID).
- Tailwind restyle of list & detail pages; status badges + last-change summary.
- Clickable sortable headers (ID / Name / Year / Status) with direction indicators.
- Colored status badges (Active / Inactive / Pending / Withdrawn) and condensed detail header change snippet.
- Error report form suppressed on student pages via overridable block.
- Audit test added (`test_students_audit.py`).

### 0.9.7 – Error Reporting & UX Polishing

- Error reporting system (model, routes, modal, screenshot upload, fingerprint dedupe, pagination, truncated traceback display).
- Global “Report Issue” modal + integrated 500 page auto-report path.
- Attendance drag & drop upload redesign.
- Issue creation/edit modal + improved accessibility.
- Timezone-aware runtime adjustments in core flows.

### 0.9.6 – Observation Checklist Reliability

- Unified normalization & migration script with backup.
- PDF/email parity improvements; Timeslot metadata.

### 0.9.5 – Appointments & Bilingual Booking

- Public booking portal (EN/BN), admin slot management, automated reminders.

### 0.9.4 – Invoicing Overhaul

- Print-friendly invoice & email sending; per-company aggregate metrics.

### 0.9.0–0.9.3 – Permissions & Role Taxonomy

- Fine-grained permission model, audit logging, role upgrades (Supervisor / Centre Manager / Admin / Super Admin).

### 0.8.0 – Auth Experience Upgrade

- Modernized login/register screens, dark mode, caps lock indicator.

### 0.7.0 – Extended Observation Module

- Structured extended reporting & unified PDF/email.

Earlier versions: see `VERSION.md` for full history.

#### Recent (2.0.8 / 2.0.9) highlights

- Manager review controls for staff invoices (Accept / Reject) with rejection reason modal and branded notification emails.
- Admin UI to manage National Minimum Wage (NMW) bands and selection shown on invoice detail pages.
- Invoice template fixes: consolidated totals and more reliable bank detail rendering in detail and PDF/print outputs.
- DB safety helper added to back up and add missing nullable `staff` columns when required.

---

## 5. Versioning & Bumping

1. Edit `VERSION` in `version_info.py` (semantic-ish minor bump for new modules / notable features).
2. Append a new section to `VERSION.md` (reverse chronological order) with: headline, bullet list of features, follow-ups, migration notes.
3. Summarize major user-facing pieces (top 1–2) in README Release Notes if impactful (keep concise, point deeper details to `VERSION.md`).
4. Add/adjust tests for new behaviors (e.g., audit, sorting, bulk ops) before bump if possible.
5. Git tag recommended: `git tag vX.Y.Z && git push --tags` (not yet automated).

### Version endpoints & CLI

- Web:

  - `/version-history` — pretty version list (newest first)
  - `/api/version` — current version + current entry
  - `/api/changelog?limit=5` — parsed changelog entries (JSON)

- CLI (from this folder):
  - `flask version` — prints current version and notes
  - `flask changelog --limit 5` — prints latest entries

See also `CHANGELOG.md` (pointer) and the canonical `VERSION.md` file.

---

## 6. Tech Stack & Architecture Notes

- Flask 3.x app; SQLite default (swap to Postgres for production) – no Alembic yet (runtime auto-migration patterns for new tables/columns).
- ORM: SQLAlchemy 2.x patterns embedded gradually (use of `Session.get` via Flask-SQLAlchemy).
- Background jobs: APScheduler (optional import fallback) for appointment reminders.
- Email: SMTP helper in `email_utils.py` (central composition functions per feature domain).
- Assets: Minimal custom JS (table utilities, modals, drag & drop) for low bundle size.
- Deployment: Provide `SECRET_KEY` and environment email creds; consider WSGI (gunicorn / waitress) + reverse proxy (nginx) and persistent DB.

---

## 7. Testing

- Pytest suite (initial coverage: checklist normalization, company JSON endpoint) — extend with:
  - Error reporting dedupe & status change notifications
  - Appointment overlap & reminder scheduling
  - Issue & task modal AJAX flows

Run tests:

```bash
pytest -q
```

---

## 8. Operational Hardening Checklist (Recommended)

- [ ] Replace default superadmin credentials
- [ ] Set secure `SECRET_KEY` via environment variable / config
- [ ] Migrate DB to Postgres & add backups
- [ ] Configure real SMTP + bounce monitoring
- [ ] Add HTTPS termination (reverse proxy / load balancer)
- [ ] Implement rate limiting (Flask-Limiter) on auth & reporting forms
- [ ] Expand test coverage & add CI pipeline
- [ ] Containerize (Dockerfile + multi-stage build) if targeting cloud deploy

---

## 9. Roadmap (Near-Term)

- Error report filtering & search (status, reporter, date range)
- Task & issue tagging / labels; bulk operations
- Full timezone localization (user preference + zoneinfo)
- Invoice aging buckets & CSV export
- Appointment ICS attachments & rebooking links
- Observation analytics dashboards (cross-cycle trends)

---

## 10. License / Usage

Internal operational tool for Excel Tutors; license not explicitly specified. Add a LICENSE file before external distribution.

---

## 11. Support / Contribution

- Open an Issue (if internal Git hosting) or extend test coverage with focused PRs.
- Keep feature PRs small: include tests + changelog bump if user-facing.

---

Happy supervising! 🚀
