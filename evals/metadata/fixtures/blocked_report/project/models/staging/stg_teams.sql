select team_id, team_name
from {{ source('raw', 'teams') }}
