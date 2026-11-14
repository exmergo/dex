with reviews as (
    select listing_id, count(*) as review_count, avg(rating) as avg_rating
    from {{ ref('stg_reviews') }}
    group by listing_id
)
select l.listing_id, l.listing_name, r.review_count, r.avg_rating
from {{ ref('stg_listings') }} as l
inner join reviews as r on r.listing_id = l.listing_id
