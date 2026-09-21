# What's left to do

Living list of remaining work. Check items off as they land; do not treat
unchecked items as optional.

## Jalon 0 — Project scaffold

- [x] Repo structure created

## Jalon 1 — Catalogue & RAG

- [x] products / product_images schema with pgvector embedding column
- [x] Voyage AI integration (`voyage-4-lite`, 1024-d, live `VOYAGE_API_KEY`)
- [x] Sample dataset (Boutique Awa, 26 products, all embedded)
- [x] `rechercher_produits`
- [x] `trouver_produits_similaires`
- [x] Manual relevance validation
- [x] `lister_categories` (in-stock categories for the agent menu)
- [x] `lister_produits_populaires` (top 10 by distinct non-cancelled orders, price DESC tie-break / cold start)

## Jalon 2 — Orders, stock, delivery

- [x] `deliverers` / `orders` / `order_items` / `stock_movements` schema and Postgres enums
- [x] Alembic `0003_orders_stock_delivery`
- [x] Alembic `0005_delivery_zones` (`delivery_zones` + `orders.city`)
- [x] `obtenir_disponibilite`
- [x] `creer_commande` with `SELECT ... FOR UPDATE` (sorted product ids, one transaction)
- [x] `assigner_livreur`
- [x] `confirmer_livraison` (finalize marker, `quantity_delta=0`)
- [x] `annuler_commande` restocks under the same locking discipline
- [x] Temporary `/orders` router for manual testing
- [x] Concurrent last-unit order test against local Postgres
- [x] `delivery_zones` (per-city availability + delay window) and `orders.city`
- [x] `creer_commande` rejects unserved cities (`DeliveryNotAvailableError`) before stock lock
- [x] Agent tool `verifier_zone_livraison` + prompt rules for city / deliverer-calls-on-arrival
- [x] Temporary `/orders/delivery-zones` router; Boutique Awa seed: Dakar 24-48h, Thiès 48-72h, Touba unavailable

## Jalon 3 (part 1 — agent core, no webhook yet)

The WhatsApp agent uses OpenAI (`gpt-5.6-terra`), not Claude — pragmatic
2026-09-07 decision because an OpenAI key was already available. The
TikTok comment classifier (Jalon 7) remains a separate model decision.

- [x] `openai` + `langgraph` dependencies; `openai_api_key` / `agent_model` settings
- [x] RAG `rag_max_distance=0.50` (measured on Jalon 1 queries)
- [x] `conversations` / `messages` schema (Alembic `0004_agent_conversations`)
- [x] 8 socle tools + `execute_tool` dispatcher (domain errors as JSON): `rechercher_produits`, `lister_categories`, `lister_produits_populaires`, `trouver_produits_similaires`, `obtenir_disponibilite`, `verifier_zone_livraison`, `creer_commande`, `escalader_vers_humain`
- [x] System prompt + LangGraph ReAct loop (`traiter_message_entrant`)
- [x] Temporary `POST /agent/simulate` and message history endpoint
- [x] Deterministic unit tests (no live OpenAI)
- [x] Agent tool JSON never exposes exact `stock_qty` (`stock_status` only)
- [x] Merchant-uploaded product photos in agent tool JSON + `/agent/simulate` `images` (dogfooding point 5 closed; URL never pasted into chat text)
- [ ] WhatsApp Cloud API webhook (`app/whatsapp`) — blocked on access approval
- [ ] WhatsApp media send / voice — still Jalon 3 part 2 (structured `images` is the input that step will consume)

Jalon 3 part 2+ items get added only once that work actually starts.

## Jalon 4 — Dashboard

- [x] Clerk session JWT verification (JWKS); `merchants.clerk_user_id`; onboarding + `/merchants/me` + temp `link-demo`
- [x] Merchant catalogue management (authenticated): product CRUD, photo upload to `/static` + `product_images` upsert, dashboard category list (no stock filter), category rename. No new `categories` table.
- [x] Merchant order management (authenticated): list/detail, deliverers list+create, assign/confirm/cancel scoped to the caller, payment-link on `online` orders only (`payment_status` stays `pending`/`paid` — no new enum). Temp `POST /orders`, availability, and delivery-zones routes left in place.
- [x] Merchant conversation handoff (authenticated): list (escalated first), thread, human reply (`turn_role=merchant`, no agent loop), return-to-agent (409 if not escalated). Temp `POST /agent/simulate` and `GET /agent/conversations/{id}/messages` left in place; dashboard lives on `/conversations`.
- [x] Merchant notifications (authenticated): `notifications` table (Alembic `0008`), emitted on successful `creer_commande` (`new_order`; `product_out_of_stock` only when that path zeroes stock) and `escalader_vers_humain` (`conversation_escalated`). Raw `data` JSON, no French copy. `GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/read-all`. Catalogue stock edits do not notify.
