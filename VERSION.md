# Version History

## 2.1.2 - 2025-10-27

Role Permissions: Tutor column added; email branding standardized to logo-only header

- Role Permissions: Expanded the matrix to include the Tutor role as its own column across the Role Permissions editor and related quick-update endpoints. You can now grant and revoke permissions for tutors explicitly (superadmin bypass remains unchanged).
- Email branding: All system emails now use a white header with the logo only. The former adjacent "Excel Tutors" header text has been removed. The logo preserves its aspect ratio (constrained via max-height) and no colored background is applied. Changes are centralized in the shared email shell and applied to fallback builders (e.g., task notifications) for consistency.

Notes:

- No schema changes; safe minor release.

## 2.1.1 - 2025-10-27

Books & Invoices polish; cover selection UX

- Books: Restyled the Active toggle to a modern switch-style control in both the list and the modal. Behavior unchanged (immediate POST toggle in list; form-bound in modal).
- Books: Added dropdown-based cover selection plus inline upload for cover images. New endpoints provide available covers and handle uploads; preview updates instantly.
  - Endpoints: `GET /tools/books/covers.json`, `POST /tools/books/upload-cover`
- Kids Club Invoices: PDF/print invoice now displays Payment Method and Status in the header metadata block (works with `?download=1` or `?print=1`).

Notes:

- Cover URL remains a separate field for other purposes; the listing preview now prefers the selected cover file, falling back to Cover URL.
- No schema changes; purely routes/templates updates.

## 2.1.0 - 2025-10-23

Recruitment Admin: Application Management, Branded Invites, and Dashboard

- New permission: `manage_recruitment` (seeded automatically) with default grants to Admin and Centre Manager; Super Admin bypass as usual.
- Navigation: added "Application Management" group (permission-gated) with links to Applications and Recruitment Dashboard.
- Applications Admin:
  - List with filters (query, status, branch preference, university, study year) and bulk actions (Mark Reviewed, Reject, Select, Onboard, Invite).
  - Detail view with per-application Invite action.
  - Invitation emails use branded HTML shell and send from the Recruitment mailbox (via EmailSetting).
- Recruitment Dashboard: KPIs (totals, last 30 days, status counts), 12‑month trend, branch distribution, top universities and subjects; implemented with Chart.js.
- Helpers: added interview slot label builder and ordinal suffix helper for nicer invite content.

Notes:

- Existing public Job Application flow unchanged. Admin features are permission-gated and hidden from unauthorized users.
- Email templates fallback to a built-in branded invite if no editable template exists; can be migrated to `EmailTemplate` later.

## 2.0.7 - 2025-10-12

Navigation width & Staff access-code communications

- Sidebar: increased desktop sidebar width slightly (md:w-72) and adjusted content padding to keep alignment. Ensures main items stay on a single line without wrapping; nav leafs now ellipsize if text is too long.
- Ordering: kept Resource Management prominent directly under Dashboard as requested; labels are whitespace‑nowrap to avoid multi‑line items.
- Staff: added per‑user “Email Code” action and bulk email actions (selected or all active) to send staff their 6‑digit access codes.
  - Endpoints: `POST /api/staff/<id>/email-access-code` and `POST /api/staff/email-access-codes` (optional JSON `{ids:[...]}`), both permission‑gated to `manage_staff`.
  - UI: multiselect checkboxes + Select All, top‑right bulk action buttons on Staff list.
  - Status: converted Active/Inactive to an inline color‑coded toggle switch with immediate AJAX update via existing toggle API.
- Security: CSRF meta/header injector continues to protect POST requests; endpoints return JSON with per‑row error collection on bulk sends.

No schema changes. Safe minor release.

## 2.0.8 - 2025-10-22

Invoice management: manager review controls, NMW bands admin, branded notifications

