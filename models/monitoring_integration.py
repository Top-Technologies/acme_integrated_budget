import logging
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


_logger = logging.getLogger(__name__)


def _analytic_distribution_shares(distribution):
    for key, percentage in (distribution or {}).items():
        analytic_ids = {
            int(account_id) for account_id in str(key).split(",")
            if account_id
        }
        share = float(percentage) / 100
        if analytic_ids and share:
            yield analytic_ids, share


def _uom_root_id(uom):
    while uom and uom.relative_uom_id:
        uom = uom.relative_uom_id
    return uom.id if uom else False


class BudgetMonthPlanIntegration(models.Model):
    _inherit = "acme.budget.month.plan"

    @api.model
    def _refresh_integrated_monitoring(self, budgets=None, companies=None):
        Budget = self.env["acme.department.budget"].sudo()
        if budgets is not None:
            budgets = budgets.sudo()
        elif companies is not None:
            budgets = Budget.search([
                ("company_id", "in", companies.ids),
            ])
        else:
            budgets = Budget.search([])

        plans = budgets.mapped("month_plan_ids").sudo()
        stats = {
            "plan_count": len(plans),
            "unmapped_line_count": 0,
            "unmapped_quantity_line_count": 0,
            "unmatched_source_count": 0,
            "ambiguous_match_count": 0,
        }
        if not plans:
            return stats

        unmapped_quantity_line_ids = set()
        lines_by_company_account = defaultdict(list)
        lines = plans.mapped("budget_line_id")
        for line in lines:
            if not line.account_id or not line.department_analytic_account_id:
                stats["unmapped_line_count"] += 1
                continue
            key = (line.company_id.id, line.account_id.id)
            lines_by_company_account[key].append(line)

        amounts = {
            plan.id: {
                "integrated_ordered_amount": 0.0,
                "integrated_order_total_amount": 0.0,
                "integrated_ordered_quantity": 0.0,
                "integrated_ordered_uom_id": False,
                "integrated_invoiced_amount": 0.0,
                "integrated_actual_amount": 0.0,
            }
            for plan in plans
        }
        date_from = min(budgets.mapped("date_from"))
        date_to = max(budgets.mapped("date_to"))
        start_datetime = fields.Datetime.to_datetime(date_from)
        end_datetime = fields.Datetime.to_datetime(
            date_to + timedelta(days=1))
        company_ids = budgets.mapped("company_id").ids

        def allocate_source(
            company_id, account_id, product_id, distribution, source_date,
            amount, metric, expected_plan_type=None, order_total_amount=None,
            order_quantity=None, order_uom=None,
        ):
            candidates = lines_by_company_account.get(
                (company_id, account_id), ())
            if not candidates or not source_date:
                stats["unmatched_source_count"] += 1
                return

            grouped_by_budget = defaultdict(list)
            for line in candidates:
                if (expected_plan_type
                        and line.plan_type != expected_plan_type):
                    continue
                grouped_by_budget[line.budget_id.id].append(line)

            matched = False
            for analytic_ids, share in _analytic_distribution_shares(
                    distribution):
                for budget_lines in grouped_by_budget.values():
                    matching = [
                        line for line in budget_lines
                        if line.department_analytic_account_id.id
                        in analytic_ids
                        and (not line.project_analytic_account_id
                             or line.project_analytic_account_id.id
                             in analytic_ids)
                        and (not line.product_id
                             or line.product_id.id == product_id)
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
                    if len(matching) > 1:
                        stats["ambiguous_match_count"] += 1
                        continue
                    if not matching:
                        continue

                    line = matching[0]
                    month_plan = self._get_month_plan_for_date(
                        line, source_date)
                    if not month_plan:
                        continue
                    if metric in (
                            "integrated_actual_amount",
                            "integrated_invoiced_amount"):
                        signed_amount = (
                            -amount if line.plan_type == "revenue"
                            else amount)
                    else:
                        signed_amount = amount
                    amounts[month_plan.id][metric] += signed_amount * share
                    if order_total_amount is not None:
                        amounts[month_plan.id][
                            "integrated_order_total_amount"
                        ] += order_total_amount * share
                    if order_quantity is not None:
                        target_uom = (
                            line.uom_id
                            or (line.product_id and line.product_id.uom_id))
                        if (not target_uom
                                and amounts[month_plan.id][
                                    "integrated_ordered_uom_id"]):
                            target_uom = self.env["uom.uom"].browse(
                                amounts[month_plan.id][
                                    "integrated_ordered_uom_id"])
                        if not target_uom:
                            target_uom = order_uom
                        if (not order_uom or not target_uom
                                or _uom_root_id(order_uom) !=
                                _uom_root_id(target_uom)):
                            unmapped_quantity_line_ids.add(line.id)
                        else:
                            amounts[month_plan.id][
                                "integrated_ordered_uom_id"
                            ] = target_uom.id
                            quantity = order_uom._compute_quantity(
                                order_quantity, target_uom, round=False)
                            amounts[month_plan.id][
                                "integrated_ordered_quantity"
                            ] += quantity * share
                    matched = True
            if not matched:
                stats["unmatched_source_count"] += 1

        AccountMoveLine = self.env["account.move.line"].sudo()
        account_ids = lines.mapped("account_id").ids
        if account_ids:
            move_lines = AccountMoveLine.search([
                ("company_id", "in", company_ids),
                ("account_id", "in", account_ids),
                ("parent_state", "=", "posted"),
                ("date", ">=", date_from),
                ("date", "<=", date_to),
                ("display_type", "in", (
                    "product", "cogs", "discount", "rounding",
                    "non_deductible_product",
                    "non_deductible_product_total",
                )),
            ])
            for move_line in move_lines:
                distribution = move_line.analytic_distribution
                allocate_source(
                    move_line.company_id.id,
                    move_line.account_id.id,
                    move_line.product_id.id,
                    distribution,
                    move_line.date,
                    move_line.balance,
                    "integrated_actual_amount",
                )
                move_type = move_line.move_type
                if move_type in (
                        "out_invoice", "out_refund", "out_receipt"):
                    plan_type = "revenue"
                elif move_type in (
                        "in_invoice", "in_refund", "in_receipt"):
                    plan_type = "cost"
                else:
                    continue
                allocate_source(
                    move_line.company_id.id,
                    move_line.account_id.id,
                    move_line.product_id.id,
                    distribution,
                    move_line.date,
                    move_line.balance,
                    "integrated_invoiced_amount",
                    expected_plan_type=plan_type,
                )

        def order_account(order_line, account_type, invoice_types):
            invoice_lines = order_line.invoice_lines.filtered(
                lambda line: line.move_id.state == "posted"
                and line.move_id.move_type in invoice_types
                and line.display_type in (
                    "product", "cogs", "discount", "rounding",
                    "non_deductible_product",
                    "non_deductible_product_total",
                )
                and line.account_id
            )
            invoice_accounts = invoice_lines.mapped("account_id")
            if len(invoice_accounts) > 1:
                stats["ambiguous_match_count"] += 1
                return False
            if invoice_accounts:
                return invoice_accounts

            product = order_line.product_id.with_company(
                order_line.company_id)
            account = product._get_product_accounts().get(account_type)
            fiscal_position = order_line.order_id.fiscal_position_id
            if account and fiscal_position:
                account = fiscal_position.map_account(account)
            return account

        SaleLine = self.env["sale.order.line"].sudo()
        sale_lines = SaleLine.search([
            ("company_id", "in", company_ids),
            ("state", "=", "sale"),
            ("display_type", "=", False),
            ("product_id", "!=", False),
            "|",
            "&", ("order_id.commitment_date", ">=", start_datetime),
            ("order_id.commitment_date", "<", end_datetime),
            "&", ("order_id.commitment_date", "=", False),
            "&", ("order_id.date_order", ">=", start_datetime),
            ("order_id.date_order", "<", end_datetime),
        ])
        for sale_line in sale_lines:
            account = order_account(
                sale_line, "income", ("out_invoice", "out_refund"))
            if not account:
                continue
            order = sale_line.order_id
            source_date = fields.Date.to_date(
                order.commitment_date or order.date_order)
            if not source_date or not date_from <= source_date <= date_to:
                continue
            remaining_amount = (
                sale_line.price_subtotal
                - sale_line.untaxed_amount_invoiced)
            amount = sale_line.currency_id._convert(
                remaining_amount, sale_line.company_id.currency_id,
                sale_line.company_id, source_date)
            order_total_amount = sale_line.currency_id._convert(
                sale_line.price_subtotal,
                sale_line.company_id.currency_id,
                sale_line.company_id, source_date)
            allocate_source(
                sale_line.company_id.id, account.id,
                sale_line.product_id.id, sale_line.analytic_distribution,
                source_date, amount, "integrated_ordered_amount",
                expected_plan_type="revenue",
                order_total_amount=order_total_amount,
                order_quantity=sale_line.product_uom_qty,
                order_uom=sale_line.product_uom_id,
            )

        PurchaseLine = self.env["purchase.order.line"].sudo()
        purchase_lines = PurchaseLine.search([
            ("company_id", "in", company_ids),
            ("state", "in", ("purchase", "done")),
            ("display_type", "=", False),
            ("product_id", "!=", False),
            ("date_planned", ">=", start_datetime),
            ("date_planned", "<", end_datetime),
        ])
        for purchase_line in purchase_lines:
            account = order_account(
                purchase_line, "expense", ("in_invoice", "in_refund"))
            if not account:
                continue
            source_date = fields.Date.to_date(purchase_line.date_planned)
            if not source_date or not date_from <= source_date <= date_to:
                continue
            billed_amount = 0.0
            for invoice_line in purchase_line.invoice_lines:
                move = invoice_line.move_id
                if move.state != "posted":
                    continue
                invoice_date = move.invoice_date or move.date
                converted_amount = invoice_line.currency_id._convert(
                    invoice_line.price_subtotal,
                    purchase_line.currency_id,
                    purchase_line.company_id,
                    invoice_date)
                if move.move_type == "in_invoice":
                    billed_amount += converted_amount
                elif move.move_type == "in_refund":
                    billed_amount -= converted_amount
            amount = purchase_line.currency_id._convert(
                purchase_line.price_subtotal - billed_amount,
                purchase_line.company_id.currency_id,
                purchase_line.company_id, source_date)
            order_total_amount = purchase_line.currency_id._convert(
                purchase_line.price_subtotal,
                purchase_line.company_id.currency_id,
                purchase_line.company_id, source_date)
            allocate_source(
                purchase_line.company_id.id, account.id,
                purchase_line.product_id.id,
                purchase_line.analytic_distribution, source_date, amount,
                "integrated_ordered_amount", expected_plan_type="cost",
                order_total_amount=order_total_amount,
                order_quantity=purchase_line.product_qty,
                order_uom=purchase_line.product_uom_id,
            )

        stats["unmapped_quantity_line_count"] = len(
            unmapped_quantity_line_ids)
        monetary_fields = {
            "integrated_ordered_amount",
            "integrated_order_total_amount",
            "integrated_invoiced_amount",
            "integrated_actual_amount",
        }
        for plan in plans:
            currency = plan.currency_id
            values = {
                field_name: (
                    currency.round(amount)
                    if field_name in monetary_fields else amount)
                for field_name, amount in amounts[plan.id].items()
            }
            plan.write(values)

        if (stats["unmapped_line_count"]
                or stats["unmapped_quantity_line_count"]
                or stats["unmatched_source_count"]
                or stats["ambiguous_match_count"]):
            _logger.warning(
                "Budget integration refreshed %d monthly plans; "
                "%d budget lines lack a GL account or department analytic "
                "account, %d matching lines lack a compatible unit for order "
                "quantities, %d transaction rows were unmatched, and %d "
                "matches were ambiguous.",
                stats["plan_count"], stats["unmapped_line_count"],
                stats["unmapped_quantity_line_count"],
                stats["unmatched_source_count"],
                stats["ambiguous_match_count"])
        return stats

    @api.model
    def _refresh_integrated_for_companies(self, companies):
        if companies:
            return self._refresh_integrated_monitoring(companies=companies)
        return {}

    @api.model
    def _cron_refresh_integrated_monitoring(self):
        stats = self._refresh_integrated_monitoring()
        _logger.info(
            "Budget transaction integration refreshed %d monthly plans.",
            stats["plan_count"])

    @api.model
    def _get_month_plan_for_date(self, budget_line, source_date):
        if not source_date:
            return self.browse()
        months = budget_line.month_plan_ids.sorted("month_number")
        for index, month_plan in enumerate(months):
            if index + 1 < len(months):
                next_start = months[index + 1].period_date
            else:
                next_start = budget_line.budget_id.date_to + timedelta(days=1)
            if month_plan.period_date <= source_date < next_start:
                return month_plan
        return self.browse()


class DepartmentBudgetIntegration(models.Model):
    _inherit = "acme.department.budget"

    def action_refresh_integrated_monitoring(self):
        if (not self.env.su and not self.env.user.has_group(
                "acme_integrated_budget.group_corporate_budget")):
            raise AccessError(_(
                "Only Corporate Finance can refresh integrated budget figures."))
        stats = self.env["acme.budget.month.plan"].sudo()._refresh_integrated_monitoring(
            budgets=self)
        message = _(
            "%(plans)s monthly plans refreshed. Posted accounting entries, "
            "invoices, and confirmed Sales/Purchase orders were matched by GL "
            "account and analytic distribution. Gross order value and quantity "
            "are shown separately from the remaining open commitment.",
            plans=stats["plan_count"])
        if stats["unmapped_line_count"]:
            message += _(
                " %(count)s budget lines lack a GL account or department "
                "analytic account and were skipped.",
                count=stats["unmapped_line_count"])
        if stats["ambiguous_match_count"]:
            message += _(
                " %(count)s ambiguous transaction matches were skipped; "
                "use more specific project analytic accounts or products.",
                count=stats["ambiguous_match_count"])
        if stats["unmatched_source_count"]:
            message += _(
                " %(count)s transaction rows did not match; check their GL "
                "account and analytic distribution.",
                count=stats["unmatched_source_count"])
        if stats["unmapped_quantity_line_count"]:
            message += _(
                " Order quantities were skipped for %(count)s matched budget "
                "lines because no compatible unit of measure is configured.",
                count=stats["unmapped_quantity_line_count"])
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Budget Integration Refreshed"),
                "message": message,
                "type": "warning" if (
                    stats["unmapped_line_count"]
                    or stats["unmatched_source_count"]
                    or stats["ambiguous_match_count"]) else "success",
                "sticky": bool(
                    stats["unmapped_line_count"]
                    or stats["unmatched_source_count"]
                    or stats["ambiguous_match_count"]),
            },
        }


