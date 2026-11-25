ACME Integrated Budget Management
=================================

Install the ``acme_integrated_budget`` app from Apps. Assign Department Budget
User to departmental preparers and managers, and Corporate Budget Office to
reviewers and consolidators. System administrators receive Corporate Budget
Office access automatically and can advance any company budget through each
approval stage.

Departmental Budgets
--------------------

Each department prepares one annual budget per fiscal year, with separate
revenue and cost lines and twelve generated monthly periods per line. The
workflow is Draft, Department Manager Review, Corporate Finance Review,
Corporate Approval, and Approved. Corporate Finance can return a submission
with feedback. Approved plans are locked; revisions are new, linked budget
records.
Fiscal periods must cover twelve monthly intervals, ending one day before
the same start date in the following year. For example, 8 July 2026 through
7 July 2027 is valid for fiscal year ``2026/27``.

The Budget Lines tab captures the template's line dimensions, including
business type, analytic accounts, project/WBS, product and cost categories,
GL account, customer, planning basis, contract reference, and owner. Monthly
Planning is shown as twelve month rows for each budget line, ordered January
through December. The budget form's Monthly Planning tab and the Monthly
Planning menu show each month's GL account, line description, quantity, unit
rate, manual amount, and calculated amount. In the budget form, select a month
row to open its plan in a popup form for recording or editing; the list stays
compact to avoid horizontal scrolling. The Budget Lines "Add a line" control
also opens the full line form in a popup.
When ``Use Quantity × Rate`` is selected, the calculated amount is quantity
multiplied by unit rate (including a zero quantity); otherwise the manual
amount is used. The Summary tab groups original budget,
approved revision, revised budget, commitment, invoice, actual, remaining,
and utilization amounts by plan type and category.

Corporate Consolidation
-----------------------

Select departments to consolidate a subset, or leave the scope empty and click
Load Approved Department Budgets to infer the scope from departmental budgets
matching the company and fiscal period. The loader links the latest approved
budget for each in-scope department; the Add picker shows all matching
approved budgets, and selecting one adds its department to the scope. The
consolidation computes totals from the original records and identifies
departments still pending approval. Corporate approval is blocked until every
department in scope has an approved submission. The original departmental
lines and ownership remain unchanged and can be opened from the consolidated
department list.

Monitoring
----------

Budget Monitoring & Reports provides monthly plans grouped by department,
plan type, category, business type, GL account, analytic dimensions, and
period. Integrated actuals come from posted journal items; invoiced/billed
amounts are shown separately as a subset of actuals. Confirmed Sales and
Purchase orders show their full gross value and ordered quantity separately
from the remaining uninvoiced commitment, so invoicing an order does not erase
its order history or double-count it as an open commitment. Sales orders use
the promised delivery date (or order date when no promise date is set);
purchase orders use the expected arrival date. Values are converted to the
budget company's currency, and ordered quantities are converted to the budget
line unit, the selected product's unit, or (if neither is configured) the
source order unit. The imported unit is shown with the quantity. Orders with
incompatible units for the same budget line are skipped for quantity totals
and identified during refresh; their values are still reported.

Transactions are matched by company, GL account, and the Department Analytic
Account selected on the budget line. A selected Project / Analytic Account
and product further narrow the match. A product selected on a budget line
must exactly match the product on a Sales, Purchase, invoice, or posted
accounting line; product-specific lines take precedence over general lines.
Analytic distribution percentages allocate split transactions. Confirmed
orders use the product's income or expense account (after fiscal-position
mapping) until a posted invoice exists; then the posted invoice line's GL
account is used, so an account selected on the invoice is honored. Posted
accounting lines must carry a product to match a product-specific budget line.
Lines without a GL account or department analytic account, and ambiguous
matches, are not automatically allocated. Create the Department analytic
plan and accounts in Accounting, then select the department account on each
budget line.

Integrated values are kept separately from manual monitoring adjustments;
budget summaries and reports show their combined values. Refreshing occurs
when orders are confirmed/cancelled or journal entries are posted/reset, on
the daily reconciliation schedule, or immediately from **Refresh
Transactions** on a departmental budget. The scheduled job reconciles later
changes to order lines and analytic distributions.

Budget Variance Analysis
------------------------

The Corporate Budget Office can create a **Budget Variance Analysis** for a
corporate consolidation, choose an analysis date range, and compute a saved
report. The report shows planned, actual spent, variance, variance percentage,
and remaining percentage in its summary. Its tabs provide variance lines by
budget line, a GL account summary, and a cost center / segment / project
summary. Analysis is limited to the approved departmental budgets included in
the selected corporate budget.

The date range includes every monthly budget period that overlaps it. Planned
monthly amounts are prorated for a partial budget month. Actual posted
accounting transactions are filtered by their exact dates, so prior months'
actuals are not carried forward into the selected month. Manual actual
adjustments are prorated for partial budget months. Variance is Planned minus
Actual Spent. A positive cost variance is favorable, while a revenue variance
is favorable when actual revenue exceeds plan.
