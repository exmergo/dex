**`transform plan --scaffold` finds an object whatever its case, and names the model in lower case.**
On an upper-casing warehouse such as Snowflake, `--scaffold orders` now finds the cached
`DB.RAW.ORDERS`, as `explore profile` already did, and both `orders` and `ORDERS` scaffold
`models/staging/stg_orders.sql` reading `source(..., 'ORDERS')`, where it wrote `stg_ORDERS`.
