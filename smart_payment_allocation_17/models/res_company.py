from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    smart_payment_auto_allocate = fields.Boolean(
        string="Auto Allocate Customer Payments",
        help=(
            "Automatically reconcile newly posted customer payments when the "
            "suggestion confidence reaches the configured threshold."
        ),
    )
    smart_payment_allocation_strategy = fields.Selection(
        selection=[
            ("smart", "Smart Suggestion"),
            ("oldest", "Oldest Due First"),
            ("newest", "Newest Due First"),
            ("exact", "Exact Amount / Combination"),
            ("reference", "Payment Reference"),
        ],
        string="Default Allocation Strategy",
        default="smart",
        required=True,
    )
    smart_payment_confidence_threshold = fields.Float(
        string="Auto Allocation Confidence %",
        default=95.0,
        help=(
            "Automatic reconciliation of newly posted payments is only performed "
            "when the suggestion confidence reaches this percentage."
        ),
    )
    smart_payment_max_combination_size = fields.Integer(
        string="Maximum Combination Size",
        default=4,
        help="Maximum number of invoices used when searching for an exact invoice combination.",
    )
    smart_payment_candidate_limit = fields.Integer(
        string="Combination Candidate Limit",
        default=20,
        help="Maximum number of open invoices considered by the exact-combination search.",
    )

    # Non-stored to avoid adding a new res.company database column during upgrade.
    # The value is stored per company in ir.config_parameter.
    smart_match_existing_records = fields.Boolean(
        string="Automatically Match Existing Payments and Invoices",
        compute="_compute_smart_match_existing_records",
        inverse="_inverse_smart_match_existing_records",
        compute_sudo=True,
        store=False,
        help=(
            "When enabled, the system automatically matches customer payments and "
            "open customer invoices that already existed before this option was enabled."
        ),
    )
    smart_existing_matching_mode = fields.Selection(
        selection=[
            ("automatic", "Automatic"),
            ("manual", "Manual Review"),
        ],
        string="Existing Records Matching Mode",
        compute="_compute_smart_existing_matching_mode",
        inverse="_inverse_smart_existing_matching_mode",
        compute_sudo=True,
        store=False,
        help=(
            "Automatic runs historical matching in the background. Manual Review "
            "keeps historical payments unchanged until a user selects customers "
            "from the Manual Payment Matching screen."
        ),
    )

    def _smart_existing_parameter_key(self, setting_name):
        self.ensure_one()
        return "smart_payment_allocation.%s.company_%s" % (setting_name, self.id)

    def _compute_smart_match_existing_records(self):
        parameters = self.env["ir.config_parameter"].sudo()
        for company in self:
            company.smart_match_existing_records = parameters.get_param(
                company._smart_existing_parameter_key("match_existing_records"),
                default="False",
            ) == "True"

    def _inverse_smart_match_existing_records(self):
        parameters = self.env["ir.config_parameter"].sudo()
        for company in self:
            parameters.set_param(
                company._smart_existing_parameter_key("match_existing_records"),
                bool(company.smart_match_existing_records),
            )

    def _compute_smart_existing_matching_mode(self):
        parameters = self.env["ir.config_parameter"].sudo()
        for company in self:
            value = parameters.get_param(
                company._smart_existing_parameter_key("existing_matching_mode"),
                default="automatic",
            )
            company.smart_existing_matching_mode = (
                value if value in {"automatic", "manual"} else "automatic"
            )

    def _inverse_smart_existing_matching_mode(self):
        parameters = self.env["ir.config_parameter"].sudo()
        for company in self:
            parameters.set_param(
                company._smart_existing_parameter_key("existing_matching_mode"),
                company.smart_existing_matching_mode or "automatic",
            )

    def _get_smart_existing_activation_datetime(self):
        self.ensure_one()
        raw_value = self.env["ir.config_parameter"].sudo().get_param(
            self._smart_existing_parameter_key("existing_activation_datetime")
        )
        return fields.Datetime.to_datetime(raw_value) if raw_value else False

    def _ensure_smart_existing_activation_datetime(self):
        self.ensure_one()
        activation_datetime = self._get_smart_existing_activation_datetime()
        if activation_datetime:
            return activation_datetime

        activation_datetime = fields.Datetime.now()
        self.env["ir.config_parameter"].sudo().set_param(
            self._smart_existing_parameter_key("existing_activation_datetime"),
            fields.Datetime.to_string(activation_datetime),
        )
        return activation_datetime

    @api.constrains("smart_payment_confidence_threshold")
    def _check_smart_payment_confidence_threshold(self):
        for company in self:
            if not 0.0 <= company.smart_payment_confidence_threshold <= 100.0:
                raise ValidationError("The confidence threshold must be between 0 and 100.")

    @api.constrains(
        "smart_payment_max_combination_size",
        "smart_payment_candidate_limit",
    )
    def _check_smart_payment_limits(self):
        for company in self:
            if company.smart_payment_max_combination_size < 1:
                raise ValidationError("The maximum combination size must be at least 1.")
            if company.smart_payment_candidate_limit < 1:
                raise ValidationError("The candidate limit must be at least 1.")
