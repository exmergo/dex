select membership_id, workspace_id, user_id
from {{ ref('stg_memberships') }}
where workspace_id not in (
    select workspace_id from {{ ref('stg_workspaces') }} where is_archived
)
