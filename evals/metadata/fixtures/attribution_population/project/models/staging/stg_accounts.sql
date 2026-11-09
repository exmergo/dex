select account_id, account_name from {{ source('raw', 'accounts') }}
