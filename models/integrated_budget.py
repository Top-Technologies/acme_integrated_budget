from dateutil.relativedelta import relativedelta
from uuid import uuid4

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.sql import drop_view_if_exists


def _fiscal_year_labels(date_from, date_to):
    if date_from.year == date_to.year:
        return {str(date_from.year), f"{date_from.year}/{date_to.year % 100:02d}"}
    return {
        f"{date_from.year}/{date_to.year % 100:02d}",
        f"{date_from.year}/{date_to.year}",
    }


class HrDepartment(models.Model):
    _inherit = "hr.department"

    budget_user_ids = fields.Many2many(
        "res.users", compute="_compute_budget_access_users", store=True)
    budget_manager_user_id = fields.Many2one(
        "res.users", compute="_compute_budget_access_users", store=True)

    @api.depends("member_ids.user_id", "manager_id.user_id")
    def _compute_budget_access_users(self):
        for department in self:
            users = department.member_ids.mapped("user_id")
            manager_user = department.manager_id.user_id
            if manager_user:
                users |= manager_user
            department.budget_user_ids = users
            department.budget_manager_user_id = manager_user


class DepartmentBudget(models.Model):
    _name = "acme.department.budget"
    _description = "Departmental Budget"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_from desc, department_id, name"
    _check_company_auto = True

    name = fields.Char(string="Budget Reference", required=True, copy=False,
                       default="New", tracking=True)
    budget_name = fields.Char(string="Budget Name", required=True,
                              default="Departmental Annual Budget")
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        index=True, tracking=True)
    currency_id = fields.Many2one(related="company_id.currency_id", store=True)
    fiscal_year = fields.Char(required=True, tracking=True)
    date_from = fields.Date(string="Start Date", required=True, tracking=True)
    date_to = fields.Date(string="End Date", required=True, tracking=True)
    department_id = fields.Many2one(
        "hr.department", required=True, check_company=True, index=True,
        tracking=True)
    owner_id = fields.Many2one(
        "res.users", string="Budget Owner", required=True,
        default=lambda self: self.env.user, tracking=True)
    prepared_by_id = fields.Many2one(
        "res.users", string="Prepared By", required=True,
        default=lambda self: self.env.user)
    version_type = fields.Selection(
        [("original", "Original"), ("revision", "Revision")],
        required=True, default="original", copy=False, tracking=True)
    version_number = fields.Integer(required=True, default=1, copy=False)
    revision_of_id = fields.Many2one(
        "acme.department.budget", string="Revision Of", readonly=True,
        copy=False, ondelete="restrict")
    revision_ids = fields.One2many(
        "acme.department.budget", "revision_of_id", string="Revisions",
        copy=False)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("manager_review", "Department Manager Review"),
            ("finance_review", "Corporate Finance Review"),
            ("corporate_review", "Corporate Approval"),
            ("approved", "Approved"),
            ("returned", "Returned for Correction"),
        ],
        default="draft", required=True, index=True, copy=False, tracking=True)
    finance_feedback = fields.Text(
        string="Corporate Finance Feedback", tracking=True)
    approval_reference = fields.Char(string="Approval Reference", tracking=True)
    submission_date = fields.Date(string="Submission Date", readonly=True, copy=False)
    assumptions = fields.Text(string="Assumptions / Notes")
    line_ids = fields.One2many(
        "acme.department.budget.line", "budget_id", string="Budget Lines",
        copy=True)
    month_plan_ids = fields.One2many(
        "acme.budget.month.plan", "budget_id",
        string="Monthly Planning")
    category_summary_ids = fields.One2many(
        "acme.budget.category.summary", "budget_id",
        string="Budget Category Summary", readonly=True)
    total_revenue = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    total_cost = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    net_result = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    total_direct_cost = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    total_operating_expense = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    gross_profit = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")
    net_operating_result = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id")

    _department_fiscal_version_unique = models.Constraint(
        "unique(company_id, department_id, fiscal_year, version_number)",
        "Budget versions must be unique per department and fiscal year.")
    _department_budget_reference_unique = models.Constraint(
        "unique(company_id, name)",
        "Budget references must be unique per company.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = "ACME-TMP-%s" % uuid4().hex
            if vals.get("revision_of_id"):
                source = self.browse(vals["revision_of_id"])
                vals.setdefault("version_type", "revision")
                if not vals.get("version_number"):
                    latest = self.search([
                        ("company_id", "=", source.company_id.id),
                        ("department_id", "=", source.department_id.id),
                        ("fiscal_year", "=", source.fiscal_year),
                    ], order="version_number desc", limit=1)
                    vals["version_number"] = max(
                        source.version_number, latest.version_number) + 1
            department_id = vals.get("department_id")
            if not self.env.su and not self.env.user.has_group(
                    "acme_integrated_budget.group_corporate_budget"):
                self._check_user_department(department_id)
        budgets = super().create(vals_list)
        for budget in budgets.filtered(
                lambda record: record.name.startswith("ACME-TMP-")):
            suffix = "" if budget.version_number == 1 else (
                "-R%02d" % budget.version_number)
            budget.write({
                "name": "ACME-BUD-%s-%03d%s" % (
                    budget.fiscal_year, budget.id, suffix),
            })
        return budgets

    def write(self, vals):
        if "state" in vals and not self.env.su:
            if set(vals) != {"state"}:
                raise UserError(_("Change the budget status separately from other fields."))
            for budget in self:
                budget._validate_state_transition(vals["state"])
        protected = set(vals) - {"finance_feedback"}
        if any(budget.state == "approved" for budget in self) and protected:
            raise UserError(_(
                "Approved budgets are locked. Create a revision to make changes."))
        if "finance_feedback" in vals and not self.env.user.has_group(
                "acme_integrated_budget.group_corporate_budget"):
            raise UserError(_("Only Corporate Finance can update review feedback."))
        if ("department_id" in vals and not self.env.su
                and not self.env.user.has_group(
                    "acme_integrated_budget.group_corporate_budget")):
            self._check_user_department(vals["department_id"])
        return super().write(vals)

    @api.depends(
        "line_ids.plan_type", "line_ids.business_type",
        "line_ids.annual_planned")
    def _compute_totals(self):
        for budget in self:
            budget.total_revenue = sum(
                line.annual_planned for line in budget.line_ids
                if line.plan_type == "revenue")
            budget.total_cost = sum(
                line.annual_planned for line in budget.line_ids
                if line.plan_type == "cost")
            budget.total_direct_cost = sum(
                line.annual_planned for line in budget.line_ids
                if line.plan_type == "cost"
                and line.business_type != "support")
            budget.total_operating_expense = sum(
                line.annual_planned for line in budget.line_ids
                if line.plan_type == "cost"
                and line.business_type == "support")
            budget.gross_profit = (
                budget.total_revenue - budget.total_direct_cost)
            budget.net_operating_result = (
                budget.gross_profit - budget.total_operating_expense)
            budget.net_result = budget.total_revenue - budget.total_cost

    @api.constrains("fiscal_year", "date_from", "date_to")
    def _check_dates(self):
        for budget in self:
            if budget.date_from and budget.date_to:
                expected_end = (
                    budget.date_from + relativedelta(months=12)
                    - relativedelta(days=1))
                if budget.date_to != expected_end:
                    raise ValidationError(_(
                        "A fiscal budget period must span twelve monthly periods, "
                        "ending the day before the same date next year."))
                if budget.fiscal_year and budget.fiscal_year.strip() not in (
                        _fiscal_year_labels(budget.date_from, budget.date_to)):
                    raise ValidationError(_(
                        "The fiscal year must match the budget start and end dates."))

    @api.constrains("department_id", "company_id", "fiscal_year",
                    "revision_of_id", "version_type", "version_number",
                    "date_from", "date_to")
    def _check_department_period(self):
        for budget in self:
            if bool(budget.revision_of_id) != (
                    budget.version_type == "revision"):
                raise ValidationError(_(
                    "Only revision budgets can reference another budget."))
            if (budget.version_type == "original"
                    and budget.version_number != 1
                    or budget.version_type == "revision"
                    and budget.version_number < 2):
                raise ValidationError(_("Invalid budget version number."))
            if budget.department_id.company_id and (
                    budget.department_id.company_id != budget.company_id):
                raise ValidationError(
                    _("The department must belong to the budget company."))
            if budget.revision_of_id:
                source = budget.revision_of_id
                if source.state != "approved":
                    raise ValidationError(
                        _("A revision can only be created from an approved budget."))
                if (budget.department_id != source.department_id
                        or budget.company_id != source.company_id
                        or budget.fiscal_year != source.fiscal_year
                        or budget.date_from != source.date_from
                        or budget.date_to != source.date_to):
                    raise ValidationError(
                        _("A revision must keep the original department and fiscal period."))
            elif budget.department_id and budget.fiscal_year:
                duplicate = self.search_count([
                    ("id", "!=", budget.id),
                    ("company_id", "=", budget.company_id.id),
                    ("department_id", "=", budget.department_id.id),
                    ("fiscal_year", "=", budget.fiscal_year),
                    ("revision_of_id", "=", False),
                ])
                if duplicate:
                    raise ValidationError(_(
                        "Only one original budget is allowed per department and fiscal year."))

    def _check_user_department(self, department_id):
        department = self.env["hr.department"].browse(department_id)
        if (not department_id
                or self.env.user not in department.budget_user_ids):
            raise UserError(_(
                "You can only prepare budgets for your own department."))

    def _check_state(self, expected):
        if any(budget.state not in expected for budget in self):
            raise UserError(_("This action is not available in the current budget status."))

    def _validate_state_transition(self, new_state):
        is_corporate = self.env.user.has_group(
            "acme_integrated_budget.group_corporate_budget")
        if new_state == "manager_review":
            self._check_state(("draft", "returned"))
            if not self.line_ids:
                raise UserError(_("Add at least one budget line before submitting."))
            if any(set(line.month_plan_ids.mapped("month_number"))
                   != set(range(1, 13)) for line in self.line_ids):
                raise UserError(_(
                    "Every budget line must contain all twelve monthly periods."))
            if not is_corporate and self.env.user not in self.department_id.budget_user_ids:
                raise UserError(_(
                    "You can only submit a budget for your own department."))
        elif new_state == "finance_review":
            self._check_state(("manager_review",))
            self._check_department_manager()
        elif new_state == "corporate_review":
            self._check_state(("finance_review",))
            if not is_corporate:
                raise UserError(_("Only Corporate Finance can review this budget."))
        elif new_state == "approved":
            self._check_state(("corporate_review",))
            if not is_corporate:
                raise UserError(_("Only Corporate Finance can approve this budget."))
        elif new_state == "returned":
            self._check_state(("finance_review", "corporate_review"))
            if not is_corporate:
                raise UserError(_("Only Corporate Finance can return this budget."))
            if not self.finance_feedback:
                raise UserError(_("Enter feedback before returning the budget."))
        else:
            raise UserError(_("This budget status transition is not allowed."))

    def _check_department_manager(self):
        if self.env.user.has_group("acme_integrated_budget.group_corporate_budget"):
            return
        if any(budget.department_id.budget_manager_user_id != self.env.user
               for budget in self):
            raise UserError(_("Only the department manager can complete this review."))

    def _set_workflow_state(self, state):
        self.write({"state": state})

    def action_submit(self):
        self._set_workflow_state("manager_review")
        self.write({"submission_date": fields.Date.context_today(self)})

    def action_manager_approve(self):
        self._set_workflow_state("finance_review")

    def action_finance_approve(self):
        self._set_workflow_state("corporate_review")

    def action_corporate_approve(self):
        self._set_workflow_state("approved")
        self.env["acme.budget.month.plan"].sudo()._refresh_integrated_monitoring(
            budgets=self)

    def action_return_for_correction(self):
        self._set_workflow_state("returned")

    def action_create_revision(self):
        self.ensure_one()
        self._check_state(("approved",))
        original = self.revision_of_id or self
        latest = self.search([
            ("company_id", "=", self.company_id.id),
            ("department_id", "=", self.department_id.id),
            ("fiscal_year", "=", self.fiscal_year),
        ], order="version_number desc", limit=1)
        next_version = max(self.version_number, latest.version_number) + 1
        return self.copy({
            "name": "%s-R%02d" % (original.name, next_version),
            "budget_name": _("%s (Revision %s)") % (
                original.budget_name, next_version),
            "version_type": "revision",
            "version_number": next_version,
            "revision_of_id": self.id,
            "state": "draft",
            "prepared_by_id": self.env.user.id,
            "finance_feedback": False,
            "approval_reference": False,
        }).get_formview_action()


class DepartmentBudgetLine(models.Model):
    _name = "acme.department.budget.line"
    _description = "Departmental Budget Line"
    _order = "sequence, id"
    _check_company_auto = True

    sequence = fields.Integer(default=10)
    line_reference = fields.Char(
        string="Line ID", compute="_compute_line_reference", store=True)
    budget_id = fields.Many2one(
        "acme.department.budget", required=True, ondelete="cascade",
        index=True, check_company=True)
    company_id = fields.Many2one(related="budget_id.company_id", store=True)
    currency_id = fields.Many2one(related="budget_id.currency_id", store=True)
    department_id = fields.Many2one(
        related="budget_id.department_id", store=True, index=True)
    description = fields.Char(
        string="Line Description / Planning Item", required=True)
    plan_type = fields.Selection(
        [("revenue", "Revenue"), ("cost", "Cost / Expense")],
        required=True, default="cost", index=True)
    category = fields.Char(string="Budget Category", required=True)
    business_type = fields.Selection(
        [
            ("stock", "Stock Sales"),
            ("project", "Project"),
            ("service", "Service"),
            ("support", "Support / Administration"),
            ("other", "Other"),
        ], default="other", required=True)
    cost_category = fields.Char(string="Cost Category")
    department_analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Department Analytic Account",
        check_company=True,
        help="Used with the GL account to allocate transactions to this department.")
    project_analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Project / Analytic Account",
        check_company=True,
        help="Optional additional analytic dimension for transaction matching.")
    project_category_wbs = fields.Char(string="Project Category / WBS")
    product_id = fields.Many2one("product.product", string="Product / Service")
    product_category_id = fields.Many2one(
        "product.category", string="Product Category")
    account_id = fields.Many2one(
        "account.account", string="GL Account", check_company=True)
    account_code = fields.Char(
        string="GL Account Code", related="account_id.code", store=True)
    account_name = fields.Char(
        string="GL Account Name", related="account_id.name")
    customer_id = fields.Many2one("res.partner", string="Customer / Client")
    uom_id = fields.Many2one("uom.uom", string="Unit of Measure")
    planning_basis = fields.Selection(
        [
            ("quantity", "Quantity × Unit Rate"),
            ("manual", "Monthly Amount"),
            ("contract", "Contract Milestone"),
            ("recurring", "Recurring Billing"),
            ("headcount", "Headcount-Based"),
            ("other", "Other"),
        ], default="manual", required=True)
    contract_reference = fields.Char(string="Contract / Reference")
    responsible_owner_id = fields.Many2one(
        "res.users", string="Responsible Owner")
    notes = fields.Text()
    month_plan_ids = fields.One2many(
        "acme.budget.month.plan", "budget_line_id",
        string="Monthly Plan", copy=True)
    annual_planned = fields.Monetary(
        compute="_compute_annual_planned", store=True,
        currency_field="currency_id")
    original_line_id = fields.Many2one(
        "acme.department.budget.line", string="Original Budget Line",
        readonly=True, copy=False, index=True)

    @api.depends("budget_id")
    def _compute_line_reference(self):
        for line in self:
            line.line_reference = (
                "L%03d" % line.id if line.id else False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            budget = self.env["acme.department.budget"].browse(
                vals.get("budget_id"))
            budget._check_state(("draft", "returned"))
            commands = list(vals.get("month_plan_ids") or [])
            existing_months = {
                command[2].get("month_number")
                for command in commands
                if command[0] == fields.Command.CREATE
                and command[2].get("month_number")
            }
            commands.extend(
                fields.Command.create({"month_number": month})
                for month in range(1, 13)
                if month not in existing_months
            )
            vals["month_plan_ids"] = commands
        return super().create(vals_list)

    def copy(self, default=None):
        self.ensure_one()
        default = dict(default or {})
        default.setdefault("original_line_id", self.original_line_id.id or self.id)
        return super().copy(default)

    def write(self, vals):
        if "budget_id" in vals:
            raise UserError(_("A budget line cannot be moved to another budget."))
        self._check_editable()
        return super().write(vals)

    def unlink(self):
        self._check_editable()
        return super().unlink()

    def _check_editable(self):
        for line in self:
            if line.budget_id.state not in ("draft", "returned"):
                raise UserError(_("Budget lines can only be changed in Draft or Returned status."))

    @api.depends("month_plan_ids.planned_amount")
    def _compute_annual_planned(self):
        for line in self:
            line.annual_planned = sum(
                line.month_plan_ids.mapped("planned_amount"))


class BudgetMonthPlan(models.Model):
    _name = "acme.budget.month.plan"
    _description = "Monthly Budget Plan"
    _order = "budget_id, budget_line_id, calendar_month_number, id"
    _check_company_auto = True

    budget_line_id = fields.Many2one(
        "acme.department.budget.line", required=True, ondelete="cascade",
        index=True, check_company=True)
    budget_id = fields.Many2one(
        related="budget_line_id.budget_id", store=True, index=True)
    company_id = fields.Many2one(related="budget_line_id.company_id", store=True)
    currency_id = fields.Many2one(related="budget_line_id.currency_id", store=True)
    department_id = fields.Many2one(
        related="budget_line_id.department_id", store=True, index=True)
    account_id = fields.Many2one(
        related="budget_line_id.account_id", store=True)
    account_code = fields.Char(
        related="budget_line_id.account_code", store=True)
    account_name = fields.Char(related="budget_line_id.account_name")
    line_reference = fields.Char(
        related="budget_line_id.line_reference", store=True)
    description = fields.Char(
        related="budget_line_id.description", store=True)
    plan_type = fields.Selection(
        related="budget_line_id.plan_type", store=True)
    category = fields.Char(related="budget_line_id.category", store=True)
    business_type = fields.Selection(
        related="budget_line_id.business_type", store=True)
    planning_basis = fields.Selection(
        related="budget_line_id.planning_basis", store=True)
    month_number = fields.Integer(required=True)
    period_date = fields.Date(compute="_compute_period", store=True)
    period_name = fields.Char(compute="_compute_period", store=True)
    calendar_month_number = fields.Integer(
        compute="_compute_period", store=True)
    planned_quantity = fields.Float(string="Planned Quantity", copy=True)
    uom_id = fields.Many2one(related="budget_line_id.uom_id", store=True)
    use_quantity_rate = fields.Boolean(
        string="Use Quantity × Rate", copy=True)
    unit_rate = fields.Monetary(
        string="Planned Unit Rate", currency_field="currency_id", copy=True)
    manual_amount = fields.Monetary(
        string="Manual Planned Amount", currency_field="currency_id", copy=True)
    planned_amount = fields.Monetary(
        compute="_compute_planned_amount", store=True,
        currency_field="currency_id")
    original_budget = fields.Monetary(
        string="Original Budget", compute="_compute_budget_versions",
        store=True, currency_field="currency_id")
    approved_revision_amount = fields.Monetary(
        string="Approved Revision (+/-)", compute="_compute_budget_versions",
        store=True, currency_field="currency_id")
    revised_budget = fields.Monetary(
        string="Revised Budget", compute="_compute_budget_versions",
        store=True, currency_field="currency_id")
    ordered_amount = fields.Monetary(
        string="Manual Commitment Adjustment", currency_field="currency_id",
        copy=False)
    invoiced_amount = fields.Monetary(
        string="Manual Invoice Adjustment", currency_field="currency_id",
        copy=False)
    actual_amount = fields.Monetary(
        string="Manual Actual Adjustment", currency_field="currency_id",
        copy=False)
    integrated_ordered_amount = fields.Monetary(
        string="Open Commitment", currency_field="currency_id",
        readonly=True, copy=False)
    integrated_order_total_amount = fields.Monetary(
        string="Gross Order Value", currency_field="currency_id",
        help="Full confirmed Sales/Purchase order value, regardless of invoicing.",
        readonly=True, copy=False)
    integrated_ordered_quantity = fields.Float(
        string="Gross Ordered Quantity", digits="Product Unit",
        help="Full confirmed order quantity in the budget line unit, or the "
             "source order unit when the budget line has no unit configured.",
        readonly=True, copy=False)
    integrated_ordered_uom_id = fields.Many2one(
        "uom.uom", string="Ordered Quantity Unit", readonly=True, copy=False)
    integrated_invoiced_amount = fields.Monetary(
        string="Integrated Invoiced / Billed", currency_field="currency_id",
        readonly=True, copy=False)
    integrated_actual_amount = fields.Monetary(
        string="Integrated Actual", currency_field="currency_id",
        readonly=True, copy=False)
    remaining_amount = fields.Monetary(
        compute="_compute_monitoring", currency_field="currency_id")
    used_percent = fields.Float(
        compute="_compute_monitoring", string="Used %")
    quantity_actual = fields.Float(string="Quantity Actual", copy=False)
    quantity_variance = fields.Float(
        compute="_compute_quantity_variance", string="Quantity Variance")

    _month_number_range = models.Constraint(
        "CHECK(month_number BETWEEN 1 AND 12)",
        "Month number must be between 1 and 12.")
    _unique_month_per_line = models.Constraint(
        "unique(budget_line_id, month_number)",
        "A budget line can have only one plan per month.")

    def write(self, vals):
        if "budget_line_id" in vals:
            raise UserError(_("A monthly plan cannot be moved to another budget line."))
        if {"planned_quantity", "unit_rate"} & set(vals):
            vals = dict(vals, use_quantity_rate=vals.get(
                "use_quantity_rate", True))
        monitoring_fields = {
            "ordered_amount", "invoiced_amount", "actual_amount",
            "integrated_ordered_amount", "integrated_order_total_amount",
            "integrated_ordered_quantity", "integrated_ordered_uom_id",
            "integrated_invoiced_amount", "integrated_actual_amount",
            "quantity_actual",
        }
        integrated_fields = {
            "integrated_ordered_amount", "integrated_order_total_amount",
            "integrated_ordered_quantity", "integrated_ordered_uom_id",
            "integrated_invoiced_amount", "integrated_actual_amount",
        }
        if integrated_fields & set(vals) and not self.env.su:
            raise UserError(_("Integrated monitoring amounts are system managed."))
        if (set(vals) & monitoring_fields and not self.env.su
                and not self.env.user.has_group(
                    "acme_integrated_budget.group_corporate_budget")):
            raise UserError(_(
                "Only Corporate Finance can update actual and commitment tracking."))
        for plan in self:
            if plan.budget_id.state == "approved":
                changed = set(vals)
                if not changed or not changed.issubset(monitoring_fields):
                    raise UserError(_(
                        "Approved monthly plans are locked. Create a budget revision to change the plan."))
                if not self.env.su and not self.env.user.has_group(
                        "acme_integrated_budget.group_corporate_budget"):
                    raise UserError(_(
                        "Only Corporate Finance can update actual and commitment tracking."))
            elif plan.budget_id.state not in ("draft", "returned"):
                raise UserError(_("Monthly plans can only be changed in Draft or Returned status."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if {"planned_quantity", "unit_rate"} & set(vals):
                vals.setdefault("use_quantity_rate", True)
            line = self.env["acme.department.budget.line"].browse(
                vals.get("budget_line_id"))
            line.budget_id._check_state(("draft", "returned"))
            monitoring_fields = {
                "ordered_amount", "invoiced_amount", "actual_amount",
                "integrated_ordered_amount", "integrated_order_total_amount",
                "integrated_ordered_quantity", "integrated_ordered_uom_id",
                "integrated_invoiced_amount", "integrated_actual_amount",
                "quantity_actual",
            }
            integrated_fields = {
                "integrated_ordered_amount", "integrated_order_total_amount",
                "integrated_ordered_quantity", "integrated_ordered_uom_id",
                "integrated_invoiced_amount", "integrated_actual_amount",
            }
            if integrated_fields & set(vals) and not self.env.su:
                raise UserError(_(
                    "Integrated monitoring figures are system managed."))
            if (set(vals) & monitoring_fields and not self.env.su
                    and not self.env.user.has_group(
                        "acme_integrated_budget.group_corporate_budget")):
                raise UserError(_(
                    "Only Corporate Finance can update actual and commitment tracking."))
        return super().create(vals_list)

    def unlink(self):
        for plan in self:
            if plan.budget_id.state not in ("draft", "returned"):
                raise UserError(_("Monthly plans cannot be deleted after submission."))
        if self:
            raise UserError(_(
                "Monthly periods cannot be deleted. Remove the budget line if it is no longer needed."))
        return super().unlink()

    @api.depends("month_number", "budget_line_id.budget_id.date_from")
    def _compute_period(self):
        for plan in self:
            start = plan.budget_line_id.budget_id.date_from
            if start and plan.month_number:
                month = start + relativedelta(months=plan.month_number - 1)
                plan.period_date = month
                plan.period_name = month.strftime("%B")
                plan.calendar_month_number = month.month
            else:
                plan.period_date = False
                plan.period_name = False
                plan.calendar_month_number = False

    @api.depends(
        "use_quantity_rate", "planned_quantity", "unit_rate", "manual_amount")
    def _compute_planned_amount(self):
        for plan in self:
            if plan.use_quantity_rate:
                plan.planned_amount = plan.planned_quantity * plan.unit_rate
            else:
                plan.planned_amount = plan.manual_amount

    @api.depends(
        "planned_amount", "month_number",
        "budget_line_id.original_line_id.month_plan_ids.month_number",
        "budget_line_id.original_line_id.month_plan_ids.planned_amount",
        "budget_line_id.budget_id.version_type",
        "budget_line_id.budget_id.state")
    def _compute_budget_versions(self):
        for plan in self:
            budget = plan.budget_id
            original_line = plan.budget_line_id.original_line_id
            original_month = original_line.month_plan_ids.filtered(
                lambda month: month.month_number == plan.month_number)
            if budget.version_type == "revision":
                original_amount = (
                    original_month.planned_amount if original_month else 0.0)
                if budget.state == "approved":
                    plan.approved_revision_amount = (
                        plan.planned_amount - original_amount)
                    plan.revised_budget = plan.planned_amount
                else:
                    plan.approved_revision_amount = 0.0
                    plan.revised_budget = original_amount
            else:
                plan.approved_revision_amount = 0.0
                plan.revised_budget = plan.planned_amount
                original_amount = plan.planned_amount
            plan.original_budget = original_amount

    @api.depends(
        "revised_budget", "actual_amount", "integrated_actual_amount")
    def _compute_monitoring(self):
        for plan in self:
            actual_amount = (
                plan.actual_amount + plan.integrated_actual_amount)
            plan.remaining_amount = plan.revised_budget - actual_amount
            plan.used_percent = (
                actual_amount / plan.revised_budget
                if plan.revised_budget else 0.0)

    @api.depends("planned_quantity", "quantity_actual")
    def _compute_quantity_variance(self):
        for plan in self:
            plan.quantity_variance = (
                plan.quantity_actual - plan.planned_quantity)


class DepartmentBudgetCategorySummary(models.Model):
    _name = "acme.budget.category.summary"
    _description = "Department Budget Category Summary"
    _auto = False
    _order = "budget_id, plan_type, category"

    budget_id = fields.Many2one("acme.department.budget", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    department_id = fields.Many2one("hr.department", readonly=True)
    currency_id = fields.Many2one("res.currency", readonly=True)
    plan_type = fields.Selection(
        [("revenue", "Revenue"), ("cost", "Cost / Expense")], readonly=True)
    category = fields.Char(readonly=True)
    annual_planned = fields.Monetary(readonly=True)
    approved_revision_amount = fields.Monetary(readonly=True)
    revised_budget = fields.Monetary(readonly=True)
    ordered_amount = fields.Monetary(
        string="Open Commitment", readonly=True)
    gross_order_value = fields.Monetary(
        string="Gross Order Value", readonly=True)
    invoiced_amount = fields.Monetary(readonly=True)
    actual_amount = fields.Monetary(readonly=True)
    remaining_amount = fields.Monetary(readonly=True)
    used_percent = fields.Float(readonly=True)

    def init(self):
        drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW acme_budget_category_summary AS (
                SELECT
                    MIN(plan.id) AS id,
                    budget.id AS budget_id,
                    budget.company_id AS company_id,
                    budget.department_id AS department_id,
                    company.currency_id AS currency_id,
                    line.plan_type AS plan_type,
                    line.category AS category,
                    SUM(plan.original_budget) AS annual_planned,
                    SUM(plan.approved_revision_amount)
                        AS approved_revision_amount,
                    SUM(plan.revised_budget) AS revised_budget,
                    SUM(COALESCE(plan.ordered_amount, 0)
                        + COALESCE(plan.integrated_ordered_amount, 0))
                        AS ordered_amount,
                    SUM(COALESCE(plan.integrated_order_total_amount, 0))
                        AS gross_order_value,
                    SUM(COALESCE(plan.invoiced_amount, 0)
                        + COALESCE(plan.integrated_invoiced_amount, 0))
                        AS invoiced_amount,
                    SUM(COALESCE(plan.actual_amount, 0)
                        + COALESCE(plan.integrated_actual_amount, 0))
                        AS actual_amount,
                    SUM(plan.revised_budget)
                        - SUM(COALESCE(plan.actual_amount, 0)
                            + COALESCE(plan.integrated_actual_amount, 0))
                        AS remaining_amount,
                    CASE
                        WHEN SUM(plan.revised_budget) = 0 THEN 0
                        ELSE SUM(COALESCE(plan.actual_amount, 0)
                            + COALESCE(plan.integrated_actual_amount, 0))
                            / SUM(plan.revised_budget)
                    END AS used_percent
                FROM acme_budget_month_plan plan
                JOIN acme_department_budget_line line
                    ON line.id = plan.budget_line_id
                JOIN acme_department_budget budget
                    ON budget.id = line.budget_id
                JOIN res_company company ON company.id = budget.company_id
                GROUP BY budget.id, budget.company_id, budget.department_id,
                    company.currency_id, line.plan_type, line.category
            )
        """)


class CorporateBudget(models.Model):
    _name = "acme.corporate.budget"
    _description = "Corporate Budget Consolidation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_from desc, name"
    _check_company_auto = True

    name = fields.Char(
        string="Corporate Budget Reference", required=True, copy=False,
        default="New", tracking=True)
    budget_name = fields.Char(
        string="Corporate Business Plan", required=True,
        default="ACME Corporate Annual Budget")
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        index=True, tracking=True)
    currency_id = fields.Many2one(related="company_id.currency_id", store=True)
    fiscal_year = fields.Char(required=True, tracking=True)
    date_from = fields.Date(string="Start Date", required=True)
    date_to = fields.Date(string="End Date", required=True)
    consolidation_date = fields.Date(
        default=fields.Date.context_today, required=True)
    consolidated_by_id = fields.Many2one(
        "res.users", string="Consolidated By", required=True,
        default=lambda self: self.env.user)
    department_ids = fields.Many2many(
        "hr.department", "acme_corporate_budget_department_rel",
        "budget_id", "department_id", string="Departments in Scope",
        check_company=True)
    department_budget_ids = fields.Many2many(
        "acme.department.budget", "acme_corporate_budget_source_rel",
        "corporate_budget_id", "department_budget_id",
        string="Approved Departmental Budgets", copy=False)
    pending_department_ids = fields.Many2many(
        "hr.department", compute="_compute_pending_departments",
        string="Departments Pending Submission")
    state = fields.Selection(
        [("draft", "Draft"), ("reviewed", "Reviewed"), ("approved", "Approved")],
        default="draft", required=True, index=True, copy=False, tracking=True)
    total_revenue = fields.Monetary(
        compute="_compute_totals", currency_field="currency_id")
    total_cost = fields.Monetary(
        compute="_compute_totals", currency_field="currency_id")
    net_result = fields.Monetary(
        compute="_compute_totals", currency_field="currency_id")

    _company_fiscal_year_unique = models.Constraint(
        "unique(company_id, fiscal_year)",
        "Only one corporate consolidation is allowed per company and fiscal year.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New" and vals.get("fiscal_year"):
                vals["name"] = "ACME-CORP-BUD-%s" % vals["fiscal_year"]
        return super().create(vals_list)

    def write(self, vals):
        if any(budget.state == "approved" for budget in self):
            raise UserError(_(
                "Approved corporate consolidations are locked. Create a new consolidation for changes."))
        if "state" in vals and not self.env.su:
            if set(vals) != {"state"}:
                raise UserError(_(
                    "Change the consolidation status separately from other fields."))
            for budget in self:
                budget._validate_state_transition(vals["state"])
        return super().write(vals)

    @api.onchange("department_budget_ids")
    def _onchange_department_budget_ids(self):
        for budget in self:
            budget.department_ids = (
                budget.department_ids
                | budget.department_budget_ids.mapped("department_id"))

    @api.depends("department_ids", "department_budget_ids.department_id")
    def _compute_pending_departments(self):
        for budget in self:
            included = set(budget.department_budget_ids.department_id.ids)
            budget.pending_department_ids = budget.department_ids.filtered(
                lambda department: department.id not in included)

    @api.depends("department_budget_ids.total_revenue",
                 "department_budget_ids.total_cost")
    def _compute_totals(self):
        for budget in self:
            budget.total_revenue = sum(
                budget.department_budget_ids.mapped("total_revenue"))
            budget.total_cost = sum(
                budget.department_budget_ids.mapped("total_cost"))
            budget.net_result = budget.total_revenue - budget.total_cost

    @api.constrains(
        "company_id", "fiscal_year", "date_from", "date_to",
        "department_ids", "department_budget_ids")
    def _check_consolidation_sources(self):
        for budget in self:
            if budget.date_from and budget.date_to:
                expected_end = (
                    budget.date_from + relativedelta(months=12)
                    - relativedelta(days=1))
                if budget.date_to != expected_end:
                    raise ValidationError(_(
                        "A fiscal budget period must span twelve monthly periods, "
                        "ending the day before the same date next year."))
                if budget.fiscal_year and budget.fiscal_year.strip() not in (
                        _fiscal_year_labels(budget.date_from, budget.date_to)):
                    raise ValidationError(_(
                        "The fiscal year must match the budget start and end dates."))
            if budget.department_budget_ids.filtered(
                    lambda source: source.state != "approved"):
                raise ValidationError(_(
                    "Only approved departmental budgets can be consolidated."))
            if budget.department_budget_ids.filtered(
                    lambda source: source.company_id != budget.company_id
                    or source.fiscal_year != budget.fiscal_year
                    or source.date_from != budget.date_from
                    or source.date_to != budget.date_to):
                raise ValidationError(_(
                    "Consolidated budgets must match the company and fiscal period."))
            department_ids = budget.department_budget_ids.department_id.ids
            if len(department_ids) != len(set(department_ids)):
                raise ValidationError(_(
                    "A corporate consolidation can include only one approved budget per department."))
            if budget.department_budget_ids.filtered(
                    lambda source: source.department_id not in budget.department_ids):
                raise ValidationError(_(
                    "Every consolidated department must be included in the scope."))

    def _check_state(self, expected):
        if any(budget.state not in expected for budget in self):
            raise UserError(_("This action is not available in the current consolidation status."))

    def _check_latest_department_budgets(self):
        for budget in self:
            for source in budget.department_budget_ids:
                latest = self.env["acme.department.budget"].search([
                    ("company_id", "=", budget.company_id.id),
                    ("fiscal_year", "=", budget.fiscal_year),
                    ("date_from", "=", budget.date_from),
                    ("date_to", "=", budget.date_to),
                    ("department_id", "=", source.department_id.id),
                    ("state", "=", "approved"),
                ], order="version_number desc, create_date desc", limit=1)
                if latest != source:
                    raise UserError(_(
                        "A newer approved revision exists for %(department)s. Reload approved budgets before approval.",
                        department=source.department_id.display_name))

    def _validate_state_transition(self, new_state):
        if new_state == "reviewed":
            self._check_state(("draft",))
            if any(not budget.department_budget_ids for budget in self):
                raise UserError(_("Load at least one approved departmental budget first."))
        elif new_state == "approved":
            self._check_state(("reviewed",))
            if any(budget.pending_department_ids for budget in self):
                raise UserError(_(
                    "All departments in scope must have an approved submission before corporate approval."))
            self._check_latest_department_budgets()
        else:
            raise UserError(_("This consolidation status transition is not allowed."))

    def action_load_approved_budgets(self):
        self.ensure_one()
        self._check_state(("draft", "reviewed"))
        domain = [
            ("company_id", "=", self.company_id.id),
            ("fiscal_year", "=", self.fiscal_year),
            ("date_from", "=", self.date_from),
            ("date_to", "=", self.date_to),
        ]
        if self.department_ids:
            domain.append(("department_id", "in", self.department_ids.ids))
        candidates = self.env["acme.department.budget"].search(
            domain, order="department_id, version_number desc, create_date desc")
        scope_departments = (
            self.department_ids or candidates.mapped("department_id"))
        latest_by_department = {}
        for candidate in candidates:
            if candidate.state == "approved":
                latest_by_department.setdefault(
                    candidate.department_id.id, candidate.id)
        values = {
            "department_budget_ids": [
                fields.Command.set(list(latest_by_department.values()))],
        }
        if not self.department_ids:
            values["department_ids"] = [
                fields.Command.set(scope_departments.ids)]
        self.write(values)

    def action_mark_reviewed(self):
        self._validate_state_transition("reviewed")
        self.write({"state": "reviewed"})

    def action_approve(self):
        self._validate_state_transition("approved")
        self.write({"state": "approved"})


class BudgetVarianceAnalysis(models.Model):
    _name = "acme.budget.variance.analysis"
    _description = "Budget Variance Analysis"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _check_company_auto = True

    name = fields.Char(
        string="Analysis Reference", required=True, readonly=True,
        copy=False, default="New", tracking=True)
    corporate_budget_id = fields.Many2one(
        "acme.corporate.budget", string="Corporate Budget", required=True,
        check_company=True, ondelete="restrict", tracking=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        readonly=True)
    currency_id = fields.Many2one(
        related="corporate_budget_id.currency_id", store=True, readonly=True)
    date_from = fields.Date(string="Analysis From", required=True)
    date_to = fields.Date(string="Analysis To", required=True)
    analysis_period = fields.Char(
        compute="_compute_analysis_period", string="Period")
    analysis_date = fields.Date(
        string="Analysis Date", required=True,
        default=fields.Date.context_today)
    prepared_by_id = fields.Many2one(
        "res.users", string="Prepared By", required=True,
        default=lambda self: self.env.user)
    state = fields.Selection(
        [("draft", "Draft"), ("computed", "Computed")],
        default="draft", required=True, tracking=True)
    analysis_line_ids = fields.One2many(
        "acme.budget.variance.analysis.line", "analysis_id",
        string="Analysis Lines", copy=False)
    variance_line_ids = fields.One2many(
        "acme.budget.variance.analysis.line", "analysis_id",
        string="Variance Lines",
        domain=[("line_type", "=", "variance")])
    gl_summary_line_ids = fields.One2many(
        "acme.budget.variance.analysis.line", "analysis_id",
        string="GL Account Summary",
        domain=[("line_type", "=", "account")])
    dimension_summary_line_ids = fields.One2many(
        "acme.budget.variance.analysis.line", "analysis_id",
        string="Cost Center / Segment / Project Summary",
        domain=[("line_type", "=", "dimension")])
    total_planned = fields.Monetary(
        string="Total Planned", compute="_compute_totals",
        currency_field="currency_id")
    total_actual = fields.Monetary(
        string="Total Actual Spent", compute="_compute_totals",
        currency_field="currency_id")
    total_variance = fields.Monetary(
        string="Total Variance", compute="_compute_totals",
        currency_field="currency_id")
    variance_percent = fields.Float(
        string="Variance %", compute="_compute_totals")
    remaining_percent = fields.Float(
        string="Remaining %", compute="_compute_totals")

    @api.model_create_multi
    def create(self, vals_list):
        today = fields.Date.context_today(self)
        for vals in vals_list:
            corporate = self.env["acme.corporate.budget"].browse(
                vals.get("corporate_budget_id"))
            if corporate:
                vals.setdefault("company_id", corporate.company_id.id)
                default_from, default_to = self._current_period_dates(
                    corporate, today)
                vals.setdefault("date_from", default_from)
                if not vals.get("date_to"):
                    date_from = fields.Date.to_date(vals["date_from"])
                    vals["date_to"] = min(
                        max(today, date_from), corporate.date_to)
                vals.setdefault("analysis_date", vals["date_to"])
        analyses = super().create(vals_list)
        for analysis in analyses.filtered(lambda record: record.name == "New"):
            analysis.name = "BVA/%05d" % analysis.id
        return analyses

    @api.model
    def _current_period_dates(self, corporate, today):
        start = corporate.date_from
        today = min(max(today, start), corporate.date_to)
        month_offset = (
            (today.year - start.year) * 12 + today.month - start.month)
        period_start = start + relativedelta(months=month_offset)
        if period_start > today:
            month_offset -= 1
            period_start = start + relativedelta(months=month_offset)
        period_end = min(
            start + relativedelta(months=month_offset + 1)
            - relativedelta(days=1),
            corporate.date_to)
        return period_start, min(today, period_end)

    @api.onchange("corporate_budget_id")
    def _onchange_corporate_budget_id(self):
        today = fields.Date.context_today(self)
        for analysis in self:
            corporate = analysis.corporate_budget_id
            if not corporate:
                continue
            analysis.company_id = corporate.company_id
            analysis.date_from, analysis.date_to = (
                self._current_period_dates(corporate, today))
            analysis.analysis_date = analysis.date_to

    @api.depends(
        "date_from", "date_to", "corporate_budget_id.date_from",
        "corporate_budget_id.date_to")
    def _compute_analysis_period(self):
        for analysis in self:
            if not (analysis.date_from and analysis.date_to):
                analysis.analysis_period = False
            elif (analysis.date_from.year == analysis.date_to.year
                  and analysis.date_from.month == analysis.date_to.month):
                analysis.analysis_period = analysis.date_from.strftime("%B")
            elif analysis.corporate_budget_id:
                start = analysis.corporate_budget_id.date_from
                month_offset = (
                    (analysis.date_from.year - start.year) * 12
                    + analysis.date_from.month - start.month)
                period_start = start + relativedelta(months=month_offset)
                if period_start > analysis.date_from:
                    month_offset -= 1
                    period_start = start + relativedelta(
                        months=month_offset)
                period_end = min(
                    period_start + relativedelta(months=1)
                    - relativedelta(days=1),
                    analysis.corporate_budget_id.date_to)
                if (period_start <= analysis.date_from
                        and analysis.date_to <= period_end):
                    analysis.analysis_period = period_start.strftime("%B")
                else:
                    analysis.analysis_period = "%s - %s" % (
                        analysis.date_from.strftime("%b %Y"),
                        analysis.date_to.strftime("%b %Y"))
            else:
                analysis.analysis_period = "%s - %s" % (
                    analysis.date_from.strftime("%b %Y"),
                    analysis.date_to.strftime("%b %Y"))

    @api.constrains(
        "company_id", "corporate_budget_id", "date_from", "date_to")
    def _check_analysis_period(self):
        for analysis in self:
            if not analysis.corporate_budget_id:
                continue
            corporate = analysis.corporate_budget_id
            if analysis.company_id != corporate.company_id:
                raise ValidationError(_(
                    "The corporate budget and analysis must belong to the same company."))
            if not (analysis.date_from and analysis.date_to):
                continue
            if analysis.date_from > analysis.date_to:
                raise ValidationError(_(
                    "The analysis start date must be on or before its end date."))
            if (analysis.date_from < corporate.date_from
                    or analysis.date_to > corporate.date_to):
                raise ValidationError(_(
                    "The analysis period must stay within the corporate budget period."))

    @api.depends(
        "analysis_line_ids.line_type",
        "analysis_line_ids.planned_amount",
        "analysis_line_ids.actual_amount",
        "analysis_line_ids.variance_amount",
    )
    def _compute_totals(self):
        for analysis in self:
            detail_lines = analysis.analysis_line_ids.filtered(
                lambda line: line.line_type == "variance")
            analysis.total_planned = sum(
                detail_lines.mapped("planned_amount"))
            analysis.total_actual = sum(
                detail_lines.mapped("actual_amount"))
            analysis.total_variance = sum(
                detail_lines.mapped("variance_amount"))
            percent = (
                analysis.total_variance / analysis.total_planned * 100
                if analysis.total_planned else 0.0)
            analysis.variance_percent = percent
            analysis.remaining_percent = percent

    def action_compute(self):
        self.ensure_one()
        if not self.env.user.has_group(
                "acme_integrated_budget.group_corporate_budget"):
            raise UserError(_(
                "Only the Corporate Budget Office can compute this analysis."))
        if not self.corporate_budget_id.department_budget_ids:
            raise UserError(_(
                "The corporate budget must include approved departmental budgets."))

        budgets = self.corporate_budget_id.department_budget_ids
        budget_lines = budgets.mapped("line_ids")
        line_values = {}

        def values_for_line(line):
            return line_values.setdefault(line.id, {
                "line_type": "variance",
                "budget_line_id": line.id,
                "department_id": line.department_id.id,
                "department_analytic_account_id":
                    line.department_analytic_account_id.id,
                "project_analytic_account_id":
                    line.project_analytic_account_id.id,
                "business_type": line.business_type or False,
                "plan_type": line.plan_type,
                "account_id": line.account_id.id,
                "description": line.description or False,
                "product_id": line.product_id.id,
                "planned_amount": 0.0,
                "actual_amount": 0.0,
            })

        plans = self.env["acme.budget.month.plan"].search([
            ("budget_id", "in", budgets.ids),
            ("period_date", "<=", self.date_to),
        ])
        for plan in plans:
            period_start = plan.period_date
            period_end = min(
                period_start + relativedelta(months=1)
                - relativedelta(days=1),
                plan.budget_id.date_to)
            overlap_start = max(self.date_from, period_start)
            overlap_end = min(self.date_to, period_end)
            if overlap_start > overlap_end:
                continue
            period_days = (period_end - period_start).days or 1
            analysis_days = (overlap_end - overlap_start).days or 1
            ratio = min(analysis_days / period_days, 1.0)
            values = values_for_line(plan.budget_line_id)
            values["planned_amount"] += plan.revised_budget * ratio
            values["actual_amount"] += plan.actual_amount * ratio

        lines_by_company_account = {}
        for line in budget_lines:
            if not line.account_id or not line.department_analytic_account_id:
                continue
            key = (line.company_id.id, line.account_id.id)
            lines_by_company_account.setdefault(key, []).append(line)

        account_ids = budget_lines.mapped("account_id").ids
        if account_ids:
            move_lines = self.env["account.move.line"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("account_id", "in", account_ids),
                ("parent_state", "=", "posted"),
                ("date", ">=", self.date_from),
                ("date", "<=", self.date_to),
                ("display_type", "in", (
                    "product", "cogs", "discount", "rounding",
                    "non_deductible_product",
                    "non_deductible_product_total",
                )),
            ])
            for move_line in move_lines:
                candidates = lines_by_company_account.get(
                    (move_line.company_id.id, move_line.account_id.id), ())
                grouped_by_budget = {}
                for line in candidates:
                    grouped_by_budget.setdefault(
                        line.budget_id.id, []).append(line)
                for analytic_key, percentage in (
                        move_line.analytic_distribution or {}).items():
                    analytic_ids = {
                        int(account_id)
                        for account_id in str(analytic_key).split(",")
                        if account_id
                    }
                    share = float(percentage) / 100
                    if not analytic_ids or not share:
                        continue
                    for budget_group in grouped_by_budget.values():
                        matching = [
                            line for line in budget_group
                            if line.department_analytic_account_id.id
                            in analytic_ids
                            and (not line.project_analytic_account_id
                                 or line.project_analytic_account_id.id
                                 in analytic_ids)
                            and (not line.product_id
                                 or line.product_id.id
                                 == move_line.product_id.id)
                        ]
                        project_specific = [
                            line for line in matching
                            if line.project_analytic_account_id
                        ]
                        if project_specific:
                            matching = project_specific
                        product_specific = [
                            line for line in matching if line.product_id
                        ]
                        if product_specific:
                            matching = product_specific
                        if len(matching) != 1:
                            continue
                        line = matching[0]
                        amount = (
                            -move_line.balance if line.plan_type == "revenue"
                            else move_line.balance)
                        values_for_line(line)["actual_amount"] += amount * share

        aggregates = {}

        def add_to_group(line_type, key, source):
            group_key = (line_type, key)
            values = aggregates.setdefault(group_key, {
                "line_type": line_type,
                "department_id": source["department_id"],
                "department_analytic_account_id":
                    source["department_analytic_account_id"],
                "project_analytic_account_id":
                    source["project_analytic_account_id"],
                "business_type": source["business_type"],
                "plan_type": source["plan_type"],
                "account_id": source["account_id"],
                "budget_line_id": source["budget_line_id"],
                "description": source["description"],
                "product_id": source["product_id"],
                "planned_amount": 0.0,
                "actual_amount": 0.0,
            })
            values["planned_amount"] += source["planned_amount"]
            values["actual_amount"] += source["actual_amount"]

        for line_id, source in line_values.items():
            add_to_group("variance", line_id, source)
            add_to_group(
                "account", (source["account_id"], source["plan_type"]),
                source)
            dimension_key = (
                source["department_id"],
                source["business_type"],
                source["department_analytic_account_id"],
                source["project_analytic_account_id"],
                source["plan_type"],
            )
            add_to_group("dimension", dimension_key, source)

        self.analysis_line_ids.sudo().unlink()
        values = [
            dict(data, analysis_id=self.id)
            for data in aggregates.values()
        ]
        if values:
            self.env["acme.budget.variance.analysis.line"].sudo().create(
                values)
        self.state = "computed"
        return True


class BudgetVarianceAnalysisLine(models.Model):
    _name = "acme.budget.variance.analysis.line"
    _description = "Budget Variance Analysis Line"
    _order = "line_type, department_id, account_id, description"
    _check_company_auto = True

    analysis_id = fields.Many2one(
        "acme.budget.variance.analysis", required=True, ondelete="cascade",
        index=True, check_company=True)
    company_id = fields.Many2one(
        related="analysis_id.company_id", store=True, readonly=True)
    currency_id = fields.Many2one(
        related="analysis_id.currency_id", store=True, readonly=True)
    line_type = fields.Selection(
        [
            ("variance", "Variance Line"),
            ("account", "GL Account Summary"),
            ("dimension", "Dimension Summary"),
        ], required=True, readonly=True, index=True)
    budget_line_id = fields.Many2one(
        "acme.department.budget.line", readonly=True)
    department_id = fields.Many2one("hr.department", readonly=True)
    department_analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Cost Center / Segment",
        readonly=True)
    project_analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Project / Analytic Account",
        readonly=True)
    business_type = fields.Selection(
        [
            ("stock", "Stock Sales"),
            ("project", "Project"),
            ("service", "Service"),
            ("support", "Support / Administration"),
            ("other", "Other"),
        ], string="Segment", readonly=True)
    plan_type = fields.Selection(
        [("revenue", "Revenue"), ("cost", "Cost / Expense")],
        readonly=True)
    account_id = fields.Many2one("account.account", string="GL Account",
                                 readonly=True)
    description = fields.Char(readonly=True)
    product_id = fields.Many2one("product.product", readonly=True)
    planned_amount = fields.Monetary(
        string="Planned", currency_field="currency_id", readonly=True)
    actual_amount = fields.Monetary(
        string="Actual Spent", currency_field="currency_id", readonly=True)
    variance_amount = fields.Monetary(
        string="Variance", compute="_compute_variance",
        currency_field="currency_id")
    variance_percent = fields.Float(
        string="Variance %", compute="_compute_variance")
    remaining_percent = fields.Float(
        string="Remaining %", compute="_compute_variance")
    status = fields.Selection(
        [("favorable", "Favorable"), ("unfavorable", "Unfavorable")],
        compute="_compute_variance")

    @api.depends("planned_amount", "actual_amount", "plan_type")
    def _compute_variance(self):
        for line in self:
            variance = line.planned_amount - line.actual_amount
            percent = (
                variance / line.planned_amount * 100
                if line.planned_amount else 0.0)
            line.variance_amount = variance
            line.variance_percent = percent
            line.remaining_percent = percent
            favorable = (
                variance >= 0 if line.plan_type == "cost"
                else variance <= 0)
            line.status = "favorable" if favorable else "unfavorable"


class DepartmentBudgetReport(models.Model):
    _name = "acme.budget.department.report"
    _description = "Departmental Budget Analysis"
    _auto = False
    _order = "department_id, period_date"

    budget_id = fields.Many2one("acme.department.budget", readonly=True)
    budget_line_id = fields.Many2one(
        "acme.department.budget.line", readonly=True)
    department_id = fields.Many2one("hr.department", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    line_reference = fields.Char(readonly=True)
    description = fields.Char(readonly=True)
    period_date = fields.Date(readonly=True)
    plan_type = fields.Selection(
        [("revenue", "Revenue"), ("cost", "Cost / Expense")], readonly=True)
    category = fields.Char(readonly=True)
    cost_category = fields.Char(readonly=True)
    business_type = fields.Selection(
        [
            ("stock", "Stock Sales"),
            ("project", "Project"),
            ("service", "Service"),
            ("support", "Support / Administration"),
            ("other", "Other"),
        ], readonly=True)
    department_analytic_account_id = fields.Many2one(
        "account.analytic.account", readonly=True)
    project_analytic_account_id = fields.Many2one(
        "account.analytic.account", readonly=True)
    project_category_wbs = fields.Char(readonly=True)
    product_id = fields.Many2one("product.product", readonly=True)
    product_category_id = fields.Many2one("product.category", readonly=True)
    account_id = fields.Many2one("account.account", readonly=True)
    account_code = fields.Char(readonly=True)
    account_name = fields.Char(related="account_id.name", readonly=True)
    customer_id = fields.Many2one("res.partner", readonly=True)
    uom_id = fields.Many2one("uom.uom", readonly=True)
    planning_basis = fields.Selection(
        [
            ("quantity", "Quantity × Unit Rate"),
            ("manual", "Monthly Amount"),
            ("contract", "Contract Milestone"),
            ("recurring", "Recurring Billing"),
            ("headcount", "Headcount-Based"),
            ("other", "Other"),
        ], readonly=True)
    use_quantity_rate = fields.Boolean(readonly=True)
    contract_reference = fields.Char(readonly=True)
    responsible_owner_id = fields.Many2one("res.users", readonly=True)
    planned_amount = fields.Monetary(readonly=True)
    original_budget = fields.Monetary(readonly=True)
    approved_revision_amount = fields.Monetary(readonly=True)
    revised_budget = fields.Monetary(readonly=True)
    ordered_amount = fields.Monetary(
        string="Open Commitment", readonly=True)
    integrated_order_total_amount = fields.Monetary(
        string="Gross Order Value", readonly=True)
    integrated_ordered_quantity = fields.Float(
        string="Gross Ordered Quantity", readonly=True,
        digits="Product Unit")
    invoiced_amount = fields.Monetary(readonly=True)
    actual_amount = fields.Monetary(readonly=True)
    remaining_amount = fields.Monetary(readonly=True)
    used_percent = fields.Float(readonly=True)
    quantity_actual = fields.Float(readonly=True)
    quantity_variance = fields.Float(readonly=True)
    currency_id = fields.Many2one("res.currency", readonly=True)

    def init(self):
        drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW acme_budget_department_report AS (
                SELECT
                    plan.id AS id,
                    budget.id AS budget_id,
                    line.id AS budget_line_id,
                    budget.department_id AS department_id,
                    budget.company_id AS company_id,
                    line.line_reference AS line_reference,
                    line.description AS description,
                    plan.period_date AS period_date,
                    line.plan_type AS plan_type,
                    line.category AS category,
                    line.cost_category AS cost_category,
                    line.business_type AS business_type,
                    line.department_analytic_account_id
                        AS department_analytic_account_id,
                    line.project_analytic_account_id
                        AS project_analytic_account_id,
                    line.project_category_wbs AS project_category_wbs,
                    line.product_id AS product_id,
                    line.product_category_id AS product_category_id,
                    line.account_id AS account_id,
                    line.account_code AS account_code,
                    line.customer_id AS customer_id,
                    COALESCE(line.uom_id, plan.integrated_ordered_uom_id)
                        AS uom_id,
                    line.planning_basis AS planning_basis,
                    plan.use_quantity_rate AS use_quantity_rate,
                    line.contract_reference AS contract_reference,
                    line.responsible_owner_id AS responsible_owner_id,
                    plan.planned_amount AS planned_amount,
                    plan.original_budget AS original_budget,
                    plan.approved_revision_amount AS approved_revision_amount,
                    plan.revised_budget AS revised_budget,
                    COALESCE(plan.ordered_amount, 0)
                        + COALESCE(plan.integrated_ordered_amount, 0)
                        AS ordered_amount,
                    COALESCE(plan.integrated_order_total_amount, 0)
                        AS integrated_order_total_amount,
                    COALESCE(plan.integrated_ordered_quantity, 0)
                        AS integrated_ordered_quantity,
                    COALESCE(plan.invoiced_amount, 0)
                        + COALESCE(plan.integrated_invoiced_amount, 0)
                        AS invoiced_amount,
                    COALESCE(plan.actual_amount, 0)
                        + COALESCE(plan.integrated_actual_amount, 0)
                        AS actual_amount,
                    plan.revised_budget
                        - COALESCE(plan.actual_amount, 0)
                        - COALESCE(plan.integrated_actual_amount, 0)
                        AS remaining_amount,
                    CASE
                        WHEN plan.revised_budget = 0 THEN 0
                        ELSE (COALESCE(plan.actual_amount, 0)
                            + COALESCE(plan.integrated_actual_amount, 0))
                            / plan.revised_budget
                    END AS used_percent,
                    plan.quantity_actual AS quantity_actual,
                    plan.quantity_actual - plan.planned_quantity
                        AS quantity_variance,
                    company.currency_id AS currency_id
                FROM acme_budget_month_plan plan
                JOIN acme_department_budget_line line
                    ON line.id = plan.budget_line_id
                JOIN acme_department_budget budget
                    ON budget.id = line.budget_id
                JOIN res_company company ON company.id = budget.company_id
                WHERE budget.state = 'approved'
            )
        """)
