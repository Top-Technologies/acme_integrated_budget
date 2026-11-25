from uuid import uuid4

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestIntegratedBudget(TransactionCase):

    def _create_budget(self, department):
        return self.env["acme.department.budget"].sudo().create({
            "company_id": self.env.company.id,
            "fiscal_year": "2026/27",
            "date_from": "2026-07-01",
            "date_to": "2027-06-30",
            "department_id": department.id,
            "budget_name": "Annual Plan",
        })

    def _approve(self, budget):
        budget.sudo().write({"state": "approved"})

    def test_budget_line_generates_twelve_months_and_calculates_amount(self):
        department = self.env["hr.department"].create({"name": "ALD"})
        budget = self._create_budget(department)
        line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Product A",
            "plan_type": "revenue",
            "category": "Stock Item Sales",
            "planning_basis": "quantity",
            "month_plan_ids": [],
        })
        self.assertEqual(len(line.month_plan_ids), 12)
        ordered_months = self.env["acme.budget.month.plan"].search([
            ("budget_line_id", "=", line.id),
        ])
        self.assertEqual(ordered_months[0].period_name, "January")
        self.assertEqual(ordered_months[-1].period_name, "December")
        self.assertEqual(len(budget.month_plan_ids), 12)

        january = line.month_plan_ids.filtered(
            lambda plan: plan.month_number == 7)
        self.assertEqual(january.period_name, "January")
        january.write({"planned_quantity": 3, "unit_rate": 50})
        self.assertEqual(january.planned_amount, 150)
        self.assertEqual(line.annual_planned, 150)
        self.assertEqual(budget.total_revenue, 150)

    def test_integrated_actuals_preserve_manual_adjustments(self):
        department = self.env["hr.department"].create({"name": "Finance"})
        budget = self._create_budget(department)
        line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Salary expense",
            "plan_type": "cost",
            "category": "Salaries",
        })
        january = line.month_plan_ids.filtered(
            lambda plan: plan.month_number == 7)
        january.write({
            "manual_amount": 2000,
            "actual_amount": 300,
            "integrated_actual_amount": 700,
        })
        self.assertEqual(january.actual_amount, 300)
        self.assertEqual(january.integrated_actual_amount, 700)
        self.assertEqual(january.remaining_amount, 1000)
        self.assertAlmostEqual(january.used_percent, 0.5)

    def test_budget_variance_analysis_computes_detail_and_summaries(self):
        company = self.env.company
        suffix = uuid4().hex[:8].upper()
        department = self.env["hr.department"].create({"name": "Finance"})
        analytic_plan = self.env["account.analytic.plan"].create({
            "name": "Budget Analysis " + suffix,
            "default_applicability": "optional",
        })
        analytic = self.env["account.analytic.account"].create({
            "name": "Finance " + suffix,
            "plan_id": analytic_plan.id,
            "company_id": company.id,
        })
        Account = self.env["account.account"].sudo()
        cost_account = Account.create({
            "name": "Budget Analysis Expense",
            "code": "BAE" + suffix,
            "account_type": "expense",
            "company_ids": [Command.set([company.id])],
        })
        revenue_account = Account.create({
            "name": "Budget Analysis Revenue",
            "code": "BAR" + suffix,
            "account_type": "income",
            "company_ids": [Command.set([company.id])],
        })
        budget = self._create_budget(department)
        cost_line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Administration",
            "plan_type": "cost",
            "category": "Admin Expense",
            "business_type": "support",
            "department_analytic_account_id": analytic.id,
            "account_id": cost_account.id,
        })
        revenue_line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Maintenance Service",
            "plan_type": "revenue",
            "category": "Other Service",
            "business_type": "service",
            "department_analytic_account_id": analytic.id,
            "account_id": revenue_account.id,
        })
        cost_july = cost_line.month_plan_ids.filtered(
            lambda month: month.month_number == 1)
        revenue_july = revenue_line.month_plan_ids.filtered(
            lambda month: month.month_number == 1)
        cost_august = cost_line.month_plan_ids.filtered(
            lambda month: month.month_number == 2)
        revenue_august = revenue_line.month_plan_ids.filtered(
            lambda month: month.month_number == 2)
        cost_july.write({
            "use_quantity_rate": False,
            "manual_amount": 100,
            "actual_amount": 140,
        })
        revenue_july.write({
            "use_quantity_rate": False,
            "manual_amount": 200,
            "actual_amount": 250,
        })
        cost_august.write({
            "use_quantity_rate": False,
            "manual_amount": 300,
        })
        revenue_august.write({
            "use_quantity_rate": False,
            "manual_amount": 400,
        })
        self._approve(budget)
        corporate = self.env["acme.corporate.budget"].sudo().create({
            "company_id": company.id,
            "fiscal_year": "2026/27",
            "date_from": "2026-07-01",
            "date_to": "2027-06-30",
            "department_ids": [Command.set([department.id])],
            "department_budget_ids": [Command.set([budget.id])],
            "state": "approved",
        })
        offset_account = Account.create({
            "name": "Budget Analysis Offset",
            "code": "BAO" + suffix,
            "account_type": "expense",
            "company_ids": [Command.set([company.id])],
        })
        journal = self.env["account.journal"].sudo().create({
            "name": "Budget Analysis " + suffix,
            "code": "BA" + suffix[:3],
            "type": "general",
            "company_id": company.id,
        })
        august_move = self.env["account.move"].sudo().create({
            "date": "2026-08-15",
            "journal_id": journal.id,
            "line_ids": [
                Command.create({
                    "name": "August cost",
                    "account_id": cost_account.id,
                    "debit": 50,
                    "credit": 0,
                    "analytic_distribution": {str(analytic.id): 100},
                }),
                Command.create({
                    "name": "Offset",
                    "account_id": offset_account.id,
                    "debit": 0,
                    "credit": 50,
                }),
            ],
        })
        august_move.action_post()
        analysis = self.env["acme.budget.variance.analysis"].create({
            "corporate_budget_id": corporate.id,
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
            "analysis_date": "2026-07-31",
        })

        self.env.flush_all()
        analysis.action_compute()

        self.assertEqual(analysis.state, "computed")
        self.assertEqual(analysis.total_planned, 300)
        self.assertEqual(analysis.total_actual, 390)
        self.assertEqual(analysis.total_variance, -90)
        self.assertAlmostEqual(analysis.variance_percent, -30)
        self.assertEqual(len(analysis.variance_line_ids), 2)
        self.assertEqual(len(analysis.gl_summary_line_ids), 2)
        self.assertEqual(len(analysis.dimension_summary_line_ids), 2)
        cost_result = analysis.variance_line_ids.filtered(
            lambda line: line.plan_type == "cost")
        revenue_result = analysis.variance_line_ids.filtered(
            lambda line: line.plan_type == "revenue")
        self.assertEqual(cost_result.status, "unfavorable")
        self.assertEqual(revenue_result.status, "favorable")

        august_analysis = self.env["acme.budget.variance.analysis"].create({
            "corporate_budget_id": corporate.id,
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "analysis_date": "2026-08-31",
        })
        august_analysis.action_compute()
        self.assertEqual(august_analysis.total_planned, 700)
        self.assertEqual(august_analysis.total_actual, 50)
        self.assertEqual(august_analysis.total_variance, 650)

    def test_posted_journal_entry_refreshes_actual_by_gl_and_analytic(self):
        company = self.env.company
        suffix = uuid4().hex[:8].upper()
        Account = self.env["account.account"].sudo()
        income_account = Account.create({
            "name": "Budget Integration Income",
            "code": "BI" + suffix,
            "account_type": "income",
            "company_ids": [Command.set([company.id])],
        })
        expense_account = Account.create({
            "name": "Budget Integration Offset",
            "code": "BE" + suffix,
            "account_type": "expense",
            "company_ids": [Command.set([company.id])],
        })
        plan = self.env["account.analytic.plan"].create({
            "name": "Budget Department " + suffix,
            "default_applicability": "optional",
        })
        department_analytic = self.env["account.analytic.account"].create({
            "name": "Finance " + suffix,
            "plan_id": plan.id,
            "company_id": company.id,
        })
        department = self.env["hr.department"].create({
            "name": "Finance " + suffix,
            "company_id": company.id,
        })
        budget = self._create_budget(department)
        line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Service revenue",
            "plan_type": "revenue",
            "category": "Service Sales",
            "account_id": income_account.id,
            "department_analytic_account_id": department_analytic.id,
        })
        january = line.month_plan_ids.filtered(
            lambda month: month.month_number == 7)
        january.write({"manual_amount": 2000})
        journal = self.env["account.journal"].sudo().create({
            "name": "Budget Integration " + suffix,
            "code": "BI" + suffix[:3],
            "type": "general",
            "company_id": company.id,
        })
        move = self.env["account.move"].sudo().create({
            "date": "2027-01-15",
            "journal_id": journal.id,
            "line_ids": [
                Command.create({
                    "name": "Offset",
                    "account_id": expense_account.id,
                    "debit": 1000,
                    "credit": 0,
                }),
                Command.create({
                    "name": "Service revenue",
                    "account_id": income_account.id,
                    "debit": 0,
                    "credit": 1000,
                    "analytic_distribution": {
                        str(department_analytic.id): 100,
                    },
                }),
            ],
        })
        move.action_post()

        self.assertEqual(january.integrated_actual_amount, 1000)
        self.assertEqual(january.integrated_invoiced_amount, 0)
        self.assertEqual(january.remaining_amount, 1000)

    def test_confirmed_sales_and_purchase_orders_refresh_commitments(self):
        company = self.env.company
        suffix = uuid4().hex[:8].upper()
        Account = self.env["account.account"].sudo()
        income_account = Account.create({
            "name": "Budget Sales Income",
            "code": "SI" + suffix,
            "account_type": "income",
            "company_ids": [Command.set([company.id])],
        })
        expense_account = Account.create({
            "name": "Budget Purchase Expense",
            "code": "PE" + suffix,
            "account_type": "expense",
            "company_ids": [Command.set([company.id])],
        })
        bill_expense_account = Account.create({
            "name": "Budget Purchase Bill Expense",
            "code": "BE" + suffix,
            "account_type": "expense",
            "company_ids": [Command.set([company.id])],
        })
        receivable_account = Account.create({
            "name": "Budget Integration Receivable",
            "code": "BR" + suffix,
            "account_type": "asset_receivable",
            "company_ids": [Command.set([company.id])],
            "reconcile": True,
        })
        plan = self.env["account.analytic.plan"].create({
            "name": "Budget Order Department " + suffix,
            "default_applicability": "optional",
        })
        department_analytic = self.env["account.analytic.account"].create({
            "name": "Operations " + suffix,
            "plan_id": plan.id,
            "company_id": company.id,
        })
        department = self.env["hr.department"].create({
            "name": "Operations " + suffix,
            "company_id": company.id,
        })
        budget = self._create_budget(department)
        revenue_line = self.env[
            "acme.department.budget.line"].sudo().create({
                "budget_id": budget.id,
                "description": "Sales order revenue",
                "plan_type": "revenue",
                "category": "Service Sales",
                "account_id": income_account.id,
                "department_analytic_account_id": department_analytic.id,
            })
        cost_line = self.env[
            "acme.department.budget.line"].sudo().create({
                "budget_id": budget.id,
                "description": "Purchase order cost",
                "plan_type": "cost",
                "category": "Project Cost",
                "account_id": expense_account.id,
                "department_analytic_account_id": department_analytic.id,
            })
        product_template = self.env["product.template"].create({
            "name": "Budget Integration Service " + suffix,
            "type": "service",
            "sale_ok": True,
            "purchase_ok": True,
            "purchase_method": "purchase",
            "property_account_income_id": income_account.id,
            "property_account_expense_id": expense_account.id,
            "taxes_id": [Command.clear()],
            "supplier_taxes_id": [Command.clear()],
        })
        product = product_template.product_variant_id
        revenue_line.write({"product_id": product.id})
        cost_line.write({"product_id": product.id})
        second_product_template = self.env["product.template"].create({
            "name": "Budget Integration Service B " + suffix,
            "type": "service",
            "sale_ok": True,
            "purchase_ok": True,
            "purchase_method": "purchase",
            "property_account_income_id": income_account.id,
            "property_account_expense_id": expense_account.id,
            "taxes_id": [Command.clear()],
            "supplier_taxes_id": [Command.clear()],
        })
        second_product = second_product_template.product_variant_id
        second_revenue_line = self.env[
            "acme.department.budget.line"].sudo().create({
                "budget_id": budget.id,
                "description": "Sales order revenue for product B",
                "plan_type": "revenue",
                "category": "Service Sales",
                "account_id": income_account.id,
                "department_analytic_account_id": department_analytic.id,
                "product_id": second_product.id,
            })
        partner = self.env["res.partner"].create({
            "name": "Budget Integration Partner " + suffix,
            "property_account_receivable_id": receivable_account.id,
        })
        sale_journal = self.env["account.journal"].create({
            "name": "Budget Sales Journal " + suffix,
            "code": "SJ" + suffix[:3],
            "type": "sale",
            "company_id": company.id,
            "default_account_id": income_account.id,
        })

        sale_order = self.env["sale.order"].create({
            "partner_id": partner.id,
            "company_id": company.id,
            "journal_id": sale_journal.id,
            "date_order": "2027-01-10 09:00:00",
            "commitment_date": "2027-01-20 09:00:00",
            "order_line": [Command.create({
                "product_id": product.id,
                "product_uom_qty": 2,
                "price_unit": 500,
                "analytic_distribution": {
                    str(department_analytic.id): 100,
                },
            })],
        })
        sale_order.action_confirm()
        second_sale_order = self.env["sale.order"].create({
            "partner_id": partner.id,
            "company_id": company.id,
            "journal_id": sale_journal.id,
            "date_order": "2027-01-11 09:00:00",
            "commitment_date": "2027-01-21 09:00:00",
            "order_line": [Command.create({
                "product_id": second_product.id,
                "product_uom_qty": 4,
                "price_unit": 100,
                "analytic_distribution": {
                    str(department_analytic.id): 100,
                },
            })],
        })
        second_sale_order.action_confirm()
        january_revenue = revenue_line.month_plan_ids.filtered(
            lambda month: month.month_number == 7)
        january_revenue_b = second_revenue_line.month_plan_ids.filtered(
            lambda month: month.month_number == 7)
        self.assertEqual(january_revenue.integrated_ordered_amount, 1000)
        self.assertEqual(january_revenue.integrated_order_total_amount, 1000)
        self.assertEqual(january_revenue.integrated_ordered_quantity, 2)
        self.assertEqual(
            january_revenue.integrated_ordered_uom_id, product.uom_id)
        self.assertEqual(january_revenue_b.integrated_ordered_amount, 400)
        self.assertEqual(january_revenue_b.integrated_order_total_amount, 400)
        self.assertEqual(january_revenue_b.integrated_ordered_quantity, 4)
        self.assertEqual(
            january_revenue_b.integrated_ordered_uom_id,
            second_product.uom_id)
        invoice = sale_order._create_invoices()
        invoice.write({
            "invoice_date": "2027-01-20",
            "date": "2027-01-20",
        })
        invoice.action_post()

        purchase_order = self.env["purchase.order"].create({
            "partner_id": partner.id,
            "company_id": company.id,
            "order_line": [Command.create({
                "product_id": product.id,
                "product_qty": 3,
                "price_unit": 200,
                "date_planned": "2027-01-25 09:00:00",
                "analytic_distribution": {
                    str(department_analytic.id): 100,
                },
            })],
        })
        purchase_order.button_confirm()

        january_cost = cost_line.month_plan_ids.filtered(
            lambda month: month.month_number == 7)
        self.assertEqual(january_revenue.integrated_ordered_amount, 0)
        self.assertEqual(january_revenue.integrated_order_total_amount, 1000)
        self.assertEqual(january_revenue.integrated_ordered_quantity, 2)
        self.assertEqual(
            january_revenue.integrated_ordered_uom_id, product.uom_id)
        self.assertEqual(january_revenue.integrated_invoiced_amount, 1000)
        self.assertEqual(january_revenue.integrated_actual_amount, 1000)
        self.assertEqual(january_revenue_b.integrated_ordered_amount, 400)
        self.assertEqual(january_revenue_b.integrated_invoiced_amount, 0)
        self.assertEqual(january_revenue_b.integrated_actual_amount, 0)
        self.assertEqual(january_cost.integrated_ordered_amount, 600)
        self.assertEqual(january_cost.integrated_order_total_amount, 600)
        self.assertEqual(january_cost.integrated_ordered_quantity, 3)
        self.assertEqual(
            january_cost.integrated_ordered_uom_id, product.uom_id)

        bill_action = purchase_order.action_create_invoice()
        bill = self.env["account.move"].browse(bill_action["res_id"])
        bill_line = bill.invoice_line_ids.filtered(
            lambda line: line.display_type == "product")
        bill_line.write({"account_id": bill_expense_account.id})
        cost_line.write({"account_id": bill_expense_account.id})
        bill.write({
            "invoice_date": "2027-01-25",
            "date": "2027-01-25",
        })
        bill.action_post()
        self.assertEqual(january_cost.integrated_ordered_amount, 0)
        self.assertEqual(january_cost.integrated_order_total_amount, 600)
        self.assertEqual(january_cost.integrated_ordered_quantity, 3)
        self.assertEqual(
            january_cost.integrated_ordered_uom_id, product.uom_id)
        self.assertEqual(january_cost.integrated_invoiced_amount, 600)
        self.env.flush_all()
        purchase_summary = budget.category_summary_ids.filtered(
            lambda summary: summary.category == "Project Cost")
        self.assertEqual(len(purchase_summary), 1)
        self.assertEqual(
            purchase_summary.gross_order_value, 600,
            repr(purchase_summary.read(["gross_order_value"])))

    def test_quantity_rate_overrides_manual_amount_and_manual_fallback_works(self):
        department = self.env["hr.department"].create({"name": "ALD"})
        budget = self._create_budget(department)
        line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Product A",
            "plan_type": "revenue",
            "category": "Stock Item Sales",
            "planning_basis": "quantity",
        })
        january = line.month_plan_ids.filtered(
            lambda plan: plan.month_number == 7)
        january.write({"manual_amount": 125})
        self.assertEqual(january.planned_amount, 125)
        january.write({"planned_quantity": 0, "unit_rate": 50})
        self.assertEqual(january.planned_amount, 0)
        january.write({
            "planned_quantity": 3,
            "unit_rate": 50,
        })
        self.assertEqual(january.planned_amount, 150)

    def test_category_summary_aggregates_monthly_plan_and_actuals(self):
        department = self.env["hr.department"].create({"name": "ALD"})
        budget = self._create_budget(department)
        lines = self.env["acme.department.budget.line"].sudo().create([
            {
                "budget_id": budget.id,
                "description": "Product A",
                "plan_type": "revenue",
                "category": "Stock Item Sales",
                "planning_basis": "manual",
            },
            {
                "budget_id": budget.id,
                "description": "Product B",
                "plan_type": "revenue",
                "category": "Stock Item Sales",
                "planning_basis": "manual",
            },
        ])
        for line, amount in zip(lines, (100, 50)):
            january = line.month_plan_ids.filtered(
                lambda plan: plan.month_number == 7)
            january.write({"manual_amount": amount})
        self._approve(budget)
        january = lines[0].month_plan_ids.filtered(
            lambda plan: plan.month_number == 7)
        january.sudo().write({"actual_amount": 80})
        self.env.flush_all()

        summary = self.env["acme.budget.category.summary"].sudo().search([
            ("budget_id", "=", budget.id),
            ("plan_type", "=", "revenue"),
            ("category", "=", "Stock Item Sales"),
        ])
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary.annual_planned, 150)
        self.assertEqual(summary.revised_budget, 150)
        self.assertEqual(summary.actual_amount, 80)
        self.assertEqual(summary.remaining_amount, 70)

    def test_consolidation_uses_approved_budgets_and_tracks_pending_departments(self):
        departments = self.env["hr.department"].create([
            {"name": "ALD"},
            {"name": "Finance"},
        ])
        first_budget = self._create_budget(departments[0])
        self.env["acme.department.budget.line"].sudo().create({
            "budget_id": first_budget.id,
            "description": "Project installation",
            "plan_type": "revenue",
            "category": "Project Sales",
            "planning_basis": "manual",
            "month_plan_ids": [
                (0, 0, {"month_number": 1, "manual_amount": 1000}),
            ] + [
                (0, 0, {"month_number": month})
                for month in range(2, 13)
            ],
        })
        self._approve(first_budget)

        corporate = self.env["acme.corporate.budget"].sudo().create({
            "company_id": self.env.company.id,
            "fiscal_year": "2026/27",
            "date_from": "2026-07-01",
            "date_to": "2027-06-30",
            "department_ids": [(6, 0, departments.ids)],
        })
        corporate.action_load_approved_budgets()
        self.assertEqual(corporate.department_budget_ids, first_budget)
        self.assertEqual(corporate.pending_department_ids, departments[1])
        self.assertEqual(corporate.total_revenue, 1000)

        corporate.action_mark_reviewed()
        with self.assertRaises(UserError):
            corporate.action_approve()

        second_budget = self._create_budget(departments[1])
        self.env["acme.department.budget.line"].sudo().create({
            "budget_id": second_budget.id,
            "description": "Stationery",
            "plan_type": "cost",
            "category": "Supporting Expense",
            "planning_basis": "manual",
        })
        self._approve(second_budget)
        corporate.action_load_approved_budgets()
        self.assertFalse(corporate.pending_department_ids)
        corporate.action_approve()
        self.assertEqual(corporate.state, "approved")

    def test_consolidation_loads_matching_budgets_without_scope(self):
        departments = self.env["hr.department"].create([
            {"name": "ALD"},
            {"name": "Finance"},
        ])
        source_budgets = self.env["acme.department.budget"].sudo().create([
            {
                "company_id": self.env.company.id,
                "fiscal_year": "2027/28",
                "date_from": "2027-07-01",
                "date_to": "2028-06-30",
                "department_id": department.id,
                "budget_name": "Annual Plan",
            }
            for department in departments
        ])
        for budget in source_budgets:
            self.env["acme.department.budget.line"].sudo().create({
                "budget_id": budget.id,
                "description": "Department plan",
                "plan_type": "cost",
                "category": "Supporting Expense",
                "planning_basis": "manual",
            })
        approved_budget = source_budgets[0]
        self._approve(approved_budget)

        corporate = self.env["acme.corporate.budget"].sudo().create({
            "company_id": self.env.company.id,
            "fiscal_year": "2027/28",
            "date_from": "2027-07-01",
            "date_to": "2028-06-30",
        })
        corporate.action_load_approved_budgets()
        self.assertEqual(
            set(corporate.department_ids.ids), set(departments.ids))
        self.assertEqual(corporate.department_budget_ids, approved_budget)
        self.assertEqual(corporate.pending_department_ids, departments[1])

        manual_add = self.env["acme.corporate.budget"].new({
            "company_id": self.env.company.id,
            "fiscal_year": "2027/28",
            "date_from": "2027-07-01",
            "date_to": "2028-06-30",
            "department_budget_ids": [Command.set(approved_budget.ids)],
        })
        manual_add._onchange_department_budget_ids()
        self.assertEqual(
            manual_add.department_ids.mapped("name"),
            [approved_budget.department_id.name])

    def test_non_annual_budget_period_is_rejected(self):
        department = self.env["hr.department"].create({"name": "Operations"})
        with self.assertRaises(ValidationError):
            self.env["acme.department.budget"].sudo().create({
                "company_id": self.env.company.id,
                "fiscal_year": "2026/27",
                "date_from": "2026-07-01",
                "date_to": "2027-07-01",
                "department_id": department.id,
                "budget_name": "Annual Plan",
            })

    def test_fiscal_year_label_must_match_budget_dates(self):
        department = self.env["hr.department"].create({"name": "Operations"})
        with self.assertRaises(ValidationError):
            self.env["acme.department.budget"].sudo().create({
                "company_id": self.env.company.id,
                "fiscal_year": "2025/26",
                "date_from": "2026-07-01",
                "date_to": "2027-06-30",
                "department_id": department.id,
                "budget_name": "Annual Plan",
            })

    def test_eighth_to_seventh_fiscal_months_generate_calendar_months(self):
        department = self.env["hr.department"].create({"name": "HR"})
        budget = self.env["acme.department.budget"].sudo().create({
            "company_id": self.env.company.id,
            "fiscal_year": "2026/27",
            "date_from": "2026-07-08",
            "date_to": "2027-07-07",
            "department_id": department.id,
            "budget_name": "HR Annual Plan",
        })
        line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": budget.id,
            "description": "Salary Expense",
            "plan_type": "cost",
            "category": "Salary Expense",
            "planning_basis": "manual",
        })

        january = line.month_plan_ids.filtered(
            lambda plan: plan.month_number == 7)
        february = line.month_plan_ids.filtered(
            lambda plan: plan.month_number == 8)
        self.assertEqual(len(budget.month_plan_ids), 12)
        self.assertEqual(january.period_name, "January")
        self.assertEqual(january.period_date.isoformat(), "2027-01-08")
        self.assertEqual(february.period_name, "February")
        self.assertEqual(february.period_date.isoformat(), "2027-02-08")

    def test_department_user_only_accesses_own_budget(self):
        departments = self.env["hr.department"].create([
            {"name": "ALD"},
            {"name": "Finance"},
        ])
        user = self.env["res.users"].create({
            "name": "ALD Budget User",
            "login": "ald-budget-user",
            "group_ids": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "acme_integrated_budget.group_department_budget_user").id,
            ])],
        })
        self.env["hr.employee"].create({
            "name": user.name,
            "user_id": user.id,
            "department_id": departments[0].id,
        })
        self.assertIn(user, departments[0].budget_user_ids)
        self.assertTrue(user.has_group(
            "acme_integrated_budget.group_department_budget_user"))
        own_budget = self.env["acme.department.budget"].with_user(user).create({
            "company_id": self.env.company.id,
            "fiscal_year": "2026/27",
            "date_from": "2026-07-01",
            "date_to": "2027-06-30",
            "department_id": departments[0].id,
            "budget_name": "ALD Plan",
        })
        other_budget = self._create_budget(departments[1])
        own_line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": own_budget.id,
            "description": "ALD Product",
            "category": "Stock Item Sales",
        })
        other_line = self.env["acme.department.budget.line"].sudo().create({
            "budget_id": other_budget.id,
            "description": "Finance Stationery",
            "category": "Supporting Expense",
        })

        visible_budgets = self.env["acme.department.budget"].with_user(
            user).search([])
        self.assertIn(own_budget, visible_budgets)
        self.assertNotIn(other_budget, visible_budgets)
        with self.assertRaises(AccessError):
            other_budget.with_user(user).read(["budget_name"])

        own_budget.sudo().write({"state": "approved"})
        self.env.flush_all()
        visible_lines = self.env["acme.department.budget.line"].with_user(
            user).search([])
        self.assertIn(own_line, visible_lines)
        self.assertNotIn(other_line, visible_lines)
        visible_months = self.env["acme.budget.month.plan"].with_user(
            user).search([])
        self.assertEqual(len(visible_months), 12)
        report_model = self.env["acme.budget.department.report"]
        all_reports = report_model.sudo().search([])
        self.assertTrue(all_reports, "Approved budget lines should appear in the report.")
        visible_reports = report_model.with_user(user).search([])
        self.assertTrue(visible_reports)
        self.assertEqual(
            set(visible_reports.mapped("department_id").ids),
            {departments[0].id})
        with self.assertRaises(UserError):
            own_budget.with_user(user).write({"budget_name": "Changed"})

    def test_system_administrators_get_corporate_budget_access(self):
        admin = self.env.ref("base.user_admin")
        self.assertTrue(admin.has_group(
            "acme_integrated_budget.group_corporate_budget"))
