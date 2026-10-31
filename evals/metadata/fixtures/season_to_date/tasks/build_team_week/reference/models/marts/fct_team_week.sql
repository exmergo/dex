select
    s.season,
    s.week,
    s.team_id,
    t.team_name,
    s.wins_to_date,
    s.losses_to_date
from {{ ref('stg_standings') }} as s
left join {{ ref('stg_teams') }} as t using (team_id)