class AccountMoveBudgetIntegration(models.Model):
    _inherit = "account.move"

    def _refresh_integrated_budget_figures(self):
        self.env["acme.budget.month.plan"].sudo()._refresh_integrated_for_companies(
            self.mapped("company_id"))

    def action_post(self):
        result = super().action_post()
        self._refresh_integrated_budget_figures()
        return result

    def button_draft(self):
        result = super().button_draft()
        self._refresh_integrated_budget_figures()
        return result

    def button_cancel(self):
        result = super().button_cancel()
        self._refresh_integrated_budget_figures()
        return result


class SaleOrderBudgetIntegration(models.Model):
    _inherit = "sale.order"

    def _refresh_integrated_budget_figures(self):
        self.env["acme.budget.month.plan"].sudo()._refresh_integrated_for_companies(
            self.mapped("company_id"))

    def action_confirm(self):
        result = super().action_confirm()
        self._refresh_integrated_budget_figures()
        return result

    def action_cancel(self):
        result = super().action_cancel()
        self._refresh_integrated_budget_figures()
        return result

    def write(self, vals):
        result = super().write(vals)
        if {"commitment_date", "date_order"} & set(vals):
            self.filtered(
                lambda order: order.state == "sale"
            )._refresh_integrated_budget_figures()
        return result


class PurchaseOrderBudgetIntegration(models.Model):
    _inherit = "purchase.order"

    def _refresh_integrated_budget_figures(self):
        self.env["acme.budget.month.plan"].sudo()._refresh_integrated_for_companies(
            self.mapped("company_id"))

    def button_confirm(self):
        result = super().button_confirm()
        self._refresh_integrated_budget_figures()
        return result

    def button_cancel(self):
        result = super().button_cancel()
        self._refresh_integrated_budget_figures()
        return result
