select day from {{ source('raw', 'calendar') }}
