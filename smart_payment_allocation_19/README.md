# Smart Payment Allocation for Odoo 19

> Ported from the Odoo 18.0.1.10.2 codebase. Odoo 19 keeps the `<list>` tag
> and view_mode syntax introduced in 18, so no view-tag changes were needed.
> No code differences were applied beyond the manifest version bump, since
> nothing in this module touches the accounting/inventory internal API
> signatures or ORM method names that changed in 19. Please test against a
> real Odoo 19 instance before deploying to confirm the core `account`
> module's `view_account_payment_tree`/`res_config_settings_view_form` anchor
> IDs used by this module's inherited views haven't been renamed.


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