- Manager controls: added Accept / Reject buttons to the staff invoice detail view. Reject opens a modal to capture the reason and both actions record audit rows in `StaffInvoiceChange`.
- Notifications: manager actions trigger branded HTML emails to the submitter. The system prefers editable `EmailTemplate` records and falls back to built-in branded HTML builders when templates are missing.
- NMW bands: introduced a small System Setup admin page `/admin/nmw-bands` to manage National Minimum Wage bands as structured rows (table CRUD, no JSON editor). Default bands seeded include: £12.21 (21+), £10.00 (18–20), £7.55 (under 18), and £7.55 (apprentice).
- Invoice detail & PDF improvements: invoice detail now shows employee age and the selected NMW band; bank details and emphasized totals are included in both print and PDF outputs. Print view includes logo and consistent inline CSS.
- DB safety helper: added `scripts/ensure_staff_columns.py` — a conservative script that creates a timestamped backup and adds missing nullable `staff` columns (used to repair missing `joining_date` during this release).

Notes:

- Default email templates for approved/rejected invoices are seeded but editable templates (via the Email Templates admin) will be used if present.

## 2.0.9 - 2025-10-22

Template and rendering fixes

- Fixed duplicated totals in the invoice detail page by consolidating totals into a single emphasized totals block.
- Ensured bank details render reliably by preferring the resolved `Staff` record (`staff_rec`) for bank fields in both detail and PDF/print rendering (fallbacks to `inv.created_by` remain).
- PDF/print renderers now pass `staff_rec` into templates for consistent rendering across contexts.

Notes:

- If bank details still appear missing for specific invoices, check the `Staff` ↔ `User` linkage for the submitting user (invoice.created_by_id vs Staff.user_id) or the staff record's bank fields.

## 2.0.6 - 2025-10-09

Dashboard: Pending End of Day (EOD) Widget + CSRF hardening for Error Reports

- Added a compact EOD widget on the main dashboard:
  - Shows a personal prompt when you have a shift today but haven’t submitted your EOD yet, with a quick link to complete it.
  - For admins/centre managers/superadmin, lists up to 10 staff who are pending EOD for today with links to the EOD page; shows “and N more…” when applicable.
  - Backed by new context values in the dashboard route: `my_eod_pending`, `missing_eod_staff`, and `today`.
- CSRF reliability improvements for error reporting:
  - Injected an explicit hidden CSRF input in the 500 page error report form and the error status update form.
  - Enhanced the global CSRF injector to observe dynamically-inserted forms (MutationObserver) and auto-inject tokens.
  - Kept fetch() header patch to attach `X-CSRFToken` for unsafe methods on same-origin requests.
- No schema changes. Purely template and route-context updates. Bumped `VERSION` to 2.0.6.

## 2.0.5 - 2025-10-09

End of Day Checklist: Superadmin Delete + Versioning API polish

- Added superadmin-only delete endpoint for End of Day Checklists:
  - `POST /floor/checklists/<id>/delete` protected by login and superadmin check, CSRF-required.
  - Index page now shows a Delete button for superadmin users with confirm prompt per row.
- Versioning & Changelog:
  - Confirmed lightweight version endpoints: `/version-history`, `/api/version`, `/api/changelog`.
  - Structured changelog parsing available via `version_info.parse_changelog`; `latest_entry()` wires the current `VERSION` with its metadata.
- Bumped `VERSION` to 2.0.5.

## 2.0.4 - 2025-10-09

Shift Management UX polish: JSON edit prefill, delete, and modal bindings

- Added lightweight JSON endpoint `/api/floor/shifts/<id>` to fetch a shift for clean modal prefill (replaces DOM scraping).
- Implemented safe delete endpoint `POST /floor/shifts/<id>/delete` with CSRF and guard against deleting past shifts.
- Updated shifts index UI:
  - Edit now loads data via fetch; prevents opening modal for past shifts.
  - Added Delete button beside Edit (future-only), with confirmation.
  - Modal form now binds Staff, Branch, Floor(s), Notes, and Timeslots directly with two-way state (Alpine).
  - Floor(s) checkboxes reflect current selection and update form state.
- Passed `BRANCH_CHOICES` into the index template to populate the Branch dropdown consistently.
- No schema changes. Minor route additions only. Version bump to 2.0.4.

## 2.0.3 - 2025-10-09

Floor Management Scaffold (Nav, Permissions, Routes, Placeholders):

