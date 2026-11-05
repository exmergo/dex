select
    posted_on,
    account,
    count(*) as posting_count,
    sum(amount) as total_amount,
    sum(fee_amount) as total_fees
from {{ ref('stg_ledger_postings') }}
group by posted_on, account
