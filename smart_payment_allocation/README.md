# Smart Payment Allocation for Odoo 17

> Ported from the Odoo 18.0.1.10.2 codebase. `<list>` view tags and
> `view_mode`/`view_id` values were converted back to the Odoo 17 `<tree>`
> convention (the tree→list rename only happened in Odoo 18). No other API
> changes were required for the fields/methods this module uses
> (`account_type`, `parent_state`, `invisible="condition"` syntax, and the
> `block`/`setting` settings layout are all already valid in 17). Please test
> against a real Odoo 17 instance before deploying, in particular the
> `res_config_settings` xpath target `pay_invoice_online_setting_container`,
> since third-party core view anchor points can shift between versions.


Version 18.0.1.9.0.

## Matching engine

Existing matching now works directly from open receivable journal items rather than only from `account.payment` records. It detects:

- unreconciled customer payments;
- bank statement customer receipts;
- imported or manual customer credit journal entries;
- partially reconciled customer credits.

Credits are matched oldest first against the customer's oldest due posted invoices on the same receivable account. Partial matching and distribution across multiple invoices are supported.

## Modes

- Automatic: runs immediately from settings and periodically through the scheduled action.
- Manual Review: Accounting > Customers > Manual Payment Matching.

The manual screen shows one row per customer with both an open customer credit/payment and an open invoice.


## 18.0.1.10.0

- Existing automatic and manual matching now include posted customer credit notes/returns (`out_refund`).
- Open credit notes are treated as customer credits and allocated against the oldest open invoices.
- Partial matching is supported; any remaining credit continues to the next invoice.
- A chatter note is posted on the matched credit note.


## 18.0.1.10.1 - Manual matching fix

- Manual screen now calculates matchable values only when credit and invoice lines use the same reconcilable receivable account.
- Match button processes only eligible same-account lines.
- Hidden exceptions are now shown to the user when no reconciliation is created.
- Automatic matching logic is intentionally unchanged in this release; it will be addressed after manual matching is confirmed.


## 18.0.1.10.2
- Fixed manual matching crash caused by using the reserved translation keyword `source`.
- Automatic matching logic was not changed in this release.
