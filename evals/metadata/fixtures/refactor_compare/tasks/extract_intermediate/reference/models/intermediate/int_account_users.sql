select
    us.account_id,
    {{ count_users('us.user_id') }} as user_count,
    count(d.user_id) as named_users
from {{ ref('stg_users') }} as us
left join {{ ref('stg_display_names') }} as d on d.user_id = us.user_id
group by us.account_id
