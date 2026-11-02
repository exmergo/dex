select member_id, team_id, joined_on, left_on
from {{ source('raw', 'members') }}
