select usage_id, account_id, used_on, units from {{ source('raw', 'usage') }}
