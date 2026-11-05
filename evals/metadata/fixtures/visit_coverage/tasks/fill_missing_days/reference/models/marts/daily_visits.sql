with bounds as (
    select min(visited_on) as first_day, max(visited_on) as last_day
    from {{ ref('stg_visits') }}
),

days as (
    select cast(day as date) as visit_date
    from bounds, unnest(generate_series(first_day, last_day, interval 1 day)) as t(day)
)

select
    d.visit_date,
    count(v.visit_id) as visits,
    avg(v.duration_minutes) as avg_duration_minutes
from days d
left join {{ ref('stg_visits') }} v on v.visited_on = d.visit_date
group by d.visit_date
