select member_id, handle
from {{ source('raw', 'handles') }}
