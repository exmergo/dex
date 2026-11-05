select account_id, account_name, region, is_sandbox from {{ source('raw', 'accounts') }}
