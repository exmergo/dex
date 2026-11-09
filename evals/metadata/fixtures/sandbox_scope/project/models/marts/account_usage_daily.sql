select account_id, used_on, sum(units) as units, count(*) as usage_records
from {{ ref('stg_usage') }}
group by account_id, used_on
