select account_id, account_name, created_on
from {{ source('raw', 'accounts') }}
