import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = "account.payment"

    smart_allocation_state = fields.Selection(
        selection=[
            ("not_suggested", "Not Suggested"),
            ("suggested", "Suggested"),
            ("review", "Needs Review"),
            ("allocated", "Allocated"),
        ],
        string="Smart Allocation Status",
        default="not_suggested",
        copy=False,
        readonly=True,
        tracking=True,
    )
    smart_allocation_confidence = fields.Float(
        string="Allocation Confidence %",
        copy=False,
        readonly=True,
        tracking=True,
    )
    smart_allocation_note = fields.Char(
        string="Allocation Result",
        copy=False,
        readonly=True,
        tracking=True,
    )
    smart_existing_backfill = fields.Boolean(
        string="Existing Payment Processing",
        copy=False,
        readonly=True,
        help="This payment was detected by the existing unallocated payment processor.",
    )
    customer_debt_before_payment = fields.Monetary(
        string="Debt Before This Payment",
        currency_field="company_currency_id",
        copy=False,
        readonly=True,
        tracking=True,
        help="Customer open invoice debt captured immediately before smart allocation is performed.",
    )
    customer_outstanding_amount = fields.Monetary(
        string="Current Outstanding Debt",
        currency_field="company_currency_id",
        compute="_compute_customer_debt_overview",
        readonly=True,
        help="Current residual amount of the customer's posted open invoices in company currency.",
    )
    customer_overdue_amount = fields.Monetary(
        string="Overdue Debt",
        currency_field="company_currency_id",
        compute="_compute_customer_debt_overview",
        readonly=True,
        help="Current residual amount of posted customer invoices whose due date has passed.",
    )
    customer_open_invoice_count = fields.Integer(
        string="Outstanding Invoices",
        compute="_compute_customer_debt_overview",
        readonly=True,
    )

    def init(self):
        """Repair queue flags left by earlier module versions during upgrade.

        Old versions could mark a payment as processed even when reconciliation did
        not happen. Reset only payments that still have an open receivable balance.
        """
        super().init()
        self.env.cr.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name = 'account_payment'
               AND column_name = 'smart_existing_backfill'
            """
        )
        if self.env.cr.fetchone():
            self.env.cr.execute(
                """
                UPDATE account_payment payment
                   SET smart_existing_backfill = FALSE
                 WHERE payment.smart_existing_backfill = TRUE
                   AND payment.is_reconciled = FALSE
                """
            )

    def _get_customer_open_invoice_domain(self):
        self.ensure_one()
        if not self.partner_id or self.partner_type != "customer":
            return [("id", "=", 0)]

        return [
            ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
            ("company_id", "=", self.company_id.id),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("amount_residual", ">", 0.0),
        ]

    @api.depends("partner_id", "partner_type", "company_id")
    def _compute_customer_debt_overview(self):
        today = fields.Date.context_today(self)
        for payment in self:
            payment.customer_outstanding_amount = 0.0
            payment.customer_overdue_amount = 0.0
            payment.customer_open_invoice_count = 0

            if not payment.partner_id or payment.partner_type != "customer" or not payment.company_id:
                continue

            invoices = self.env["account.move"].search(payment._get_customer_open_invoice_domain())
            payment.customer_open_invoice_count = len(invoices)
            payment.customer_outstanding_amount = sum(
                abs(invoice.amount_residual_signed) for invoice in invoices
            )
            payment.customer_overdue_amount = sum(
                abs(invoice.amount_residual_signed)
                for invoice in invoices
                if invoice.invoice_date_due and invoice.invoice_date_due < today
            )

    def action_open_customer_outstanding_invoices(self):
        self.ensure_one()
        if not self.partner_id or self.partner_type != "customer":
            return False

        return {
            "type": "ir.actions.act_window",
            "name": _("Outstanding Invoices - %(customer)s", customer=self.partner_id.display_name),
            "res_model": "account.move",
            "view_mode": "tree,form",
            "domain": self._get_customer_open_invoice_domain(),
            "context": {
                "default_move_type": "out_invoice",
                "create": False,
            },
        }

    def _is_smart_allocation_candidate(self):
        self.ensure_one()
        return bool(
            self.payment_type == "inbound"
            and self.partner_type == "customer"
            and self.partner_id
            and self.move_id
            and self.move_id.state == "posted"
            and self._get_smart_payment_counterpart_lines()
        )

    def _get_smart_payment_counterpart_lines(self, account=None):
        """Return the payment's still-open customer receivable lines.

        Read the posted journal entry directly instead of depending on payment
        status helpers.  This is intentionally compatible with payments created
        before this module was installed and with partially reconciled payments.
        For an inbound customer payment, the receivable residual is a credit and
        therefore negative in company currency.
        """
        self.ensure_one()
        if not self.move_id or self.move_id.state != "posted" or not self.partner_id:
            return self.env["account.move.line"]

        commercial_partner = self.partner_id.commercial_partner_id
        lines = self.move_id.line_ids.filtered(
            lambda line: (
                line.parent_state == "posted"
                and line.account_id.account_type == "asset_receivable"
                and not line.reconciled
                and line.amount_residual < 0.0
                and not line.company_currency_id.is_zero(line.amount_residual)
                and line.partner_id
                and line.partner_id.commercial_partner_id == commercial_partner
                and (not account or line.account_id == account)
            )
        )
        return lines.sorted(lambda line: (line.date or self.date, line.id))

    def _get_oldest_open_invoice_lines(self, account, historical_only=False):
        self.ensure_one()
        domain = [
            ("company_id", "=", self.company_id.id),
            ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
            ("parent_state", "=", "posted"),
            ("account_id", "=", account.id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("move_id.move_type", "=", "out_invoice"),
            ("reconciled", "=", False),
            ("amount_residual", ">", 0.0),
        ]
        if historical_only:
            activation_datetime = self.company_id._get_smart_existing_activation_datetime()
            if activation_datetime:
                domain.append(("move_id.create_date", "<=", activation_datetime))

        return self.env["account.move.line"].sudo().with_company(self.company_id).search(
            domain,
            order="date_maturity asc, date asc, id asc",
        )

    def _match_open_invoices_oldest_first(self, historical_only=False):
        """Match this payment directly against the oldest open invoices.

        The standard Odoo reconciliation engine creates partial reconciliations
        automatically when the payment is lower than the invoice residual. The
        method then continues with the next invoice while payment balance remains.
        """
        self.ensure_one()
        result = {
            "payment_id": self.id,
            "matched": False,
            "matched_amount": 0.0,
            "invoice_count": 0,
            "invoice_names": [],
            "reason": False,
        }

        if not self._is_smart_allocation_candidate():
            result["reason"] = _("The payment has no open customer receivable balance.")
            return result

        company_currency = self.company_id.currency_id
        payment_lines = self._get_smart_payment_counterpart_lines().sorted("id")

        for payment_line in payment_lines:
            invoice_lines = self._get_oldest_open_invoice_lines(
                payment_line.account_id,
                historical_only=historical_only,
            )
            for invoice_line in invoice_lines:
                if payment_line.reconciled or company_currency.is_zero(payment_line.amount_residual):
                    break
                if invoice_line.reconciled or company_currency.is_zero(invoice_line.amount_residual):
                    continue

                payment_residual_before = abs(payment_line.amount_residual)
                invoice_residual_before = abs(invoice_line.amount_residual)

                (payment_line + invoice_line).reconcile()
                payment_line.invalidate_recordset(
                    ["amount_residual", "amount_residual_currency", "reconciled"]
                )
                invoice_line.invalidate_recordset(
                    ["amount_residual", "amount_residual_currency", "reconciled"]
                )

                # Measure what Odoo actually reconciled.  This is safer than
                # assuming the minimum residual in multi-currency cases.
                payment_residual_after = abs(payment_line.amount_residual)
                matched_amount = max(payment_residual_before - payment_residual_after, 0.0)
                if company_currency.is_zero(matched_amount):
                    # A reconciliation that consumed the invoice but whose payment
                    # residual rounded to the same value is still a valid match.
                    invoice_residual_after = abs(invoice_line.amount_residual)
                    matched_amount = max(invoice_residual_before - invoice_residual_after, 0.0)
                if company_currency.is_zero(matched_amount):
                    continue

                result["matched"] = True
                result["matched_amount"] += matched_amount
                result["invoice_count"] += 1
                result["invoice_names"].append(invoice_line.move_id.display_name)

        if result["matched"]:
            decimals = company_currency.decimal_places
            amount_text = (
                f"{company_currency.round(result['matched_amount']):,.{decimals}f} "
                f"{company_currency.name}"
            )
            note = _(
                "Matched %(amount)s against %(count)s invoice(s): %(invoices)s",
                amount=amount_text,
                count=result["invoice_count"],
                invoices=", ".join(result["invoice_names"]),
            )
            self.write({
                "smart_allocation_state": "allocated",
                "smart_allocation_confidence": 70.0,
                "smart_allocation_note": note,
            })
            self.message_post(body=note)
        else:
            result["reason"] = _("No open invoice was found on the same receivable account.")
            self.write({
                "smart_allocation_state": "review",
                "smart_allocation_confidence": 0.0,
                "smart_allocation_note": result["reason"],
            })

        return result

    def _prepare_smart_allocation_wizard_vals(self, existing_backfill=False, strategy=None):
        self.ensure_one()
        return {
            "payment_id": self.id,
            "allocation_mode": "distribute",
            "strategy": strategy or self.company_id.smart_payment_allocation_strategy,
            "existing_backfill": existing_backfill,
        }

    def action_open_smart_allocation(self):
        self.ensure_one()
        wizard = self.env["smart.payment.allocation.wizard"].create(
            self._prepare_smart_allocation_wizard_vals(
                existing_backfill=self.smart_existing_backfill,
            )
        )
        wizard.action_reload_invoices()
        wizard.action_suggest()
        return {
            "type": "ir.actions.act_window",
            "name": _("Smart Payment Allocation"),
            "res_model": "smart.payment.allocation.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def _run_smart_auto_allocation(self):
        for payment in self:
            if not payment.company_id.smart_payment_auto_allocate:
                continue
            if not payment._is_smart_allocation_candidate():
                continue

            wizard = self.env["smart.payment.allocation.wizard"].create(
                payment._prepare_smart_allocation_wizard_vals()
            )
            wizard.action_reload_invoices()
            wizard.action_suggest()

            if not wizard.line_ids.filtered("selected"):
                payment.write({
                    "smart_allocation_state": "review",
                    "smart_allocation_confidence": wizard.confidence,
                    "smart_allocation_note": _("No suitable open invoices were found."),
                })
                continue

            if wizard.confidence >= payment.company_id.smart_payment_confidence_threshold:
                wizard.action_apply_allocation()
            else:
                payment.write({
                    "smart_allocation_state": "review",
                    "smart_allocation_confidence": wizard.confidence,
                    "smart_allocation_note": _("A suggestion is available and requires review."),
                })
                payment.message_post(
                    body=_(
                        "Smart allocation found a suggestion with %(confidence).2f%% confidence. Manual review is required.",
                        confidence=wizard.confidence,
                    )
                )

    @api.model
    def _get_existing_unallocated_payment_domain(self, company):
        """Backward-compatible helper retained for external calls.

        Existing matching now works from open receivable journal items, because
        historical receipts may come from payments, bank statements, or imported
        journal entries.
        """
        return [("id", "=", 0)]

    @api.model
    def _process_existing_unallocated_payments(self, company=None, limit=1000):
        return self.env["account.move.line"]._process_existing_unallocated_customer_credits(
            company=company,
            limit=limit,
        )

    @api.model
    def _cron_process_existing_unallocated_payments(self):
        return self.env["account.move.line"]._cron_process_existing_unallocated_customer_credits()

    def action_post(self):
        result = super().action_post()

        for payment in self.filtered(
            lambda record: record.payment_type == "inbound"
            and record.partner_type == "customer"
            and record.partner_id
        ):
            payment.customer_debt_before_payment = payment.customer_outstanding_amount

        for payment in self:
            try:
                with self.env.cr.savepoint():
                    payment._run_smart_auto_allocation()
            except Exception as error:
                _logger.exception("Smart payment auto allocation failed for payment %s", payment.id)
                payment.smart_allocation_state = "review"
                payment.smart_allocation_note = _(
                    "Automatic allocation failed: %(error)s",
                    error=str(error),
                )
                payment.message_post(
                    body=_("Automatic smart allocation could not be completed. Please review the payment manually.")
                )
        return result
