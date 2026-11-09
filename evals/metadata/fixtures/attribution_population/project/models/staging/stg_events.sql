select event_id, account_id, event_on from {{ source('raw', 'events') }}