- Added new sidebar group "Floor Management" gated by floor permissions and shown when the user has any of: `floor_dashboard`, `manage_shifts`, `manage_eod_checklist`, `manage_floor_reports`, `manage_call_list`.
- Created initial routes and permission checks for:
  - Dashboard (`/floor`)
  - Shifts (`/floor/shifts`, `/floor/shifts/new`)
  - End of Day Checklist (`/floor/checklists`, `/floor/checklists/new`)
  - Print Reports (`/floor/reports`, `/floor/reports/new`)
  - Call List (`/floor/call-list`, `/floor/call-list/new`)
- Seeded permissions and default role grants (Centre Manager and Admin) previously; sidebar now consumes them. Superadmin sees all by default.
- Added minimal placeholder templates for each page under `templates/floor/...` so pages render successfully pending full UI/logic.

Notes / Next Steps:

- Replace placeholders with real forms, lists, and persistence as requirements are finalised.
- Consider extracting common list header actions into components for consistency with other modules (Meetings, Tasks).
- Optional: add quick stats to the Floor Dashboard (active shifts now, pending EOD checklists, calls queued today).

## 2.0.2 - 2025-10-08

System Setup Navigation Group & Tuition Pricing Relocation:

- Introduced new "System Setup" sidebar group for foundational configuration tasks; initial item is the Tuition Prices Setup page (previously listed under Tools as "Enrollment Calculator").
- Relocated pricing configuration link out of the general-purpose Tools grouping to improve information architecture—administrative setup now clearly separated from ad‑hoc utilities.
- Renamed link label to a clearer action-oriented title: "Tuition Prices Setup" (was "Enrollment Calculator") for better user intent recognition.
- Permission Gating: Entire group (and contained link) is shown only to users granted `manage_pricing` (or superadmin). Removed `manage_pricing` from Tools gating logic to prevent duplicate exposure.
- Added unique persistent toggle key `system_setup` to localStorage nav state (`navStateV2`). Existing users start with it collapsed by default, preserving current open groups.
- No database, API, or seeding changes (permission key already introduced in prior release). Purely navigational / UX structural update; safe patch increment.

Follow-Ups (Not in 2.0.2):

- Potential future System Setup items (branding assets, email/SMPP credentials, feature flags) once underlying config UIs exist.
- Macro extraction for repeated collapsible group button/template patterns to reduce duplication.
- Add subtle badge or dot indicator for pending setup tasks (e.g., unset pricing tiers) when config completeness tracking is implemented.

## 2.0.1 - 2025-10-08

Sidebar Icon Consistency & Layout Spacing Polish:

- Added compact SVG icons to every nested (leaf) item inside collapsible sidebar groups (Admin, Books, Invoice, Meetings, Staff Management, Student Management, Tools, Tutor Observations) for faster visual scanning and consistent affordance hierarchy.
- Adjusted nav leaf styling (line-height tweak) to vertically align new 14px icon set with text baseline and avoid jitter between active/hover states.
- Increased horizontal padding between the sidebar and main content (header & main containers now use `pl-8`) improving readability of page titles and reducing visual crowding next to the navigation edge.
- Minor a11y/UX improvement: text still readable in reduced motion environments (no animation changes required); icons inherit current color for dark/light parity.
- No database or API changes; purely presentational. Safe patch release.

Follow-Ups (Not in 2.0.1):

- Optional subtle vertical divider shadow between sidebar/content for additional depth.
- Keyboard focus outline refinement for newly iconised leaf links.
- Extract repeating SVG attributes into a macro for DRYness if icon set expands further.

## 2.0.0 - 2025-10-08

Navigation Redesign, Branding Alignment & Modern Auth Layout:

