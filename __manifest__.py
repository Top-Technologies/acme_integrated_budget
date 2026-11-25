{
    "name": "ACME Integrated Budget Management",
    "author": "ACME",
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "summary": "Departmental planning, approval, and corporate budget consolidation",
    "description": """
ACME Integrated Budget Management
=================================
Department-owned annual budgets with monthly revenue and cost planning,
departmental and corporate approval workflows, immutable approved plans,
linked revisions, corporate consolidation, and budget monitoring reports.
    """,
    "depends": [
        "base", "account", "hr", "mail", "sale_management", "purchase",
    ],
    "data": [
        "security/integrated_budget_security.xml",
        "security/ir.model.access.csv",
        "views/integrated_budget_views.xml",
        "data/integrated_budget_cron.xml",
    ],
    "license": "LGPL-3",
    "installable": True,
    "application": True,
    "auto_install": False,
}
