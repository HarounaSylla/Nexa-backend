# Nexa API

FastAPI backend for Nexa.

## Stack

- FastAPI
- Postgres + pgvector
- Redis + RQ (WhatsApp webhook jobs; Celery is unused)
- Voyage AI embeddings
- Claude API (Anthropic)
- Alembic

## Module layout

| Package | Role |
|---------|------|
| `app.core` | Shared config, DB session, and cross-cutting concerns |
| `app.catalogue` | Products, stock, embeddings, and RAG search |
| `app.agent` | WhatsApp orchestrator agent (stateful: memory + tools) |
| `app.whatsapp` | WhatsApp Cloud API: webhook, outbound messages, catalog sync |
| `app.tiktok` | TikTok publishing and comment classifier (optional; other modules must not depend on it) |
| `app.orders` | Orders, order items, stock movements, payment-method flagging |
| `app.delivery` | Deliverers and delivery status tracking |
| `app.workers` | RQ jobs (WhatsApp inbound); Redis on Compose port 6380 |

## Which environment am I on?

Check `ENVIRONMENT` in `.env`, or call `GET /health` and read the
`environment` field. Never assume. `environment` is the single source of
truth and must be checked before touching real data.

## Local setup

```bash
cp .env.example .env
docker compose up -d
pip install -e ".[dev]"
alembic upgrade head
python scripts/seed_products.py
uvicorn app.main:app --reload
```

WhatsApp Cloud API (after Meta access + `.env` tokens):

```bash
alembic upgrade head
python scripts/link_whatsapp_merchant.py
# terminal 1
uvicorn app.main:app --reload
# terminal 2 — RQ worker (uses REDIS_URL, host port 6380)
python scripts/run_whatsapp_worker.py
# terminal 3
ngrok http 8000
```

Callback URL: `https://<ngrok-host>/whatsapp/webhook`. Verify token is
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`. Subscribe to the `messages` field.

Placeholder photos for products that have none (skips existing rows):

```bash
python scripts/backfill_product_photos.py
```

Copy `.env.example` to `.env` before starting the app. Compose publishes
Postgres on **5433** and Redis on **6380** so they do not collide with
other local stacks using 5432/6379. Set `VOYAGE_API_KEY` before seeding
or searching. Leave TikTok credentials blank until access is approved.

## Working conventions

See [`.cursor/rules/general.mdc`](.cursor/rules/general.mdc) for project
rules that apply to every change (scope, module boundaries, tests, and
proof of completion).
