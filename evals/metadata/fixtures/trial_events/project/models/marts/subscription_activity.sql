with events as (
    select
        subscription_id,
        count(*) as event_count,
        count(*) filter (where event_tier = 'trial') as trial_event_count,
        count(*) filter (where event_tier = 'paid') as paid_event_count
    from {{ ref('stg_events') }}
    group by subscription_id
)

select
    s.subscription_id,
    s.plan_name,
    s.started_on,
    s.cancelled_on,
    coalesce(e.event_count, 0) as event_count,
    coalesce(e.trial_event_count, 0) as trial_event_count,
    coalesce(e.paid_event_count, 0) as paid_event_count
from {{ ref('stg_subscriptions') }} s
left join events e on e.subscription_id = s.subscription_id
where not s.is_internal
