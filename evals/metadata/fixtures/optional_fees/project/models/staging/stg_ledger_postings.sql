select
    posting_id,
    lower(account) as account,
    posted_on,
    amount,
    fee_amount
from {{ source('raw', 'ledger_postings') }}
