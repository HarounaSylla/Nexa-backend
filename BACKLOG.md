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
- [x] Image embeddings (`voyage-multimodal-3.5`, 1024-d, `products.image_embedding`, Alembic `0021`) + `trouver_produits_par_image` / `classify_image_match`. Step 2 wires this into WhatsApp inbound photos and one agent turn (`traiter_photo_produit`).

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
- [x] 10 socle tools + `execute_tool` dispatcher (domain errors as JSON): `rechercher_produits`, `lister_categories`, `lister_produits_populaires`, `trouver_produits_similaires`, `obtenir_disponibilite`, `verifier_zone_livraison`, `creer_commande`, `escalader_vers_humain`, `consulter_commande`, `analyser_photo_client` (on-demand, latest inbound photo of this conversation, 24 h window)
- [x] System prompt + LangGraph ReAct loop (`traiter_message_entrant`)
- [x] Temporary `POST /agent/simulate` and message history endpoint
- [x] Deterministic unit tests (no live OpenAI)
- [x] Agent tool JSON never exposes exact `stock_qty` (`stock_status` only)
- [x] Merchant-uploaded product photos in agent tool JSON + `/agent/simulate` `images` (dogfooding point 5 closed; URL never pasted into chat text)
- [x] WhatsApp Cloud API webhook (`GET`/`POST /whatsapp/webhook`) — RQ job on Redis, worker calls `traiter_message_entrant` once then Graph API send. Merchant routed by `merchants.whatsapp_phone_number_id` (Alembic `0009`). `POST /agent/simulate` unchanged.
- [x] WhatsApp outbound product photos: worker uploads local files via Graph `/media` then sends `type=image` with `media_id` (not a public `link`). Cap 3 per turn. `extract_product_images` lives in `app/agent/images.py` (simulate unchanged).
- [x] Close conversations on delivery (`confirmer_livraison`) and 48h inactivity (lazy inbound + RQ `close_stale_conversations` on `maintenance` every 15 min). Sweep preserves `updated_at` so the 15-min reopen grace does not revive a swept thread.
- [x] WhatsApp inbound images (payment proofs **and** product photos) — stored privately. Vision runs when the daily cap is not reached, except on an escalated thread with no order awaiting proof. Voice / PDF still skipped.
- [x] Agent sees human-handover and inbound-photo turns as replayable text markers (`app/agent/handover.py`); a captioned `not_analyzed` photo keeps `(non analysée)` on the marker. A developer note is injected at replay when a `[Boutique]` reply follows the last native agent turn (a bare « oui » / « ok » / « d'accord » does not name a product). Tool outputs carry `photo_status` (`will_be_sent` / `already_sent_earlier` / `none`) using the same plan as the WhatsApp worker. Prompt rules 14–18. No schema change.

- [x] `rechercher_produits` resolves a guessed `categorie` against stored names (case / accents / trivial singular-plural) then exact-equality filter; if that search is empty it retries once without a category (`rag_max_distance` still applies).

Jalon 3 part 2+ items get added only once that work actually starts.

## Agent context — remaining risks (audit 2026-10-06)

These were listed in `Nexa/proofs/audit-agent-context.md` and are **not** in this change (conversation lifetime, vision, and embeddings stay untouched).

- The 48 h `active` conversation is shared across calendar days, so last night's order, product UUIDs, and `sent_product_images` stay in play the next afternoon.
- `sent_product_images` is unique on `(conversation_id, product_id)` for the whole conversation lifetime — a product photo is never re-sent even after a new intent.
- Inbound WhatsApp audio / document types are not handled (`app/whatsapp`); those messages never enter this pipeline.
- `obtenir_dernier_message_agent` is the latest agent turn for this **merchant + phone**, not strictly this conversation — wrong-thread risk if two conversations ever exist for the same phone.
- No history truncation; replayed `input_list` grows for the whole conversation.
- Return the merchant's real category names in the `rechercher_produits` tool result when a guessed `categorie` is dropped, so the model can retry with an exact value.
- Merchant-side category normalisation in the dashboard (typos / accents / casing when creating or renaming a category).

## Agent cart — remaining

The multi-article flow (rule 17) is prompt-led: the cart is whatever
the model infers from replayed history. Older assistant turns that
jumped from a **bare** quantity to the address are rewritten at replay
(`rewrite_outdated_quantity_skips`) so the model does not imitate them;
a matching turn is left alone when the customer message just before it
already said that is all, or already gave address/city details. Stored
rows are unchanged. `creer_commande` already accepts several `items`
and aggregates duplicate product ids.

- Explicit cart state (confirmed product + quantity per conversation)
  instead of relying on history replay for earlier `product_id`s
- Order total in the agent reply from a tool result (`creer_commande`
  currently returns lines and unit prices, not a total; rule 2 forbids
  the model from computing one)
- Customer wants to modify or remove an article before the order is
  created (today: only "add another" / "that's all")
- Ask quantity per article in one sentence when several items are
  confirmed together ("je prends 2 robes et 1 sac")

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
- PDF/document proofs and image questions from customers (non-proof inbound photos now leave a text marker for the agent; a `product_photo` classification also runs catalogue search + one agent turn that must confirm before ordering)
- Notification when a customer sends a photo before any link was sent (currently stored and visible in the thread only)
- Merchant "attach this photo to an order" action for ambiguous cases
- [x] Inbound image file retention (Alembic `0019`, 90 days after reception once unpaid/pending proofs are done; row kept, file deleted)
- Retention counted from `paid_at` once that column exists
- Delete conversations/messages retention policy
- Delete-merchant data purge
- 24 h template for acknowledgements
- Real-phone test of the inbound proof flow (blocked on the WhatsApp token/webhook)
- Customer acknowledgement when a message arrives on an escalated conversation (merchant is notified; agent stays silent; no auto-reply today). A customer who writes again while waiting (e.g. « Etes vous toujours fermé ? ») currently gets nothing — consider one automatic acknowledgement (« Votre message est bien transmis ») without reopening the agent.
- Auto-return / auto-close policy for unanswered escalations
- `other` while an order awaits a proof: still stored, no reply, no notification (backlog question unchanged)
- At the vision/recognition cap (`max_image_analyses_per_phone_per_day`, default 10): today `not_analyzed` and **no customer reply**. Decide whether to tell the customer.
- Several product photos in one image (classifier + search + verifier assume a single main subject)
- Crop-to-product before embedding/verify (today the whole frame, including TikTok chrome, is embedded)
- [x] Phase 2 burst batching: rapid inbound texts/photos wait a quiet window then share ONE agent turn and ONE reply (`traiter_rafale_entrante`). Settings `WHATSAPP_BATCH_QUIET_SECONDS` (default 3.0; 0 disables) and `WHATSAPP_BATCH_MAX_WAIT_SECONDS` (default 10.0). Worker must run with the scheduler (`uv run python scripts/run_whatsapp_worker.py` → `SimpleWorker.work(with_scheduler=True)`). Payment-proof ACK stays immediate. `/agent/simulate` stays unbatched.
- Run exactly one WhatsApp worker (ingest ordering with several workers)
- Per-message quoting of replies to a burst (future, optional)
- Voice notes and stickers handling
- Several photos sent **during an escalation**: `analyser_photo_client` only looks at the latest inbound image of the conversation (last 24 h, file still present). Older unanalysed photos stay `not_analyzed`.
- `analyser_photo_client` 24 h window: a photo older than 24 h is treated as `no_photo`. Decide whether to extend, or to tell the customer the photo expired.
- Merchant notification wording for a visual-search `error` level (`product_photo_unrecognized` today uses the same copy as a catalogue miss). The on-demand tool never emits that notification; the live path still does.
- Passing the customer image itself into the agent model (multimodal turn) as an alternative to the classifier + Voyage + verifier + `analyser_photo_client` tool. Today the model never sees pixels.
- Caption-only intent (text "vous avez cette robe?" without a photo) is unchanged text search — not visual search
- Frontend: show "Produit reconnu : …" on a `product_photo` in the conversation thread (`thread-panel.tsx` currently returns null). Backend `GET /conversations/{id}/messages` now sends `match_level`, `matched_product_id`, `matched_product_name`, `match_kind`.
- Frontend: show the quoted message ("en réponse à") from `quoted` on `GET /conversations/{id}/messages` (`QuotedMessageOut`: kind / excerpt / product_name / from_earlier_conversation / message_id). Backend already sends it.
- Frontend: map `product_photo_unrecognized` in `notification-copy.ts` (unknown types render as the raw `item.type` string, not `data.title`). Not in step 2b.
- Tune fallback `image_match_*` cutoffs from real `inbound_images.match_candidates` (still used when verification is off or fails). Retrieval cutoff is 0.65; decision is the vision verifier.

## Preferences — later

- Agent tone (tu/vous, greeting, emoji level)
- WhatsApp alert to the merchant on escalation / new order
- Price negotiation (max discount)
- Agent active hours
- Manual order validation
- Structured per-zone delivery fee
- Multi-country readiness: per-merchant currency code/symbol instead of the hardcoded "F"/"FCFA" in `app/agent/images.py` and the frontend formatters; merchant country / default phone prefix; UI and agent language beyond French
- Multi-country phone defaults beyond `DEFAULT_COUNTRY_CALLING_CODE=221` (per-merchant calling code, non-Senegalese local forms)

## Image search (step 2b: shortlist + vision verify)

Catalogue image embeddings (`voyage-multimodal-3.5`) only **shortlist**
(`image_match_retrieval_distance=0.65`, `image_match_shortlist_size=4`).
A second vision call (`verify_image_against_candidates`, default
`agent_model`) decides `same` / `similar` / `none`. The agent proposes
from a **code-owned** set and must get a customer "oui" before ordering.
No new HTTP route. Fallback thresholds never emit `strong`.

- Confirm Voyage **and OpenAI** data retention / privacy **before the pilot**:
  a product photo now hits classifier + query embedding + verifier (customer
  image plus up to 4 downscaled catalogue photos). Voyage hosted API stores
  inputs for training unless the org Admin opts out (zero-day retention).
  OpenAI: confirm `store=False` on both vision calls is enough for the org's
  retention policy. https://docs.voyageai.com/docs/faq
- HNSW (or ivfflat) index on `products.image_embedding` when catalogues grow
  past a sequential scan (same backlog as text `products.embedding` in 0002)
- A second photo per product (today: one file `{product_id}.{ext}`) to
  improve recall on awkward crops/angles
- Multimodal text-to-image search (query text against image vectors — different
  from today's image-to-image and from text RAG)
- Cost per product photo with verification (step 2b toy set, gpt-5.6-terra
  at $2 / $12 per 1M from https://developers.openai.com/api/docs/models/gpt-5.6-terra):
  verifier **~$0.006–0.012** per call (≈2.6k–5.6k input + ~100 output tokens),
  plus the classifier call and Voyage multimodal ($0.60 / billion pixels;
  50k-pixel floor, 2M-pixel ceiling). Free-tier Voyage **3 RPM** still
  stretches latency (compare script: 40.3s / 39.9s / 56.5s on three rows).
- Latency budget: classifier ~2–3 s, Voyage ~1–2 s when not rate-limited,
  verifier ~2–5 s (pipeline red-dress: 3.12 / 1.22 / 1.79 s), then the
  agent turn. Several products in one image and crop-to-product are not done.

