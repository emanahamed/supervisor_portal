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
- Tailwind (CDN) + Soft UI styling

> For production, change `SECRET_KEY`, consider a real mail provider, and move to a managed DB.
