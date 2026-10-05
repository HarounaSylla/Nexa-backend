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
- [x] Per-merchant `order_number` (Alembic `0010`, merchant row `SELECT ... FOR UPDATE`) + optional `orders.conversation_id` (nullable; conversation closes on delivery, not on confirm)
- [x] Agent tool `consulter_commande` (own orders only, by number or most recent)
- [x] Agent tool `verifier_zone_livraison` + prompt rules for city / deliverer-calls-on-arrival
- [x] Temporary `/orders/delivery-zones` router; Boutique Awa seed: Dakar 24-48h, Thiès 48-72h, Touba unavailable

## Jalon 3 (part 1 — agent core, no webhook yet)

The WhatsApp agent uses OpenAI (`gpt-5.6-terra`), not Claude — pragmatic
2026-09-07 decision because an OpenAI key was already available. The
TikTok comment classifier (Jalon 7) remains a separate model decision.

- [x] `openai` + `langgraph` dependencies; `openai_api_key` / `agent_model` settings
- [x] RAG `rag_max_distance=0.50` (measured on Jalon 1 queries)
- [x] `conversations` / `messages` schema (Alembic `0004_agent_conversations`)
- [x] 9 socle tools + `execute_tool` dispatcher (domain errors as JSON): `rechercher_produits`, `lister_categories`, `lister_produits_populaires`, `trouver_produits_similaires`, `obtenir_disponibilite`, `verifier_zone_livraison`, `creer_commande`, `escalader_vers_humain`, `consulter_commande`
- [x] System prompt + LangGraph ReAct loop (`traiter_message_entrant`)
- [x] Temporary `POST /agent/simulate` and message history endpoint
- [x] Deterministic unit tests (no live OpenAI)
- [x] Agent tool JSON never exposes exact `stock_qty` (`stock_status` only)
- [x] Merchant-uploaded product photos in agent tool JSON + `/agent/simulate` `images` (dogfooding point 5 closed; URL never pasted into chat text)
- [x] WhatsApp Cloud API webhook (`GET`/`POST /whatsapp/webhook`) — RQ job on Redis, worker calls `traiter_message_entrant` once then Graph API send. Merchant routed by `merchants.whatsapp_phone_number_id` (Alembic `0009`). `POST /agent/simulate` unchanged.
- [x] WhatsApp outbound product photos: worker uploads local files via Graph `/media` then sends `type=image` with `media_id` (not a public `link`). Cap 3 per turn. `extract_product_images` lives in `app/agent/images.py` (simulate unchanged).
- [x] Close conversations on delivery (`confirmer_livraison`) and 48h inactivity (lazy inbound + RQ `close_stale_conversations` on `maintenance` every 15 min). Sweep preserves `updated_at` so the 15-min reopen grace does not revive a swept thread.
- [x] WhatsApp inbound images (payment proofs) — stored privately, vision only when an order is awaiting a proof. Voice / PDF still skipped.

Jalon 3 part 2+ items get added only once that work actually starts.

## Jalon 4 — Dashboard

