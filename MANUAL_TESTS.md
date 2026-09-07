# Manual tests

Nothing is marked **Passed** without a proof entry (command output, log
line, screenshot, or database check) in the Proof column.

| Priority | Test | Status | Proof |
|----------|------|--------|-------|
| P0 | Docker Compose stack starts cleanly | Passed | `docker compose ps` 2026-09-07: `backend-postgres-1` Up on `0.0.0.0:5433->5432`, `backend-redis-1` Up on `0.0.0.0:6380->6379`. Host 5432/6379 already taken by `shop-debt-*`; Nexa uses 5433/6380. |
| P0 | `alembic upgrade head` runs on a fresh DB | Passed | 2026-09-07: `Running upgrade  -> 0001_enable_pgvector` then `Running upgrade 0001_enable_pgvector -> 0002_catalogue_tables`. `psql \d products` shows `embedding vector(1024)`, indexes `ix_products_merchant_id` and `ix_products_category`, no ivfflat/hnsw. |
| P1 | RAG search relevance on French test queries | Passed | Seed 2026-09-07: `merchants=1`, `products=26`, `with_embedding=26`, `vector_dims=1024`, `merchant_id=37292228-b8f5-437d-b8e6-2ff81d4e249d`. Query pairs below. Out-of-catalogue still returns 5 nearest neighbors (no distance cutoff); names are unrelated, not construction materials. |
| P0 | Concurrent order creation on last stock unit only succeeds once | Passed | `pytest -v` 2026-09-07: `tests/test_orders.py::test_concurrent_creer_commande_on_last_unit PASSED`. Full suite `9 passed in 2.11s` (catalogue 5 + orders 4). Two concurrent `creer_commande` on `stock_qty=1`: exactly one `Order`, one `InsufficientStockError`, final `stock_qty=0`. |
| P1 | Dashboard renders on a real phone browser | Not started | — |

## RAG query / result pairs (2026-09-07, `voyage-4-lite`)

`GET /catalogue/search?merchant_id=37292228-b8f5-437d-b8e6-2ff81d4e249d&q=...`

**q = `robe rouge pour une soirée`**

1. Robe longue rouge de soirée
2. Robe longue noire de soirée
3. Ensemble deux pièces pagne wax
4. Rouge à lèvres mat bordeaux
5. Hijab en soie beige

**q = `vous avez des baskets pr homme svp`**

1. Baskets blanches homme
2. Baskets noires homme running
3. Mocassins cuir marron
4. Escarpins noirs à talon
5. Sandales plates femme

**q = `ciment 50kg pour chantier`** (not in catalogue)

1. Baskets blanches homme
2. Fond de teint teinte foncée
3. Sandales plates femme
4. Baskets noires homme running
5. Parfum musc blanc 50 ml

**`GET /catalogue/products/8717de24-9b5a-48ef-aa81-75cd023ea7cf/similar`** (Robe longue rouge de soirée)

1. Robe longue noire de soirée
2. Ensemble deux pièces pagne wax
3. Jean slim taille haute
4. Hijab en soie beige
5. T-shirt oversize blanc femme
