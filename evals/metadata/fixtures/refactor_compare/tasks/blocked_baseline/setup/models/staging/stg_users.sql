{{ config(materialized='table') }}

select user_id, account_id, cast(signup_code as integer) as signup_code, is_test
from {{ source('raw', 'users') }}
