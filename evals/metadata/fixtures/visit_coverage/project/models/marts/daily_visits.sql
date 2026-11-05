select
    visited_on as visit_date,
    count(*) as visits,
    avg(duration_minutes) as avg_duration_minutes
from {{ ref('stg_visits') }}
group by visited_on
