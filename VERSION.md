# Version History

## 0.3.0 - 2025-09-29

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