- Rebuilt left sidebar navigation with grouped, collapsible sections (persisted open state in `localStorage` under `navStateV2`) and fine-grained permission gating (`can()` + superadmin safeguards). Groups alphabetically ordered inside domains (Admin, Books, Invoices, Issues, Meetings, Staff, Students, Tasks, Tools, Tutor Observations) for faster scan.
- Removed legacy scattered conditional logic and fragile `globals()` fallbacks in nav partial; fixed 500 error triggered by undefined `globals` usage on certain tool routes by referencing concrete endpoint names (`book_orders_index`, `companies_index`).
- Replaced placeholder Tailwind mark with official Excel Tutors logo across sidebar & auth pages; added subtle blend overlay on hero image side of auth views.
- Fully replaced legacy glassmorphism auth templates with a modern split layout (`_auth_base.html`): left form panel, right responsive hero image, dark‑mode ready, simplified markup, accessible form labels, and consistent flash message styling across state categories.
- Updated login (`auth/login.html`) and registration (`auth/register.html`) pages to new design system: semantic headings, improved validation message styling, password strength meter (registration), consistent button styles, and improved dark mode color tokens.
- Consolidated theme initialization for auth views (localStorage `authTheme` respected) and retained Caps Lock detection script; removed redundant gradient/background decorative DOM nodes from old layout.
- Changelog entry & version bump to 2.0.0 (no DB schema changes required).

Follow-Ups (Not in 2.0.0):

- Add dedicated dark/light mode toggle affordance on auth screen (currently honors stored preference only).
- Unify password strength component between login (optional) & registration for consistency.
- Add social / SSO placeholder region (commented) for future identity provider integrations.
- Integrate rate limiting feedback & generic auth error code mapping (lockout, disabled account) for clearer user messaging.

## 1.9.9 - 2025-10-07

Students Module (CRUD, Import/Export, Audit) + UI Consistency & Sorting:

- Introduced full Students module with create (modal), detail (edit + audit log), delete, CSV/XLSX import (idempotent upsert) & Excel export.
- Added `Student` model (id, student_id unique, name, type, year, email, phone, address, academic, status, timestamps) and `StudentChange` audit model for field-level edit tracking (old/new + actor + timestamp).
- Import pipeline parses preferred contact column (email/phone extraction) and updates existing rows without blanking missing fields; export includes both parsed and raw contact forms.
- Added bulk Activate / Inactivate actions with multi-select + select-all UI.
- Implemented server-side pagination (page & per_page) with navigation controls and per-page selector.
- Added server-side sorting (default: Active status first then ID asc) with clickable column headers toggling asc/desc for ID, Name, Year, Status.
- Unified Students list & detail styling to Tailwind Soft UI (replacing legacy Bootstrap remnants) with status badges & condensed last change summary.
- Removed global error reporting form from Students pages via new `report_form` overridable block and per-template suppression.
- Added audit-focused pytest (`test_students_audit.py`) verifying edit generates `StudentChange` rows for changed fields (name & status scenario).
- Enhanced index actions column: View / Edit anchor (scroll to form) / Delete (POST with CSRF).
- Added colored status badges (Active / Inactive / Pending / Withdrawn) reused across list & detail.

Changelog & Versioning Enhancements:

- Bumped `VERSION` to 1.9.9; updated README release summary guidelines.
- Established consistent multi-key default ordering pattern (status priority + primary sort + id).

Follow-Ups (Not in 1.9.9):

- Add filtering quick pills for Active / Inactive.
- Extend audit test coverage (import upsert scenarios, bulk status changes) and pagination edge tests.
- Add inline duplicate ID real-time validation (AJAX) in modal before submission.
- Optional student merge & archival workflow.

Upgrade / Migration Notes:

`Student` and `StudentChange` tables auto-create on first request if missing (runtime DDL for SQLite). For production RDBMS without permissive DDL, apply equivalent CREATE TABLE statements or integrate Alembic migration before deployment.

Security / UX Considerations:

- Bulk operations restricted to `manage_students` permission.
- Delete action converted to POST form (CSRF-protected) instead of GET link.
- Error report form suppression ensures focused UI on student workflows.

## 1.9.8 - 2025-10-07

Theme Preference Persistence, System Detection & SVG Toggle:

- Added persistent per-user theme preference (`User.theme_preference`) supporting values: `light`, `dark`, `system`.
- New profile page select allows users to save their preference server-side; falls back to system when set to `system`.
- Global theme dropdown (replaces prior single button) with SVG sun/moon icons and smooth color transitions (`transition-colors`).
- System preference detection via `matchMedia('(prefers-color-scheme: dark)')` with live listener when in `system` mode.
- Lightweight AJAX endpoint path (`POST /profile` with `_theme_update=1`) allows instant toggle without full form submission.
- Introduced centralized theme utility script (`static/js/theme.js`) encapsulating detection, application, and listener logic (reduces inline JS duplication).
- Added graceful initialization order: explicit localStorage choice → saved server preference → system → default light.
- Ensured dark mode classes applied prior to paint to minimize flash; transitions handled after initial load.

