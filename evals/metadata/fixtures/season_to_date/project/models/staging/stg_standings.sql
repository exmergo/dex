select season, week, team_id, wins_to_date, losses_to_date
from {{ source('raw', 'standings') }}
