select membership_id, workspace_id, user_id
from {{ ref('stg_memberships') }}
