select visit_id, visited_on, duration_minutes from {{ source('raw', 'visits') }}