- [x] Clerk session JWT verification (JWKS); `merchants.clerk_user_id`; onboarding + `/merchants/me` + temp `link-demo`
- [x] Merchant catalogue management (authenticated): product CRUD, photo upload to `/static` + `product_images` upsert, dashboard category list (no stock filter), category rename. No new `categories` table.
- [x] Merchant order management (authenticated): list/detail, deliverers list+create+edit, unique canonical phone per merchant (Alembic `0020_deliverer_unique_phone`), assign/confirm/cancel scoped to the caller, payment-link on `online` orders only (`payment_status` stays `pending`/`paid` — no new enum). `POST /orders` is authenticated (merchant from the session; no `merchant_id` in the body). Delivery-zone routes are authenticated (same session merchant); availability stays unauthenticated.
- Deactivate/delete a deliverer (the `active` column exists but is unused)
- Manage deliverers from Paramètres
- [x] Order `payment_status` is real: cash-on-delivery becomes `paid` when delivery is confirmed; online is marked `paid` by the merchant via `POST /orders/{id}/mark-paid` (idempotent, 409 on COD or cancelled). Cancel does not change `payment_status`. Alembic `0013_backfill_cod_paid` backfills delivered COD rows.
- [x] Merchant preferences (Alembic `0012_merchant_prefs`): payment methods, timezone, shop address/hours/return policy/fee note/extra info. `GET`/`PUT /merchants/me/preferences`. Agent prompt + `creer_commande` tool lock. Dashboard `POST /orders` is not restricted by preferences.
- [x] Merchant conversation handoff (authenticated): list (escalated first), thread, human reply (`turn_role=merchant`, no agent loop), return-to-agent (409 if not escalated). Temp `POST /agent/simulate` and `GET /agent/conversations/{id}/messages` left in place; dashboard lives on `/conversations`.
- [x] Merchant replies and payment links reach the customer on WhatsApp (`envoyer_message_commercant`). `POST /conversations/{id}/reply` sends then stores (502 + no row on send failure). `POST /orders/{id}/send-payment-link` replaces `PATCH .../payment-link`: save link, send French message, set `payment_link_sent_at` (Alembic `0014`). `/agent/simulate` conversations cannot receive replies (fake phones).
- [x] Merchant notifications (authenticated): `notifications` table (Alembic `0008`), emitted on successful `creer_commande` (`new_order`; `product_out_of_stock` only when that path zeroes stock) and `escalader_vers_humain` (`conversation_escalated`). Raw `data` JSON, no French copy. `GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/read-all`. Catalogue stock edits do not notify.

## Payments — later

- `paid_at` timestamp when an order becomes paid
- Refund handling when a paid order is cancelled
- Automatic online payment confirmation via a provider webhook
- Un-marking a payment marked by mistake
- [x] Inbound payment-proof photos: WhatsApp images stored privately, vision only when an online order is awaiting a proof (`payment_link_sent_at` set). Status becomes `proof_received` (never `paid`). Alembic `0015_payment_proofs`. `POST /orders/{id}/reject-proof`. Conversation ↔ order links on the merchant APIs.
- [x] Reusable payment links in Préférences; `POST /orders/{id}/send-payment-link` takes a configured `payment_link_id` (Alembic `0016_merchant_payment_links`). Order stores a URL + label snapshot.
- Per-order custom link / amount-prefilled links
- Payment-link ordering
- Deactivating a payment link without deleting it
- WhatsApp message templates for sending after the 24 h window
- A "Renvoyer" retry queue for failed merchant sends
- PDF/document proofs and image questions from customers (non-proof images get no agent reply today)
- Notification when a customer sends a photo before any link was sent (currently stored and visible in the thread only)
- Merchant "attach this photo to an order" action for ambiguous cases
- [x] Inbound image file retention (Alembic `0019`, 90 days after reception once unpaid/pending proofs are done; row kept, file deleted)
- Retention counted from `paid_at` once that column exists
- Delete conversations/messages retention policy
- Delete-merchant data purge
- 24 h template for acknowledgements
- Real-phone test of the inbound proof flow (blocked on the WhatsApp token/webhook)
- Customer acknowledgement when a message arrives on an escalated conversation (merchant is notified; agent stays silent; no auto-reply today)
- Auto-return / auto-close policy for unanswered escalations

## Preferences — later

- Agent tone (tu/vous, greeting, emoji level)
- WhatsApp alert to the merchant on escalation / new order
- Price negotiation (max discount)
- Agent active hours
- Manual order validation
- Structured per-zone delivery fee
- Multi-country readiness: per-merchant currency code/symbol instead of the hardcoded "F"/"FCFA" in `app/agent/images.py` and the frontend formatters; merchant country / default phone prefix; UI and agent language beyond French
- Multi-country phone defaults beyond `DEFAULT_COUNTRY_CALLING_CODE=221` (per-merchant calling code, non-Senegalese local forms)
