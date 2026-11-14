select membership_id, workspace_id, user_id from {{ source('raw', 'memberships') }}