Migration Note:

SQLite auto-migration adds `theme_preference` column to `user` table on first request if missing (default `system`). No manual migration required. For production deployments using another RDBMS, apply an `ALTER TABLE user ADD COLUMN theme_preference VARCHAR(20) DEFAULT 'system';` before deploying if automatic DDL is disabled.

Testing:

- Added pytest covering quick theme preference update path (`_theme_update=1`).

Follow-Ups (Not in 1.9.8):

- Persist resolved explicit vs system choice as separate key (currently reuses `portalTheme`); optional enhancement to track original user intent more explicitly.
- Add visual indicator (checkmark) in theme dropdown reflecting active mode.
- Consolidate remaining inline theme-related JS into utility for version modal / legacy auth pages.

## 1.9.7 - 2025-10-06

Error Reporting System, Traceback Hygiene, Fingerprinting & UX Improvements:

- Introduced full in-app error reporting workflow:
  - 500 error page now offers a "Report this error" inline form that auto-attaches traceback, request path, method, and user agent (metadata cached server-side on exception).
  - Global top‑nav "Report Issue" button (modal) allows any authenticated user to submit a manual system issue (without traceback unless from 500 page).
  - Added `ErrorReport` model capturing diagnostic fields (error_type, error_message, traceback, fingerprint, request metadata, optional screenshot) plus status workflow (Open / In Progress / Resolved) and resolution audit (resolved_by, resolved_at).
  - Screenshot upload support with drag/drop style (png/jpg/jpeg) stored under `static/uploads/`.
  - Superadmin navigation now includes "Error Reports" section with listing & detail pages; status updates trigger email notification to original reporter when marked Resolved.
- Implemented SHA-256 fingerprint generation (error_type + message + first traceback line) to de‑duplicate recurring errors; new submissions append reporter comment onto existing open report instead of creating duplicates.
- Added pagination to Error Reports index (page & per_page params; default 25, capped 100) with compact navigation controls.
- Traceback sanitation: truncate oversized tracebacks (>20k chars) at capture; detail view shows a collapsed 2k-char preview with expand/collapse toggle to preserve UI performance.
- Converted key naive `datetime.utcnow()` usages in runtime logic to timezone-aware `datetime.now(timezone.utc)` to silence deprecation warnings and prep future TZ features.
- Added reporter comment consolidation (appends with separator when merging into existing fingerprinted report).

Attendance Fix Upload UX Refresh:

- Replaced basic file input with accessible drag & drop zone (dashed border, hover & focus ring, dynamic overlay, iconography) for `.xls` / `.xlsx` at `/attendance/fix`.
- File name + size now displayed post-selection; invalid type rejection with inline message; prevents submit without a chosen file.
- Minor responsive layout correction (upload area alignment in grid third column) improving spacing consistency.

Misc / Internal:

- Added error report templates: `errors/reports_index.html`, `errors/report_detail.html` with badge styling, status select, and screenshot preview.
- Updated navigation & base layout to integrate reporting modal & admin link.
- Added fingerprint column usage for error grouping (foundation for future analytics / suppression logic).

Follow-Ups (Not in 1.9.7):

- Migrate all remaining model default timestamps to timezone-aware factories.
- Add automated tests for error reporting (fingerprint de-dupe, status change notification, pagination) & drag/drop file upload (feature detection / progressive enhancement).
- Implement search & filtering (status, reporter, date range) for error reports beyond simple pagination.
- Optional rate limiting / spam throttling for manual report submissions.
- Bulk resolve / tag classification for error triage.

Upgrade Note:

No destructive migrations required; `ErrorReport` table will auto-create on first request (SQLite pragmas). Existing deployments simply pick up new navigation & modal. Consider backfilling historical 500 logs via manual insert if legacy data is desired.

