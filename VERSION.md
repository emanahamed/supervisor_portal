# Version History

## 0.9.2 - 2025-09-30

Permission Visibility & Audit:

- Dynamic navigation pruning: all primary module links now gated with `can()` helper (only show links user can actually access).
- Admin section shows for superadmin or users with `manage_users`; role/user permission editors remain superadmin-only.
- Added effective permission panel on Profile page showing each permission key, description, allowed flag, and source (role / override allow / override deny / superadmin).
- Added recent permission changes list (latest 50) involving you (as actor or target) for transparency.
- Added current version changelog excerpt to Profile page for quick release awareness.
- Introduced `PermissionAudit` table and automatic logging for role permission additions/removals and user override changes (allow/deny/inherit transitions).

Internal:

- Lightweight auto-migration creates `permission_audit` table if missing.
- Seed logic untouched (still idempotent); audit intentionally ignores initial seed operations.
- Version bumped to 0.9.2.

Next (Not in 0.9.2): route-level enforcement decorator (server-side guard), export/import of permission configuration, grouping permissions by domain in UI, pagination/search for large audit history, diff view for role matrices.

## 0.9.3 - 2025-09-30

Role Taxonomy Alignment & Navigation Consistency:

- Replaced legacy roles (observer → supervisor, lead → centre_manager) with new organizational titles: Supervisor, Centre Manager, Admin, Super Admin.
- Added migration logic that transparently upgrades any existing users with legacy roles to new role keys on request handling.
- Updated default seeded permissions per new role set (Supervisor gains observation + meetings; Centre Manager broad operational set; Admin adds user & attendance management).
- Updated profile role select choices with user-friendly labels.
- Updated Role Permissions matrix to surface new roles; legacy roles no longer shown unless still present in DB pre-migration.
- Accepts legacy role names in role change POST for backwards compatibility (auto-maps to new names).
- Version bumped to 0.9.3.

Follow-ups (Not in 0.9.3): remove residual legacy references in data exports, add route decorators tying each endpoint to permission keys, UI help tooltip clarifying each role's scope.

## 0.9.1 - 2025-09-30

Role Taxonomy Expansion:

- Added new roles (centre_manager, supervisor, admin) with default permission seeds.
- Updated Role Permissions matrix to display human-readable role labels.
- Extended profile role choices and seeding logic for newly introduced roles.
- Version bumped to 0.9.1.

## 0.8.0 - 2025-09-30

Auth & UX Enhancements:

- Modernised login & registration pages: cleaner headings, icon-decorated inputs, logo-only branding (removed adjacent text label).
- Added dark/light mode toggle (persisted via localStorage) using Tailwind `dark` class strategy.
- Implemented Caps Lock detection for password inputs with subtle inline indicator.
- Added password visibility toggle consistency and remember-me checkbox (persistent session support).
- Inserted "Back to site" convenience link under auth card.

Internal:

- Enabled Tailwind darkMode config in auth base template.
- Added minimal JS bundle for theme + caps lock (no external dependency).
- Version bumped to 0.8.0.

Future (Not in 0.8.0): password strength meter, security rate-limit UI feedback, keyboard-focus outline refinement, progressive enhancement for no-JS environments.

## 0.9.0 - 2025-09-30

Fine-Grained Permission System:

- Added database-backed permission system with three new tables: `permission`, `role_permission`, `user_permission` (per-user overrides).
- Implemented hierarchical evaluation: superadmin > user override (allow/deny) > role-based grant.
- Seeded core permission set (dashboard, staff, cycles, observations, availability, issues, meetings, tasks, attendance fix, users, reports) with sensible defaults for roles staff / observer / lead.
- Added context helper `can(perm_key)` available in templates for conditional UI rendering.
- Introduced superadmin-only management pages:
  - Role Permissions (`/admin/role-permissions`): matrix editor of roles vs permissions.
  - User Permission Overrides (`/admin/user-permissions`): per-user inherit/allow/deny controls.
- Navigation updated with new Admin entries: Role Permissions & User Overrides.
- Idempotent seeding logic in startup ensures missing permissions / mappings are created without duplicating existing customizations.

Notes:

- Superadmin bypass remains unconditional (implicit all permissions).
- Removing a role permission immediately affects all users of that role unless an explicit user override exists.
- Deny override takes precedence over role grants; removing an override returns to role inheritance.

Potential Follow-ups (not in 0.9.0): integrate permission checks for each route decorator, UI hiding of unauthorized nav links using `can()`, audit logging of permission changes, export/import of permission configuration, grouping permissions by domain.

## 0.7.0 - 2025-09-30

Observation Module (Extended Tutor Observation & Reporting) Completion:

- Added extended observation workflow (`/observations/extended/...`) with rich checklist + narrative detail captured in `ObservationDetail`.
- Structured JSON-backed checklists (Weekly Test, Homework, Classwork, Organisation & Class Management) stored as text, parsed via helper methods.
- Dynamic multi-item list inputs (positives, improvements, targets, actions) with add/remove JS; JSON stored then rendered as numbered or bulleted lists in reports.
- Form resilience: user-entered data (all checklists, lists, targets/actions, notes) preserved on validation errors (no wiping behavior).
- Report revamp: unified Email + PDF styling with brand colours, section numbering aligned to extended form (1–11), renamed main header to "Tutor Observation Report".
- Criteria presentation: square tick/cross indicators; email uses 3-column responsive grid, PDF uses 3-column table for xhtml2pdf compatibility; both show met & unmet criteria.
- Converted Positive Aspects, Areas for Improvement, Targets, Actions Taken to ordered / bullet list formats (chips removed for consistency).
- Added per-section comment headings ("Comment Regarding <Section>").
- Removed former "Unmet Criteria Summary" block per requirements.
- Logo scaling adjustments (email) with aspect-ratio preservation.
- Conditional page break inserted in PDF after Section 5 for long reports.
- Target/Action storage backward compatibility: newline-joined lists derived from JSON arrays; legacy consumers unaffected.
- Timezone-aware generated timestamp passed to templates.
- Accessibility groundwork (semantic list structures) & consistent minimum font sizes (>=11) across PDF for readability.

Internal / Refactor:

- Macro & template simplifications removing obsolete card/table hybrids.
- Safeguards against missing detail rows (lazy creation in edit route).
- Clean removal of deprecated unmet-summary logic.

Next Potential Enhancements (not implemented in 0.7.0): font embedding for TW Cen MT in PDF, audit trail for cycle changes, date-in-cycle validation, global unmet criteria roll-up.

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
