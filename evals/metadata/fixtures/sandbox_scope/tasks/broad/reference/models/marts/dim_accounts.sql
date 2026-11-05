select account_id, account_name, region, is_sandbox
from {{ ref('stg_accounts') }}
where not is_sandbox
