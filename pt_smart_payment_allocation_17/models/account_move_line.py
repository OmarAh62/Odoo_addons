import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.model
    def _smart_unmatched_customer_credit_domain(self, company, partner=None):
        """Return all open customer credit/payment journal items.

        This deliberately works at journal-item level instead of account.payment.
        It therefore supports:
        - payments created before the module was installed;
        - bank statement receipts;
        - imported/manual receipt journal entries;
        - partially reconciled credits.
        """
        domain = [
            ("company_id", "=", company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("account_id.reconcile", "=", True),
            ("move_id.move_type", "in", ("entry", "out_refund")),
            ("partner_id", "!=", False),
            ("reconciled", "=", False),
            ("amount_residual", "<", 0.0),
        ]
        if partner:
            domain.append(("partner_id", "child_of", partner.commercial_partner_id.id))
        return domain

    def _smart_oldest_open_invoice_lines(self):
        self.ensure_one()
        commercial_partner = self.partner_id.commercial_partner_id
        return self.env["account.move.line"].sudo().with_company(self.company_id).search(
            [
                ("company_id", "=", self.company_id.id),
                ("partner_id", "child_of", commercial_partner.id),
                ("parent_state", "=", "posted"),
                ("account_id", "=", self.account_id.id),
                ("account_id.account_type", "=", "asset_receivable"),
                ("move_id.move_type", "=", "out_invoice"),
                ("reconciled", "=", False),
                ("amount_residual", ">", 0.0),
            ],
            order="date_maturity asc, date asc, id asc",
        )

    def _smart_match_customer_credit_oldest_first(self):
        """Reconcile one open customer credit against oldest open invoices.

        Odoo's standard reconciliation engine automatically creates a partial
        reconciliation when the credit is lower than the invoice residual and
        allows the remaining credit to continue to the next invoice.
        """
        self.ensure_one()
        result = {
            "credit_line_id": self.id,
            "matched": False,
            "matched_amount": 0.0,
            "invoice_count": 0,
            "invoice_names": [],
            "reason": False,
        }

        company_currency = self.company_id.currency_id
        if (
            self.parent_state != "posted"
            or self.account_id.account_type != "asset_receivable"
            or self.move_id.move_type not in ("entry", "out_refund")
            or not self.partner_id
            or self.reconciled
            or self.amount_residual >= 0.0
            or company_currency.is_zero(self.amount_residual)
        ):
            result["reason"] = _("This journal item is not an open customer payment, credit, or credit note.")
            return result

        for invoice_line in self._smart_oldest_open_invoice_lines():
            self.invalidate_recordset(
                ["amount_residual", "amount_residual_currency", "reconciled"]
            )
            invoice_line.invalidate_recordset(
                ["amount_residual", "amount_residual_currency", "reconciled"]
            )

            if self.reconciled or company_currency.is_zero(self.amount_residual):
                break
            if invoice_line.reconciled or company_currency.is_zero(invoice_line.amount_residual):
                continue

            credit_before = abs(self.amount_residual)
            invoice_before = abs(invoice_line.amount_residual)

            (self + invoice_line).reconcile()

            self.invalidate_recordset(
                ["amount_residual", "amount_residual_currency", "reconciled"]
            )
            invoice_line.invalidate_recordset(
                ["amount_residual", "amount_residual_currency", "reconciled"]
            )

            matched_amount = max(credit_before - abs(self.amount_residual), 0.0)
            if company_currency.is_zero(matched_amount):
                matched_amount = max(invoice_before - abs(invoice_line.amount_residual), 0.0)
            if company_currency.is_zero(matched_amount):
                continue

            result["matched"] = True
            result["matched_amount"] += matched_amount
            result["invoice_count"] += 1
            result["invoice_names"].append(invoice_line.move_id.display_name)

        payment = self.payment_id
        source_label = _("Credit Note") if self.move_id.move_type == "out_refund" else _("Payment/Credit")
        if result["matched"]:
            decimals = company_currency.decimal_places
            amount_text = (
                f"{company_currency.round(result['matched_amount']):,.{decimals}f} "
                f"{company_currency.name}"
            )
            note = _(
                "%(source_label)s %(document)s matched %(amount)s against %(count)s invoice(s): %(invoices)s",
                source_label=source_label,
                document=self.move_id.display_name,
                amount=amount_text,
                count=result["invoice_count"],
                invoices=", ".join(result["invoice_names"]),
            )
            if payment:
                payment.write(
                    {
                        "smart_allocation_state": "allocated",
                        "smart_allocation_confidence": 70.0,
                        "smart_allocation_note": note,
                        "smart_existing_backfill": bool(self.reconciled),
                    }
                )
                payment.message_post(body=note)
            elif self.move_id.move_type == "out_refund":
                self.move_id.message_post(body=note)
        else:
            result["reason"] = _(
                "No open invoice was found for this customer on the same receivable account."
            )
            if payment:
                payment.write(
                    {
                        "smart_allocation_state": "review",
                        "smart_allocation_confidence": 0.0,
                        "smart_allocation_note": result["reason"],
                    }
                )

        return result

    @api.model
    def _process_existing_unallocated_customer_credits(self, company=None, limit=1000):
        companies = company or self.env["res.company"].search([])
        totals = {
            "processed": 0,
            "allocated": 0,
            "matched_amount": 0.0,
            "no_match": 0,
            "errors": 0,
        }

        for current_company in companies:
            if (
                not current_company.smart_match_existing_records
                or current_company.smart_existing_matching_mode != "automatic"
            ):
                continue

            credit_lines = self.sudo().with_company(current_company).search(
                self._smart_unmatched_customer_credit_domain(current_company),
                order="date asc, id asc",
                limit=limit or 1000,
            )

            for credit_line in credit_lines:
                totals["processed"] += 1
                try:
                    with self.env.cr.savepoint():
                        match_result = credit_line._smart_match_customer_credit_oldest_first()
                        if match_result["matched"]:
                            totals["allocated"] += 1
                            totals["matched_amount"] += match_result["matched_amount"]
                        else:
                            totals["no_match"] += 1
                except Exception:
                    totals["errors"] += 1
                    _logger.exception(
                        "Existing customer credit matching failed for journal item %s",
                        credit_line.id,
                    )

        return totals

    @api.model
    def _cron_process_existing_unallocated_customer_credits(self):
        return self._process_existing_unallocated_customer_credits(limit=1000)
