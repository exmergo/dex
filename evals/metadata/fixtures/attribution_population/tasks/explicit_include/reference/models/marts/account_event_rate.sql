select
    a.account_id,
    count(e.event_id) as event_count,
    count(e.event_id) / 30.0 as events_per_day
from {{ ref('stg_accounts') }} a
left join {{ ref('stg_events') }} e on a.account_id = e.account_id
group by a.account_id
