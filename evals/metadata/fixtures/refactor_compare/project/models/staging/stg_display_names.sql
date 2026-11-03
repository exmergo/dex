select user_id, display_name
from {{ source('raw', 'display_names') }}
