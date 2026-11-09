select user_id, account_id, signup_code, is_test
from {{ source('raw', 'users') }}
