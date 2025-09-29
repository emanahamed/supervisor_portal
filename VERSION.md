# Version History

## 0.6.0 - 2025-09-29

- Added Tasks (To-Do) module with model fields: Description, Notes, Actions Taken, Criticality, Urgency, Status (Pending/Done), Due Date, Created By, Assigned To, timestamps.
- Dashboard metrics: Open, Done, Overdue, Due in 3 Days, Total (scoped to selected assignee / current user by default).
- Filtering: Assigned To (superadmin only can change), single-select dropdowns for Status, Criticality, Urgency + free-text search.
- Ordering logic prioritises Pending, higher criticality & urgency, earlier due date, newest created.
- Modal-based create & edit with AJAX form submission (JSON success or HTML error re-render), plus full-page fallback.
- Inline status change via select (AJAX) and separate toggle endpoint.
- Access control: Non-superadmin users can only view tasks assigned to them; edit/delete restricted to creator, assignee, or superadmin.
- Visual overdue highlighting for tasks past due date (row tint) and due-soon metric (<=3 days remaining).
- Internal refactor: extracted reusable form partial `todos/partials/_form_inner.html` mirroring meetings pattern.

## 0.5.0 - 2025-09-29

- Added Meetings module: scheduling between users with fields (Participant, Agenda, Date, Time, optional Student, Parent, Outcome, Booked By metadata).
- Meetings analytics: counts for Today (all/you) and Week (all/you) plus total meetings summary.
- Filter bar: participant, booked_by, date range, text search (agenda substring).
- Integrated with shared table sorter/paginator UI.
- Added modal-based create & edit (AJAX) with graceful fallback full-page form.
- Automatic lightweight schema backfill (adds new meeting columns if missing) without Alembic.
- Extended global user loader to use SQLAlchemy 2.x `Session.get` pattern (removed legacy warning).

## 0.4.0 - 2025-09-29

- Added Issue Tracking module: CRUD, filtering (status, criticality, urgency, branch, text search) and dashboard metrics (total, open, resolved, critical open, high urgency open).
- Real-time (debounced) auto-apply multi-select filters mirroring Availability UX.
- Soft UI tables integrated with existing lightweight sorter/paginator.
- Navigation updated with Issues link; version bumped to 0.4.0.

## 0.3.0 - 2025-09-29

## 0.4.1 - 2025-09-29

- Added per-issue change log (IssueChange audit trail) recording field-level edits with old/new values and timestamps.
- Added optional "Action Taken" narrative field to Issues (not required on create).
- Issue list now surfaces recent (last 5) changes expandable inline for quick context.

- Added Tutor Availability module with real-time remote sync on page load (upsert semantics).
- Multi-select filtering: Departments, Branches, Subjects, Days + free-text search.
- Real-time (debounced) filter application; removed manual Apply button.
- Custom pagination with selectable page size (10/25/50/100/250/All) and persisted preference.
- Always ensure canonical branch list includes Whitechapel, East Ham, Stratford, Docklands (even if absent in source data).
- Simplified soft UI dropdown pattern (aligned with Staff page) replacing earlier complex chip interface.
- Table actions: inline edit/delete; improved badge styling for branch display.
- Internal refactor removing dependency on simple-datatables for this view (now using shared lightweight sorter/paginator logic).

## 0.2.0 - 2025-09-29

- Added cycle-based dashboard filtering and section grouping.
- Introduced version footer with modal changelog display.

## 0.1.1 - 2025-09-28

- Added user roles & avatar uploads, observer calibration, variance analytics.

## 0.1.0 - 2025-09-27

- Initial dashboard release: core KPIs, leaderboards, distributions, trends.
