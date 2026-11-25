# ACME Project Dashboard — 19.0.2.0.1

Dashboard-only update for the installed five-addon ACME suite. Replace only the acme_project_dashboard folder. Your local Core 19.0.2.0.1 and Approvals changes are not included or overwritten.

## Changes

Responsive five-card layout: Progress, Profitability and WIP across the first row; Schedule and Budget vs Actual on the second. Compact Project/From/To/currency filter bar and Refresh action. Clear title and breadcrumb, customer and Project Manager summary, readable totals, progress bars and contextual empty states. Financial cards remain visible with unavailable states instead of disappearing. Styles are scoped to this form and use theme variables for light/dark compatibility; no external chart/CDN dependencies.

Calculations and security remain unchanged: progress counts visible noncancelled leaf tasks; profitability is contract margin against incurred analytic cost; WIP is period analytic cost; schedule uses end dates and current Done status; budget is initial build-up budget versus lifetime costs. Dates affect WIP and Schedule only. No new finance access or automatic analytic allocation is introduced. Expand Metric definitions for details. Existing formula/date-rule open questions remain business decisions.

## Upgrade

Back up the test database. Replace the existing acme_project_dashboard folder with the folder in this ZIP. Do not install a second renamed copy. Restart/upgrade using your existing Odoo configuration:

```powershell
& "C:\Program Files\Odoo 19.0.20260711\python\python.exe" `
  "C:\Program Files\Odoo 19.0.20260711\server\odoo-bin" `
  -c "C:\Program Files\Odoo 19.0.20260711\server\odoo.conf" `
  -d YOUR_DATABASE -u acme_project_dashboard --stop-after-init
```

Stop the normal Odoo service before running a separate upgrade process, then restart it afterwards. Hard-refresh the browser (Ctrl+F5) to load the new backend stylesheet. Open Project details → Project Dashboard (not the status-update smart button).

## Validation

Python syntax/compilation, XML parsing, manifest asset paths and internal reference checks pass. Three Odoo regression tests are supplied, covering task/date metrics and the title/summary; they were NOT executed here because this environment lacks an Odoo Enterprise/PostgreSQL server. SCSS compilation and actual browser rendering inside Odoo remain target-server checks. This is a production-oriented implementation, not a claim of completed production sign-off.

Before production: upgrade on staging, verify all five cards at desktop/tablet/mobile widths and light/dark themes; test without an analytic account and without finance rights; reconcile costs and currency against accounting; change dates and verify only WIP/Schedule respond; check long project names/large and negative amounts; verify refresh and Project switching. If assets fail compilation, capture the server traceback. No uninstall or data reset is needed.