## 1.9.6 - 2025-10-06

Extended Observation Checklist Reliability & PDF Polishing:

- Unified checklist value normalization across form, email, and PDF via new `checklist_utils.py` (single source for label canonicalization, variant generation, and Jinja helpers `checklist_value_for`).
- Refactored `ObservationDetail.get_checklist` to delegate to centralized normalization eliminating drift between storage, form rendering, and report outputs.
- Updated extended observation form (`extended_form.html`) and PDF (`report_pdf.html`) to remove legacy ad‑hoc checkbox logic and rely exclusively on the helper, fixing prior issue where met criteria appeared crossed.
- Added pytest `test_checklist_render.py` covering mixed historical key variants (bare, prefixed, multi‑prefixed) ensuring they render as checked; guards against future regressions.
- Introduced debug endpoint `/observations/<id>/debug_checklist` (temporary) exposing normalized true keys per group for data verification post-migration.
- Implemented migration script `migrate_checklists.py` to canonicalize historical checklist JSON (produced timestamped backup file) removing noisy multi‑prefixed keys while preserving logical truth states.
- PDF adjustments: added Timeslot to header metadata block; removed observer profile picture from "Prepared By" section to align with updated privacy/style requirement.
- Consolidated PDF checkbox logic (removed custom macro) ensuring parity with email rendering.

Internal / Maintenance:

- Central helper exposes `normalize_label`, `generate_variants`, `normalize_mapping`, and `value_for` (registered as Jinja globals) enabling consistent future additions.
- Simplified templates & reduced conditional proliferation; improved maintainability of observation reporting pipeline.

Follow-Ups (Not in 1.9.6):

- Remove debug endpoint after confirming no further data anomalies.
- Expand test coverage to additional checklist groups (homework, classwork, org_mgmt) with seeded truth scenarios.
- Enforce canonical key write-path to prevent reintroduction of multi‑prefixed variants (already mitigated by normalization, further hardening optional).

Migration Note:

All existing observation detail records processed; backup stored as `backup_checklists_<epoch>.json` in project root for rollback/audit.

## 1.9.5 - 2025-10-05

Appointment Scheduling (Public & Admin) + Access Control:

- Introduced public bilingual (English / Bangla) appointment booking portal at `/booking` allowing visitors to reserve a slot with a member of the management team (formerly referred to as "Super Admin").
- Added management-side slot administration UI `/admin/appointments` with:
  - Single and bulk slot creation (duration auto-splitting).
  - Real‑time status indicators (Available / Booked / Inactive).
  - Per-slot actions: Activate / Deactivate / Cancel (cascades booking cancel + notification).
  - Booking cancellation (admin and attendee flows) with email notifications (confirmation, reminder 12h before, cancellations) to attendee and management member.
  - Automatic reminder scheduling via APScheduler (12 hours pre‑start) with resilient lazy initialization.
  - Schema auto-migrations: `appointment_slot` and `appointment_booking` tables (idempotent creation guarded by runtime checks).
- Implemented robust filtering, searching & sorting for Upcoming Slots (and mirrored criteria for Past Slots source list):
  - Text search across management member name, notes, and active booking (name, student ref, email, reason).
  - Status filter (Available / Booked / Inactive).
  - Management member filter.
  - Date range (start / end) filtering.
  - Sort by Date/Time, Member, or Status with direction toggle (asc/desc).
- Unified terminology: replaced all public & email occurrences of "Super Admin" with "member of our management team" (Bangla copy updated accordingly). Admin UI labels updated (column headings, badges, error text, form labels).
- Added new permission `manage_appointments` seeded for role `admin` (and automatically granted to `superadmin`) controlling all appointment admin endpoints.
- Navigation: Appointments link only renders if user is superadmin or has `manage_appointments`.
- Context processor now exposes `supported_languages` & `SUPPORTED_LANGUAGES` for templates needing bilingual labels.
- Hardened templates against 404 / 500 rendering errors by guarding `request.endpoint` before invoking `startswith`.
- Booking creation fixed: ensured `cancel_token` populated before building external cancellation URL (resolves intermittent BuildError on form submit).
- Superadmin user management: ability to delete users with safeguards (cannot delete self, last superadmin, or users with linked operational records).

