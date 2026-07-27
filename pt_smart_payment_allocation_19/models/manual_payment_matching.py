import logging

from odoo import _, fields, models, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SmartPaymentMatchCustomer(models.Model):
    _name = "smart.payment.match.customer"
    _description = "Customer Payment Matching Summary"
    _auto = False
    _rec_name = "partner_id"
    _order = "matchable_amount desc, partner_id"

    partner_id = fields.Many2one("res.partner", string="Customer", readonly=True)
    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)
    open_payment_count = fields.Integer(string="Unmatched Payments/Credits/Returns", readonly=True)
    open_payment_amount = fields.Monetary(
        string="Unmatched Payment/Credit/Return Amount",
        currency_field="currency_id",
        readonly=True,
    )
    open_invoice_count = fields.Integer(string="Open Invoices", readonly=True)
    open_invoice_amount = fields.Monetary(
        string="Open Invoice Debt", currency_field="currency_id", readonly=True
    )
    matchable_amount = fields.Monetary(
        string="Amount Available to Match",
        currency_field="currency_id",
        readonly=True,
        help=(
            "Amount that can really be reconciled because the payment/credit and "
            "invoice are on the same receivable account."
        ),
    )

    def init(self):
        """Create one row per customer, but calculate only same-account matches.

        Odoo can reconcile journal items only when they belong to the same account.
        Earlier versions joined payments and invoices only by customer/company, so
        the screen could show a matchable amount while the Match button had no
        eligible invoice on the payment's receivable account.
        """
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE OR REPLACE VIEW smart_payment_match_customer AS (
                WITH payment_by_account AS (
                    SELECT
                        aml.company_id,
                        COALESCE(partner.commercial_partner_id, partner.id) AS partner_id,
                        aml.account_id,
                        COUNT(DISTINCT aml.move_id) AS payment_count,
                        SUM(-aml.amount_residual) AS payment_amount
                    FROM account_move_line aml
                    JOIN account_account account ON account.id = aml.account_id
                    JOIN account_move move ON move.id = aml.move_id
                    JOIN res_partner partner ON partner.id = aml.partner_id
                    WHERE aml.parent_state = 'posted'
                      AND account.account_type = 'asset_receivable'
                      AND account.reconcile = TRUE
                      AND move.move_type IN ('entry', 'out_refund')
                      AND aml.reconciled = FALSE
                      AND aml.amount_residual < 0
                    GROUP BY
                        aml.company_id,
                        COALESCE(partner.commercial_partner_id, partner.id),
                        aml.account_id
                ),
                invoice_by_account AS (
                    SELECT
                        aml.company_id,
                        COALESCE(partner.commercial_partner_id, partner.id) AS partner_id,
                        aml.account_id,
                        COUNT(DISTINCT move.id) AS invoice_count,
                        SUM(aml.amount_residual) AS invoice_amount
                    FROM account_move_line aml
                    JOIN account_account account ON account.id = aml.account_id
                    JOIN account_move move ON move.id = aml.move_id
                    JOIN res_partner partner ON partner.id = aml.partner_id
                    WHERE aml.parent_state = 'posted'
                      AND account.account_type = 'asset_receivable'
                      AND account.reconcile = TRUE
                      AND aml.reconciled = FALSE
                      AND aml.amount_residual > 0
                      AND move.move_type = 'out_invoice'
                    GROUP BY
                        aml.company_id,
                        COALESCE(partner.commercial_partner_id, partner.id),
                        aml.account_id
                ),
                matched_accounts AS (
                    SELECT
                        payments.company_id,
                        payments.partner_id,
                        payments.account_id,
                        payments.payment_count,
                        payments.payment_amount,
                        invoices.invoice_count,
                        invoices.invoice_amount,
                        LEAST(payments.payment_amount, invoices.invoice_amount) AS matchable_amount
                    FROM payment_by_account payments
                    JOIN invoice_by_account invoices
                      ON invoices.company_id = payments.company_id
                     AND invoices.partner_id = payments.partner_id
                     AND invoices.account_id = payments.account_id
                    WHERE LEAST(payments.payment_amount, invoices.invoice_amount) > 0
                ),
                matching_customers AS (
                    SELECT
                        company_id,
                        partner_id,
                        SUM(payment_count)::integer AS open_payment_count,
                        SUM(payment_amount) AS open_payment_amount,
                        SUM(invoice_count)::integer AS open_invoice_count,
                        SUM(invoice_amount) AS open_invoice_amount,
                        SUM(matchable_amount) AS matchable_amount
                    FROM matched_accounts
                    GROUP BY company_id, partner_id
                )
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY matching_customers.company_id, matching_customers.partner_id
                    )::integer AS id,
                    matching_customers.company_id,
                    matching_customers.partner_id,
                    company.currency_id,
                    matching_customers.open_payment_count,
                    matching_customers.open_payment_amount,
                    matching_customers.open_invoice_count,
                    matching_customers.open_invoice_amount,
                    matching_customers.matchable_amount
                FROM matching_customers
                JOIN res_company company ON company.id = matching_customers.company_id
            )
            """
        )

    def _get_customer_open_invoice_lines(self):
        self.ensure_one()
        return self.env["account.move.line"].sudo().with_company(self.company_id).search(
            [
                ("company_id", "=", self.company_id.id),
                ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
                ("parent_state", "=", "posted"),
                ("account_id.account_type", "=", "asset_receivable"),
                ("account_id.reconcile", "=", True),
                ("move_id.move_type", "=", "out_invoice"),
                ("reconciled", "=", False),
                ("amount_residual", ">", 0.0),
            ],
            order="date_maturity asc, date asc, id asc",
        )

    def _get_customer_unmatched_credit_lines(self):
        """Return only credits having an open invoice on the same account."""
        self.ensure_one()
        CreditLine = self.env["account.move.line"].sudo().with_company(self.company_id)
        invoice_lines = self._get_customer_open_invoice_lines()
        account_ids = invoice_lines.account_id.ids
        if not account_ids:
            return CreditLine.browse()

        domain = CreditLine._smart_unmatched_customer_credit_domain(
            self.company_id,
            partner=self.partner_id,
        )
        domain.extend(
            [
                ("account_id", "in", account_ids),
                ("account_id.reconcile", "=", True),
            ]
        )
        return CreditLine.search(domain, order="date asc, id asc")

    def _match_customer_oldest_first(self):
        self.ensure_one()
        totals = {
            "payments_processed": 0,
            "payments_matched": 0,
            "matched_amount": 0.0,
            "errors": 0,
            "error_messages": [],
        }

        credit_lines = self._get_customer_unmatched_credit_lines()
        if not credit_lines:
            raise UserError(
                _(
                    "No eligible payment/credit was found for %(customer)s on the same "
                    "reconcilable receivable account as an open invoice.",
                    customer=self.partner_id.display_name,
                )
            )

        for credit_line in credit_lines:
            totals["payments_processed"] += 1
            try:
                with self.env.cr.savepoint():
                    result = credit_line._smart_match_customer_credit_oldest_first()
                    if result["matched"]:
                        totals["payments_matched"] += 1
                        totals["matched_amount"] += result["matched_amount"]
                    elif result.get("reason"):
                        totals["error_messages"].append(
                            "%s: %s" % (credit_line.move_id.display_name, result["reason"])
                        )
            except Exception as exc:
                totals["errors"] += 1
                totals["error_messages"].append(
                    "%s: %s" % (credit_line.move_id.display_name, str(exc))
                )
                _logger.exception(
                    "Manual customer matching failed for customer %s and journal item %s",
                    self.partner_id.id,
                    credit_line.id,
                )

        return totals

    def action_match_selected_customers(self):
        if not self:
            raise UserError(_("Select at least one customer."))

        totals = {
            "customers": len(self),
            "payments_processed": 0,
            "payments_matched": 0,
            "matched_amount": 0.0,
            "errors": 0,
            "error_messages": [],
        }

        for customer_summary in self:
            result = customer_summary._match_customer_oldest_first()
            for key in ("payments_processed", "payments_matched", "matched_amount", "errors"):
                totals[key] += result[key]
            totals["error_messages"].extend(result.get("error_messages", []))

        if not totals["payments_matched"]:
            details = "\n".join(totals["error_messages"][:8]) or _(
                "No eligible debit and credit lines were found on the same receivable account."
            )
            raise UserError(
                _(
                    "No reconciliation was created.\n\n%(details)s",
                    details=details,
                )
            )

        currency = self[:1].currency_id if len(self.mapped("currency_id")) == 1 else False
        if currency:
            decimals = currency.decimal_places
            amount_text = f"{currency.round(totals['matched_amount']):,.{decimals}f} {currency.name}"
        else:
            amount_text = f"{totals['matched_amount']:,.2f}"

        message = _(
            "Matching completed for %(customers)s customer(s). "
            "%(matched)s of %(processed)s payment/credit/return item(s) were matched for %(amount)s.",
            customers=totals["customers"],
            matched=totals["payments_matched"],
            processed=totals["payments_processed"],
            amount=amount_text,
        )
        if totals["errors"] or totals["error_messages"]:
            message += _(" Some items were skipped; open the remaining lines to review them.")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Manual Payment Matching"),
                "message": message,
                "type": "warning" if totals["errors"] else "success",
                "sticky": bool(totals["errors"]),
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_match_customer(self):
        self.ensure_one()
        return self.action_match_selected_customers()

    def action_open_customer_payments(self):
        self.ensure_one()
        credit_lines = self._get_customer_unmatched_credit_lines()
        return {
            "type": "ir.actions.act_window",
            "name": _(
                "Matchable Payments/Credits/Returns - %(customer)s",
                customer=self.partner_id.display_name,
            ),
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [("id", "in", credit_lines.ids)],
            "context": {"create": False},
        }

    def action_open_customer_invoices(self):
        self.ensure_one()
        invoice_lines = self._get_customer_open_invoice_lines()
        return {
            "type": "ir.actions.act_window",
            "name": _("Open Invoices - %(customer)s", customer=self.partner_id.display_name),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("id", "in", invoice_lines.move_id.ids)],
            "context": {"create": False, "default_move_type": "out_invoice"},
        }
