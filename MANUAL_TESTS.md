# Manual tests

Nothing is marked **Passed** without a proof entry (command output, log
line, screenshot, or database check) in the Proof column.

| Priority | Test | Status | Proof |
|----------|------|--------|-------|
| P0 | Docker Compose stack starts cleanly | Passed | `docker compose ps` 2026-09-07: `backend-postgres-1` Up on `0.0.0.0:5433->5432`, `backend-redis-1` Up on `0.0.0.0:6380->6379`. Host 5432/6379 already taken by `shop-debt-*`; Nexa uses 5433/6380. |
| P0 | `alembic upgrade head` runs on a fresh DB | Not started | — |
| P1 | RAG search relevance on French test queries | Not started | — |
| P0 | Concurrent order creation on last stock unit only succeeds once | Not started | — |
| P1 | Dashboard renders on a real phone browser | Not started | — |