Technical Notes:

- Email generation includes structured, bilingual customer + management copy with consistent HTML shell and button styling, using explicit slot time range formatting.
- Reminder jobs skip past-due windows and reschedule only future booked, non-cancelled appointments on first request (lazy priming pattern replacing deprecated Flask hook).
- Filtering and sorting operate in-memory over fetched slot list (sufficient for current scale; future optimization could push filtering into SQL with dynamic query composition).

Follow-Ups (Not in 1.9.5):

- Pagination & batched loading for large slot histories.
- ICS calendar attachment in confirmation/reminder emails.
- Timezone awareness using `zoneinfo` & user preference.
- Audit logging for slot/booking state transitions (PermissionAudit analogue).
- Rate limiting / captcha for public booking to mitigate automated abuse.

## 1.9.4 - 2025-10-01

Invoice Communication & Analytics Enhancements:

- Replaced fragile server-side PDF generation with a robust print‑friendly HTML invoice (browser Print / Save as PDF workflow) including auto print dialog via `?print=1`.
- Added inline CSS in print mode ensuring consistent styling when saved as PDF or forwarded.
- Introduced invoice emailing: new `/invoices/<id>/email` POST endpoint sends fully rendered HTML invoice to recipient (parent_email) using BrightStar SMTP credentials.
- Added Email action buttons on invoice detail & list rows (with confirmation prompt).
- Implemented summary widgets on invoice index: counts (total/paid/unpaid) and monetary totals (total & unpaid).
- Added per-company stats panel (count, paid/unpaid, totals) derived from current filtered result set, sorted by total billed descending.
- Extended sorting: column header toggles with direction indicators (Invoice #, Company, Invoice Date, Parent, Child, Total, Status, plus existing defaults).
- Added no-cache headers and inline CSS fallback to ensure immediate reflection of template/style edits.
- Refined invoice template (icon removal for reliability, improved separator styling, scalable centered logo, streamlined metadata positioning).

Technical Notes:

- Stats and per-company aggregates computed in-memory for current (capped) query (500 invoices) to avoid heavy aggregate queries; future optimization may push to SQL.
- Email content reuses print invoice template for single source of truth (avoids duplication & drift).
- Security: SMTP credentials currently in application code; recommend environment variable extraction + secrets management in a subsequent release.

Follow-Ups (Not in 1.9.4):

- Track `emailed_at` & `email_fail_count` fields per invoice for audit / resend logic.
- Optional PDF attachment using headless Chromium (WeasyPrint / Playwright) for clients requiring attached artifact.
- Overdue highlighting (due_date < today & unpaid) and aging buckets widget (0–30 / 31–60 / 61–90 / 90+).
- Pagination + CSV export for large invoice sets & company stats.
- Rate limiting / debounce on rapid repeated email sends.

## 1.9.2 - 2025-09-30

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
- Version bumped to 1.9.2.

Next (Not in 1.9.2): route-level enforcement decorator (server-side guard), export/import of permission configuration, grouping permissions by domain in UI, pagination/search for large audit history, diff view for role matrices.

## 1.9.3 - 2025-09-30

Role Taxonomy Alignment & Navigation Consistency:

- Replaced legacy roles (observer → supervisor, lead → centre_manager) with new organizational titles: Supervisor, Centre Manager, Admin, Super Admin.
- Added migration logic that transparently upgrades any existing users with legacy roles to new role keys on request handling.
- Updated default seeded permissions per new role set (Supervisor gains observation + meetings; Centre Manager broad operational set; Admin adds user & attendance management).
- Updated profile role select choices with user-friendly labels.
- Updated Role Permissions matrix to surface new roles; legacy roles no longer shown unless still present in DB pre-migration.
- Accepts legacy role names in role change POST for backwards compatibility (auto-maps to new names).
- Version bumped to 1.9.3.

Follow-ups (Not in 1.9.3): remove residual legacy references in data exports, add route decorators tying each endpoint to permission keys, UI help tooltip clarifying each role's scope.

## 1.9.1 - 2025-09-30

Role Taxonomy Expansion:

- Added new roles (centre_manager, supervisor, admin) with default permission seeds.
- Updated Role Permissions matrix to display human-readable role labels.
- Extended profile role choices and seeding logic for newly introduced roles.
- Version bumped to 1.9.1.

## 1.8.0 - 2025-09-30

Auth & UX Enhancements:

- Modernised login & registration pages: cleaner headings, icon-decorated inputs, logo-only branding (removed adjacent text label).
- Added dark/light mode toggle (persisted via localStorage) using Tailwind `dark` class strategy.
- Implemented Caps Lock detection for password inputs with subtle inline indicator.
- Added password visibility toggle consistency and remember-me checkbox (persistent session support).
- Inserted "Back to site" convenience link under auth card.

Internal:

- Enabled Tailwind darkMode config in auth base template.
- Added minimal JS bundle for theme + caps lock (no external dependency).
- Version bumped to 1.8.1.

Future (Not in 1.8.0): password strength meter, security rate-limit UI feedback, keyboard-focus outline refinement, progressive enhancement for no-JS environments.

## 1.9.0 - 2025-09-30

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

Potential Follow-ups (not in 1.9.0): integrate permission checks for each route decorator, UI hiding of unauthorized nav links using `can()`, audit logging of permission changes, export/import of permission configuration, grouping permissions by domain.

## 1.7.0 - 2025-09-30

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

Next Potential Enhancements (not implemented in 1.7.0): font embedding for TW Cen MT in PDF, audit trail for cycle changes, date-in-cycle validation, global unmet criteria roll-up.

## 1.6.0 - 2025-09-29

- Added Tasks (To-Do) module with model fields: Description, Notes, Actions Taken, Criticality, Urgency, Status (Pending/Done), Due Date, Created By, Assigned To, timestamps.
- Dashboard metrics: Open, Done, Overdue, Due in 3 Days, Total (scoped to selected assignee / current user by default).
- Filtering: Assigned To (superadmin only can change), single-select dropdowns for Status, Criticality, Urgency + free-text search.
- Ordering logic prioritises Pending, higher criticality & urgency, earlier due date, newest created.
- Modal-based create & edit with AJAX form submission (JSON success or HTML error re-render), plus full-page fallback.
- Inline status change via select (AJAX) and separate toggle endpoint.
- Access control: Non-superadmin users can only view tasks assigned to them; edit/delete restricted to creator, assignee, or superadmin.
- Visual overdue highlighting for tasks past due date (row tint) and due-soon metric (<=3 days remaining).
- Internal refactor: extracted reusable form partial `todos/partials/_form_inner.html` mirroring meetings pattern.

## 1.5.0 - 2025-09-29

- Added Meetings module: scheduling between users with fields (Participant, Agenda, Date, Time, optional Student, Parent, Outcome, Booked By metadata).
- Meetings analytics: counts for Today (all/you) and Week (all/you) plus total meetings summary.
- Filter bar: participant, booked_by, date range, text search (agenda substring).
- Integrated with shared table sorter/paginator UI.
- Added modal-based create & edit (AJAX) with graceful fallback full-page form.
- Automatic lightweight schema backfill (adds new meeting columns if missing) without Alembic.
- Extended global user loader to use SQLAlchemy 2.x `Session.get` pattern (removed legacy warning).

## 1.4.0 - 2025-09-29

- Added Issue Tracking module: CRUD, filtering (status, criticality, urgency, branch, text search) and dashboard metrics (total, open, resolved, critical open, high urgency open).
- Real-time (debounced) auto-apply multi-select filters mirroring Availability UX.
- Soft UI tables integrated with existing lightweight sorter/paginator.
- Navigation updated with Issues link; version bumped to 1.4.1.

## 1.3.0 - 2025-09-29

## 1.4.1 - 2025-09-29

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

## 1.2.0 - 2025-09-29

- Added cycle-based dashboard filtering and section grouping.
- Introduced version footer with modal changelog display.

## 1.1.1 - 2025-09-28

- Added user roles & avatar uploads, observer calibration, variance analytics.

## 1.1.0 - 2025-09-27

- Initial dashboard release: core KPIs, leaderboards, distributions, trends.
