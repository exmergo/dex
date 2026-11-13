select
    event_id,
    subscription_id,
    case lower(trim(tier_code))
        when 'trial' then 'trial'
        when 'paid' then 'paid'
        else lower(trim(tier_code))
    end as event_tier,
    occurred_on
from {{ source('raw', 'billing_events') }}
