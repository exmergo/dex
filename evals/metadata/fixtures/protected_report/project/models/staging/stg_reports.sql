select report_id, comments from {{ source('raw', 'reports') }}
