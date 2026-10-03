# Database bootstrap

`schema.sql` creates five tables in one transaction. Orders belong to users; items link orders and products; each durable job belongs to exactly one order. `seed.sql` inserts a fixed demo user and three products. UUIDs for new orders and jobs are generated in the API with Node's `crypto.randomUUID`.

Both SQL files are mounted into PostgreSQL's initialization directory by Compose and run only against an empty data volume. They are independent of the web/api/worker build contexts. The database container supplies its standard healthcheck with `pg_isready`.
