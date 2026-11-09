select
    s.season,
    s.team_id,
    t.team_name,
    sum(s.wins_to_date) as wins,
    sum(s.losses_to_date) as losses
from {{ ref('stg_standings') }} as s
join {{ ref('stg_teams') }} as t using (team_id)
group by 1, 2, 3
