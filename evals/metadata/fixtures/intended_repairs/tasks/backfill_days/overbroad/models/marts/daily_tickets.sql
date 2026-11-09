-- Over-broad: every day crossed with every channel, zero-filled. The day repeats.
with bounds as (
    select min(opened_on) as first_day, max(opened_on) as last_day
    from {{ ref('stg_tickets') }}
),

days as (
    select c.day as ticket_date
    from {{ ref('stg_calendar') }} c
    join bounds b on c.day between b.first_day and b.last_day
),

channels as (select distinct channel from {{ ref('stg_tickets') }})

select
    d.ticket_date,
    count(t.ticket_id) as tickets
from days d
cross join channels ch
left join {{ ref('stg_tickets') }} t
    on t.opened_on = d.ticket_date and t.channel = ch.channel
group by d.ticket_date, ch.channel
