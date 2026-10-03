# Iris multi-image shop

A local full-stack deployment example with three application images and two infrastructure images. It includes concrete deployment declarations and a real asynchronous workflow.

| Service | Source | Runtime | Internal port | Purpose |
| --- | --- | --- | --- | --- |
| web | `web/` | nginx | 80 | Vite-built storefront and `/api` reverse proxy |
| api | `api/` | Node.js + Express | 3000 | Products, demo users, order submission and status |
| worker | `worker/` | Node.js + BullMQ | 3001 (health only) | Processes order jobs and updates PostgreSQL |
| postgres | official PostgreSQL image | PostgreSQL 16 | 5432 | Users, products, orders, order items, durable job outbox |
| redis | official Redis image | Redis 7 | 6379 | BullMQ transport with append-only persistence |

## Run locally

Docker with Compose is required to run the complete stack. From the repository root:

```sh
cp .env.example .env
docker compose config
docker compose up --build -d
docker compose ps
```

Open `http://localhost:8088`. The API is also available at `http://localhost:3000/api/products`. Both published ports bind to loopback. PostgreSQL, Redis and worker health remain on the internal Compose network.

Submit an order through the storefront. Its status should move from `pending` to `completed`, with stock decreasing once. Use `docker compose logs -f api worker` to inspect job processing. The seed user is `11111111-1111-4111-8111-111111111111`.

## How the request moves

1. Vite reads `VITE_API_BASE_URL=/api` while building the web image. nginx serves the compiled assets and proxies `/api/` to `http://api:3000` while preserving the path.
2. Express reads `DATABASE_URL`, `REDIS_URL`, `PORT` and `ORDER_QUEUE_NAME` at runtime. It stores a pending order, its items, and a durable job record in one PostgreSQL transaction.
3. Express asks BullMQ to enqueue the job. If Redis is temporarily unavailable, the durable database job remains available for the worker's outbox scan.
4. The worker reads its runtime settings, recovers undispatched database jobs, and consumes Redis jobs. It locks the order, validates inventory, decreases stock, and marks the order and job completed in one database transaction. Retried or duplicate jobs see a terminal order and do not decrease stock twice.
5. The browser polls the order endpoint until it receives a terminal state.

See `docs/architecture.md` for dependencies and `docs/api.md` for the exact HTTP routes. PostgreSQL bootstrap SQL is in `db/schema.sql`; foreign keys are explicit. `db/seed.sql` supplies a demo user and products.

## Development and checks

Install each application's dependencies with `npm ci` in its directory. Each `npm run check` performs JavaScript syntax checks; `web` additionally supports `npm run build`. API route-validation tests use `npm test` and do not require Docker.

```sh
cd api
npm ci
npm test
npm run check
```

## Limits of this example

This is a local deployment fixture, not a production commerce system. It uses a fixed demo user without authentication and has no payment provider. Order processing simulates fulfillment. Prices are integer cents and checkout quantities are bounded. Configuration includes obvious local-only credentials. There is no production HTTPS, autoscaling, backup policy, or migration runner.

Initialization SQL runs only when PostgreSQL starts with an empty data volume. To reset the demo data, `docker compose down -v` removes both named volumes, including orders and queue data. Changing `.env` does not change the credentials inside an already initialized PostgreSQL volume. A frontend API-base change requires rebuilding `web`; API and worker configuration is runtime configuration.
