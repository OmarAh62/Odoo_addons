{
    "name": "Smart Automated Reconcile Payment",
    "summary": "Smart automatic reconciliation for old and manually entered invoices, with live customer balance shown during payment",
    "version": "18.0.1.10.2",
    "category": "Accounting/Accounting",
    "author": "Omar Ahmed",
    "website": "",
    "license": "LGPL-3",
    "price": 79.00,
    "currency": "USD",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
        "views/account_payment_views.xml",
        "views/manual_payment_matching_views.xml",
        "wizard/payment_allocation_wizard_views.xml"
    ],
    "assets": {},
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "auto_install": False
}
