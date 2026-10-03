# Architecture and deployment boundaries

The three application images are built from separate contexts: `web`, `api`, and `worker`. Each Dockerfile has a `runtime` target; the web also has a builder stage. These stages do not represent additional deployed applications.

The web does not access the database or Redis. It makes browser requests to `/api` on the same origin. nginx resolves the Compose service hostname `api` and forwards HTTP to port 3000. The API writes business records to PostgreSQL and sends only an order ID plus durable database-job ID to BullMQ. The worker accesses both PostgreSQL and Redis, but has no public business endpoint. Its port 3001 exposes dependency readiness only.

PostgreSQL is the source of truth for both order state and job state. `jobs` is a transactional outbox: order and job records commit together before the queue is contacted. A worker timer retries undispatched jobs after queue outages. BullMQ handles retries, while row locks and terminal-state checks make successful processing idempotent. Inventory rows are locked in product-ID order to avoid conflicting checkout lock order.

`postgres_data` persists PostgreSQL data and `redis_data` persists Redis AOF files. `db/schema.sql` and `db/seed.sql` are read-only initialization mounts, not runtime application services. All services use the default isolated Compose network. Only nginx and the API publish host ports, both on localhost.

After a job exhausts its five processing attempts or BullMQ marks it terminally failed after repeated stalls, the failed-event handler marks the pending order and durable job rejected in one transaction. It locks the order first and preserves already completed orders. Attempts are recorded outside the inventory transaction so a rolled-back processing attempt remains visible. If PostgreSQL stays unavailable through every retry and the failure handler also cannot reach it, manual queue recovery is required; the fixture does not include a dead-letter reconciliation scheduler.

Both application processes have graceful shutdown handlers. API readiness checks PostgreSQL and Redis; worker readiness checks the same dependencies. The nginx health route confirms the HTTP server can serve requests but does not independently verify API readiness; Compose waits for API health before starting it.
