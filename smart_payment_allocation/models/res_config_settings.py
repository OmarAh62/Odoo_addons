import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    smart_payment_auto_allocate = fields.Boolean(
        related="company_id.smart_payment_auto_allocate", readonly=False
    )
    smart_payment_allocation_strategy = fields.Selection(
        related="company_id.smart_payment_allocation_strategy", readonly=False
    )
    smart_payment_confidence_threshold = fields.Float(
        related="company_id.smart_payment_confidence_threshold", readonly=False
    )
    smart_payment_max_combination_size = fields.Integer(
        related="company_id.smart_payment_max_combination_size", readonly=False
    )
    smart_payment_candidate_limit = fields.Integer(
        related="company_id.smart_payment_candidate_limit", readonly=False
    )
    smart_match_existing_records = fields.Boolean(
        related="company_id.smart_match_existing_records", readonly=False
    )
    smart_existing_matching_mode = fields.Selection(
        related="company_id.smart_existing_matching_mode", readonly=False
    )

    def action_open_manual_payment_matching(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Manual Payment Matching"),
            "res_model": "smart.payment.match.customer",
            "view_mode": "tree",
            "view_id": self.env.ref(
                "smart_payment_allocation.view_smart_payment_match_customer_list"
            ).id,
            "search_view_id": self.env.ref(
                "smart_payment_allocation.view_smart_payment_match_customer_search"
            ).id,
            "context": {},
        }

    def _count_existing_matchable_credits(self, company):
        return self.env["account.move.line"].sudo().with_company(company).search_count(
            self.env["account.move.line"]._smart_unmatched_customer_credit_domain(company)
        )

    def action_run_existing_matching_now(self):
        self.ensure_one()
        company = self.company_id
        if not company.smart_match_existing_records:
            raise UserError(_("Enable Match Existing Payments and Invoices first."))
        if company.smart_existing_matching_mode != "automatic":
            raise UserError(_("Select Automatic mode before running automatic matching."))

        eligible = self._count_existing_matchable_credits(company)
        totals = self.env["account.move.line"].sudo()._process_existing_unallocated_customer_credits(
            company=company,
            limit=10000,
        )
        currency = company.currency_id
        decimals = currency.decimal_places
        amount_text = f"{currency.round(totals['matched_amount']):,.{decimals}f} {currency.name}"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Existing Payment Matching"),
                "message": _(
                    "%(eligible)s open payment/credit item(s) found. "
                    "%(processed)s item(s) checked; %(allocated)s item(s) matched "
                    "for %(amount)s. No matching invoice: %(no_match)s. Errors: %(errors)s.",
                    eligible=eligible,
                    processed=totals["processed"],
                    allocated=totals["allocated"],
                    amount=amount_text,
                    no_match=totals["no_match"],
                    errors=totals["errors"],
                ),
                "type": "warning" if totals["errors"] else "success",
                "sticky": bool(totals["errors"]),
            },
        }

    def set_values(self):
        result = super().set_values()
        CreditLine = self.env["account.move.line"].sudo()

        for settings in self:
            company = settings.company_id
            if (
                not company.smart_match_existing_records
                or company.smart_existing_matching_mode != "automatic"
            ):
                continue

            try:
                CreditLine._process_existing_unallocated_customer_credits(
                    company=company,
                    limit=10000,
                )
            except Exception:
                _logger.exception(
                    "Immediate existing payment allocation failed for company %s",
                    company.id,
                )

        return result
