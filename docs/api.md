# HTTP interface

All business routes are JSON under `/api`; health routes are unprefixed.

| Method | Path | Response / use |
| --- | --- | --- |
| GET | `/health` | process liveness |
| GET | `/ready` | PostgreSQL + Redis readiness |
| GET | `/api/users` | active demo users |
| GET | `/api/products` | active products with inventory and integer-cent prices |
| GET | `/api/products/:id` | one product |
| GET | `/api/orders` | most recent orders, optionally `?userId=<uuid>` |
| GET | `/api/orders/:id` | order plus items and durable job status |
| POST | `/api/orders` | create pending order and queue processing |
| GET | `/api/jobs/:id` | one durable background-job record |

POST `/api/orders` accepts:

```json
{"userId":"11111111-1111-4111-8111-111111111111","items":[{"productId":"22222222-2222-4222-8222-222222222221","quantity":1}]}
```

It returns HTTP 202 with `id`, `status`, `totalCents`, `queueAccepted`, and `jobId`. Inventory is checked at submission and checked again under a lock by the worker. An intervening order can exhaust stock; the later order then becomes `rejected`. Validation failures use HTTP 400; missing entities use HTTP 404. The worker's `GET /health` responds on internal port 3001.
