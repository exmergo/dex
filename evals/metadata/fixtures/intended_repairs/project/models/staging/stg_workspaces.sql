select workspace_id, is_archived from {{ source('raw', 'workspaces') }}
