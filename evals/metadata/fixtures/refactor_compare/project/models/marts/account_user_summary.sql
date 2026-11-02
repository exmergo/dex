select
    a.account_id,
    a.account_name,
    coalesce(u.user_count, 0) as user_count,
    coalesce(n.named_users, 0) as named_users
from {{ ref('stg_accounts') }} as a
left join (
    select account_id, {{ count_users('user_id') }} as user_count
    from {{ ref('stg_users') }}
    group by account_id
) as u on u.account_id = a.account_id
left join (
    select us.account_id, count(*) as named_users
    from {{ ref('stg_users') }} as us
    inner join {{ ref('stg_display_names') }} as d on d.user_id = us.user_id
    group by us.account_id
) as n on n.account_id = a.account_id
