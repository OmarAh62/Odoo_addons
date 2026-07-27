from itertools import combinations
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class SmartPaymentAllocationWizard(models.TransientModel):
    _name = "smart.payment.allocation.wizard"
    _description = "Smart Payment Allocation Wizard"

    payment_id = fields.Many2one(
        comodel_name="account.payment",
        string="Payment",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        related="payment_id.partner_id",
        string="Customer",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="payment_id.company_id",
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="payment_id.currency_id",
        readonly=True,
    )
    payment_amount = fields.Monetary(
        string="Payment Amount",
        currency_field="currency_id",
        compute="_compute_totals",
    )
    available_amount = fields.Monetary(
        string="Available Amount",
        currency_field="currency_id",
        compute="_compute_totals",
    )
    suggested_amount = fields.Monetary(
        string="Suggested Amount",
        currency_field="currency_id",
        compute="_compute_totals",
    )
    remaining_amount = fields.Monetary(
        string="Remaining Amount",
        currency_field="currency_id",
        compute="_compute_totals",
    )
    confidence = fields.Float(
        string="Confidence %",
        readonly=True,
    )
    allocation_mode = fields.Selection(
        selection=[
            ("distribute", "Distribute Across Invoices"),
            ("single", "Pay One Invoice"),
        ],
        string="Allocation Method",
        required=True,
        default="distribute",
        help=(
            "Distribute Across Invoices uses the selected matching strategy and consumes "
            "the payment across multiple invoices. Pay One Invoice restricts the payment "
            "to a single selected invoice and leaves any excess as unallocated customer credit."
        ),
    )
    strategy = fields.Selection(
        selection=[
            ("smart", "Smart Suggestion"),
            ("oldest", "Oldest Due First"),
            ("newest", "Newest Due First"),
            ("exact", "Exact Amount / Combination"),
            ("reference", "Payment Reference"),
        ],
        string="Strategy",
        required=True,
        default="smart",
    )
    existing_backfill = fields.Boolean(
        string="Existing Payment Backfill",
        readonly=True,
        help="Restricts suggestions to invoices that existed before historical matching was enabled.",
    )
    result_note = fields.Char(string="Suggestion Result", readonly=True)
    line_ids = fields.One2many(
        comodel_name="smart.payment.allocation.wizard.line",
        inverse_name="wizard_id",
        string="Open Invoices",
    )

    @api.depends(
        "payment_id.amount",
        "payment_id.move_id.line_ids.amount_residual",
        "payment_id.move_id.line_ids.amount_residual_currency",
        "line_ids.selected",
        "line_ids.suggested_amount",
    )
    def _compute_totals(self):
        for wizard in self:
            wizard.payment_amount = wizard.payment_id.amount
            wizard.available_amount = wizard._get_available_payment_amount()
            wizard.suggested_amount = sum(wizard.line_ids.filtered("selected").mapped("suggested_amount"))
            wizard.remaining_amount = max(wizard.available_amount - wizard.suggested_amount, 0.0)

    def _get_available_payment_amount(self):
        self.ensure_one()
        payment = self.payment_id
        if not payment.move_id:
            return payment.amount

        counterpart_lines = payment._get_smart_payment_counterpart_lines()
        total = 0.0
        for line in counterpart_lines:
            if line.currency_id and line.currency_id == payment.currency_id:
                total += abs(line.amount_residual_currency)
            else:
                total += payment.company_id.currency_id._convert(
                    abs(line.amount_residual),
                    payment.currency_id,
                    payment.company_id,
                    payment.date,
                )
        return total

    def _get_open_invoice_lines(self):
        self.ensure_one()
        commercial_partner = self.payment_id.partner_id.commercial_partner_id
        domain = [
            ("partner_id", "child_of", commercial_partner.id),
            ("company_id", "=", self.payment_id.company_id.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("reconciled", "=", False),
            ("move_id.move_type", "=", "out_invoice"),
            ("amount_residual", ">", 0.0),
        ]
        if self.existing_backfill:
            activation_datetime = self.company_id._get_smart_existing_activation_datetime()
            if activation_datetime:
                domain.append(("move_id.create_date", "<=", activation_datetime))
        return self.env["account.move.line"].search(
            domain,
            order="date_maturity asc, date asc, id asc",
        )

    def _convert_invoice_residual(self, move_line):
        self.ensure_one()
        invoice_currency = move_line.currency_id or move_line.company_currency_id
        if invoice_currency == self.currency_id:
            if move_line.currency_id:
                return abs(move_line.amount_residual_currency)
            return abs(move_line.amount_residual)
        source_amount = (
            abs(move_line.amount_residual_currency)
            if move_line.currency_id
            else abs(move_line.amount_residual)
        )
        return invoice_currency._convert(
            source_amount,
            self.currency_id,
            self.company_id,
            self.payment_id.date,
        )

    def action_reload_invoices(self):
        self.ensure_one()
        self.line_ids.unlink()
        commands = []
        for sequence, move_line in enumerate(self._get_open_invoice_lines(), start=1):
            commands.append((0, 0, {
                "sequence": sequence,
                "move_line_id": move_line.id,
                "invoice_id": move_line.move_id.id,
                "invoice_date": move_line.move_id.invoice_date,
                "due_date": move_line.date_maturity,
                "invoice_currency_id": (move_line.currency_id or move_line.company_currency_id).id,
                "invoice_residual": abs(
                    move_line.amount_residual_currency
                    if move_line.currency_id
                    else move_line.amount_residual
                ),
                "residual_payment_currency": self._convert_invoice_residual(move_line),
            }))
        self.write({"line_ids": commands, "confidence": 0.0, "result_note": False})
        return True

    @staticmethod
    def _normalize_reference(value):
        return re.sub(r"[^A-Z0-9]", "", (value or "").upper())

    def _payment_reference_text(self):
        self.ensure_one()
        payment = self.payment_id
        return " ".join(filter(None, [payment.memo, payment.name, payment.move_id.ref]))

    @api.onchange("allocation_mode")
    def _onchange_allocation_mode(self):
        for wizard in self:
            wizard._reset_suggestions()

    def _reset_suggestions(self):
        for line in self.line_ids:
            line.selected = False
            line.suggested_amount = 0.0
            line.confidence = 0.0
            line.match_reason = False
        self.confidence = 0.0
        self.result_note = False

    def _select_lines_in_order(self, lines, confidence, reason):
        self.ensure_one()
        remaining = self.available_amount
        selected_count = 0
        max_invoices = 1 if self.allocation_mode == "single" else False
        for line in lines:
            if max_invoices and selected_count >= max_invoices:
                break
            if self.currency_id.is_zero(remaining):
                break
            allocation = min(remaining, line.residual_payment_currency)
            if self.currency_id.is_zero(allocation):
                continue
            line.write({
                "selected": True,
                "suggested_amount": allocation,
                "confidence": confidence,
                "match_reason": reason,
            })
            remaining -= allocation
            selected_count += 1
        self.confidence = confidence if selected_count else 0.0
        return selected_count

    def _suggest_by_reference(self):
        self.ensure_one()
        normalized_payment_ref = self._normalize_reference(self._payment_reference_text())
        if not normalized_payment_ref:
            return 0

        matched = self.env["smart.payment.allocation.wizard.line"]
        for line in self.line_ids:
            references = [line.invoice_id.name, line.invoice_id.ref, line.invoice_id.payment_reference]
            if any(
                normalized_reference and normalized_reference in normalized_payment_ref
                for normalized_reference in map(self._normalize_reference, references)
            ):
                matched |= line

        if not matched:
            return 0
        return self._select_lines_in_order(matched.sorted("sequence"), 100.0, _("Invoice reference found in payment"))

    def _find_exact_combination(self):
        self.ensure_one()
        amount = self.available_amount
        lines = self.line_ids.sorted("sequence")[: self.company_id.smart_payment_candidate_limit]
        max_size = min(self.company_id.smart_payment_max_combination_size, len(lines))

        for line in lines:
            if self.currency_id.is_zero(line.residual_payment_currency - amount):
                return line

        if self.allocation_mode == "single":
            return self.env["smart.payment.allocation.wizard.line"]

        for size in range(2, max_size + 1):
            for candidate in combinations(lines, size):
                candidate_total = sum(line.residual_payment_currency for line in candidate)
                if self.currency_id.is_zero(candidate_total - amount):
                    result = self.env["smart.payment.allocation.wizard.line"]
                    for line in candidate:
                        result |= line
                    return result
        return self.env["smart.payment.allocation.wizard.line"]

    def _suggest_exact(self):
        self.ensure_one()
        exact_lines = self._find_exact_combination()
        if not exact_lines:
            return 0
        reason = _("Exact invoice amount") if len(exact_lines) == 1 else _("Exact invoice combination")
        confidence = 98.0 if len(exact_lines) == 1 else 95.0
        return self._select_lines_in_order(exact_lines.sorted("sequence"), confidence, reason)

    def _suggest_oldest(self):
        self.ensure_one()
        lines = self.line_ids.sorted(
            key=lambda line: (line.due_date or line.invoice_date or fields.Date.today(), line.sequence)
        )
        return self._select_lines_in_order(lines, 70.0, _("Oldest due invoice first"))

    def _suggest_newest(self):
        self.ensure_one()
        lines = self.line_ids.sorted(
            key=lambda line: (line.due_date or line.invoice_date or fields.Date.today(), line.sequence),
            reverse=True,
        )
        return self._select_lines_in_order(lines, 65.0, _("Newest due invoice first"))

    def action_suggest(self):
        self.ensure_one()
        if not self.payment_id._is_smart_allocation_candidate():
            raise UserError(_("The payment must be a posted, unreconciled inbound customer payment."))
        if not self.line_ids:
            self.action_reload_invoices()

        self._reset_suggestions()
        selected_count = 0

        if self.strategy == "reference":
            selected_count = self._suggest_by_reference()
        elif self.strategy == "exact":
            selected_count = self._suggest_exact()
        elif self.strategy == "oldest":
            selected_count = self._suggest_oldest()
        elif self.strategy == "newest":
            selected_count = self._suggest_newest()
        else:
            selected_count = self._suggest_by_reference()
            if not selected_count:
                selected_count = self._suggest_exact()
            if not selected_count:
                selected_count = self._suggest_oldest()

        if selected_count:
            mode_label = dict(self._fields["allocation_mode"].selection).get(self.allocation_mode)
            strategy_label = dict(self._fields["strategy"].selection).get(self.strategy)
            self.result_note = _(
                "%(count)s invoice(s) suggested using %(mode)s / %(strategy)s.",
                count=selected_count,
                mode=mode_label,
                strategy=strategy_label,
            )
            self.payment_id.write({
                "smart_allocation_state": "suggested",
                "smart_allocation_confidence": self.confidence,
                "smart_allocation_note": self.result_note,
            })
        else:
            self.result_note = _("No matching open invoice was found.")
            self.payment_id.write({
                "smart_allocation_state": "review",
                "smart_allocation_confidence": 0.0,
                "smart_allocation_note": self.result_note,
            })
        return True

    def action_clear_suggestion(self):
        self.ensure_one()
        self._reset_suggestions()
        return True

    def action_apply_allocation(self):
        self.ensure_one()
        payment = self.payment_id
        if not payment._is_smart_allocation_candidate():
            raise UserError(_("The payment is no longer available for allocation."))

        selected_lines = self.line_ids.filtered("selected").sorted("sequence")
        if not selected_lines:
            raise ValidationError(_("Select at least one invoice before applying the allocation."))
        if self.allocation_mode == "single" and len(selected_lines) > 1:
            raise ValidationError(_("Pay One Invoice allows only one selected invoice."))

        allocated_details = []
        reconciled_invoice_names = []
        for wizard_line in selected_lines:
            invoice_line = wizard_line.move_line_id
            if invoice_line.reconciled:
                continue

            payment_lines = payment._get_smart_payment_counterpart_lines(account=invoice_line.account_id)
            if not payment_lines:
                raise UserError(_(
                    "No open payment line was found on receivable account %(account)s for invoice %(invoice)s.",
                    account=invoice_line.account_id.display_name,
                    invoice=wizard_line.invoice_id.display_name,
                ))

            payment_line = payment_lines[0]
            allocation_amount = min(
                wizard_line.suggested_amount or wizard_line.residual_payment_currency,
                wizard_line.residual_payment_currency,
                self._get_available_payment_amount(),
            )
            (payment_line + invoice_line).reconcile()
            reconciled_invoice_names.append(wizard_line.invoice_id.display_name)
            decimals = payment.currency_id.decimal_places
            amount_text = f"{payment.currency_id.round(allocation_amount):,.{decimals}f} {payment.currency_id.name}"
            allocated_details.append(f"{wizard_line.invoice_id.display_name}: {amount_text}")

            if not payment._get_smart_payment_counterpart_lines():
                break

        if not reconciled_invoice_names:
            raise UserError(_("No invoice could be reconciled."))

        if self.allocation_mode == "single":
            note = _("Allocated to one invoice: %(details)s", details="; ".join(allocated_details))
        else:
            note = _("Distributed allocation: %(details)s", details="; ".join(allocated_details))
        payment.write({
            "smart_allocation_state": "allocated",
            "smart_allocation_confidence": self.confidence,
            "smart_allocation_note": note,
        })
        payment.message_post(body=_("Smart payment allocation completed.<br/>%(note)s", note=note))
        return {"type": "ir.actions.act_window_close"}


class SmartPaymentAllocationWizardLine(models.TransientModel):
    _name = "smart.payment.allocation.wizard.line"
    _description = "Smart Payment Allocation Wizard Line"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        comodel_name="smart.payment.allocation.wizard",
        required=True,
        ondelete="cascade",
    )
    sequence = fields.Integer(default=10)
    selected = fields.Boolean(string="Select")
    move_line_id = fields.Many2one(
        comodel_name="account.move.line",
        string="Receivable Line",
        required=True,
        readonly=True,
    )
    invoice_id = fields.Many2one(
        comodel_name="account.move",
        string="Invoice",
        required=True,
        readonly=True,
    )
    invoice_date = fields.Date(readonly=True)
    due_date = fields.Date(readonly=True)
    invoice_currency_id = fields.Many2one("res.currency", readonly=True)
    invoice_residual = fields.Monetary(
        string="Invoice Residual",
        currency_field="invoice_currency_id",
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="wizard_id.currency_id",
        readonly=True,
    )
    residual_payment_currency = fields.Monetary(
        string="Residual in Payment Currency",
        currency_field="currency_id",
        readonly=True,
    )
    suggested_amount = fields.Monetary(
        string="Suggested Allocation",
        currency_field="currency_id",
        readonly=True,
    )
    confidence = fields.Float(string="Confidence %", readonly=True)
    match_reason = fields.Char(string="Matching Reason", readonly=True)

    @api.onchange("selected")
    def _onchange_selected(self):
        for line in self:
            if not line.selected:
                line.suggested_amount = 0.0
                continue

            if line.wizard_id.allocation_mode == "single":
                for other in line.wizard_id.line_ids:
                    if other != line:
                        other.selected = False
                        other.suggested_amount = 0.0
                        other.confidence = 0.0
                        other.match_reason = False

            other_amount = sum(
                other.suggested_amount
                for other in line.wizard_id.line_ids
                if other != line and other.selected
            )
            available = max(line.wizard_id.available_amount - other_amount, 0.0)
            line.suggested_amount = min(available, line.residual_payment_currency)
            if not line.match_reason:
                line.match_reason = _("Manual selection")
            if not line.confidence:
                line.confidence = 50.0
