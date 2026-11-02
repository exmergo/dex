select
    a.account_id,
    a.account_name,
    coalesce(i.user_count, 0) as user_count,
    coalesce(i.named_users, 0) as named_users
from {{ ref('stg_accounts') }} as a
left join {{ ref('int_account_users') }} as i on i.account_id = a.account_id
