select ticket_id, opened_on, channel from {{ source('raw', 'tickets') }}
