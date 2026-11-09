with activity as (
    select subscription_id, count(*) as activity_count, max(occurred_at) as last_activity_at
    from {{ ref('stg_activity') }}
    group by subscription_id
)

select
    s.subscription_id,
    a.last_activity_at,
    coalesce(a.activity_count, 0) as activity_count
from {{ ref('stg_subscriptions') }} s
left join activity a on a.subscription_id = s.subscription_id
