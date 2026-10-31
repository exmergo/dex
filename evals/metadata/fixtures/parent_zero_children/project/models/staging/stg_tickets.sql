select ticket_id, account_id, opened_on, status
from {{ source('raw', 'tickets') }}
