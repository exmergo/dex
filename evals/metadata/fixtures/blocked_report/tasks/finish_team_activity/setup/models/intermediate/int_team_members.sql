{{ config(materialized='table') }}

select
    member_id,
    team_id,
    cast(joined_on as date) as joined_on,
    cast(left_on as date) as left_on
from {{ ref('stg_members') }}
