select
    posted_on,
    account,
    count(*) as posting_count,
    sum(amount) as total_amount
    {% if var('enable_platform_fees') %}
    , sum(fee_amount) as total_fees
    {% endif %}
from {{ ref('stg_ledger_postings') }}
group by posted_on, account
