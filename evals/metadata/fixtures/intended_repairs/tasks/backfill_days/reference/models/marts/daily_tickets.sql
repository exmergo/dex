with bounds as (
    select min(opened_on) as first_day, max(opened_on) as last_day
    from {{ ref('stg_tickets') }}
),

days as (
    select c.day as ticket_date
    from {{ ref('stg_calendar') }} c
    join bounds b on c.day between b.first_day and b.last_day
)

select
    d.ticket_date,
    count(t.ticket_id) as tickets
from days d
left join {{ ref('stg_tickets') }} t on t.opened_on = d.ticket_date
group by d.ticket_date
