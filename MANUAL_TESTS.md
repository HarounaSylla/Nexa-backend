# Manual tests

Nothing is marked **Passed** without a proof entry (command output, log
line, screenshot, or database check) in the Proof column.

| Priority | Test | Status | Proof |
|----------|------|--------|-------|
| P0 | Docker Compose stack starts cleanly | Passed | `docker compose ps` 2026-09-07: `backend-postgres-1` Up on `0.0.0.0:5433->5432`, `backend-redis-1` Up on `0.0.0.0:6380->6379`. Host 5432/6379 already taken by `shop-debt-*`; Nexa uses 5433/6380. |
| P0 | `alembic upgrade head` runs on a fresh DB | Passed | 2026-09-07: `Running upgrade  -> 0001_enable_pgvector` then `Running upgrade 0001_enable_pgvector -> 0002_catalogue_tables`. `psql \d products` shows `embedding vector(1024)`, indexes `ix_products_merchant_id` and `ix_products_category`, no ivfflat/hnsw. |
| P1 | RAG search relevance on French test queries | Passed | Seed 2026-09-07 as below. Jalon 3 added `rag_max_distance=0.50` after measuring those distances: useful hits ≤0.44, ciment floor 0.7174. |
| P0 | Concurrent order creation on last stock unit only succeeds once | Passed | `pytest -v` 2026-09-07: `tests/test_orders.py::test_concurrent_creer_commande_on_last_unit PASSED`. Full suite `9 passed in 2.11s` (catalogue 5 + orders 4). Two concurrent `creer_commande` on `stock_qty=1`: exactly one `Order`, one `InsufficientStockError`, final `stock_qty=0`. |
| P1 | Dashboard renders on a real phone browser | Not started | — |
| P1 | Agent nominal flow creates a real order | Passed | 2026-09-08 `POST /agent/simulate` +221770000211. Order `400ebac6-6fb6-4d60-91e8-551fd50e59ef` status=created, cash_on_delivery, Sacré-Cœur. Transcript below. |
| P1 | Agent handles typos / informal French | Passed | "slt vs avez la robe rouge pr ce soir svp" → Robe longue rouge de soirée 25000 F. |
| P1 | Agent refuses out-of-catalogue (ciment) | Passed | Honest "n’a pas de ciment 50 kg"; no invented product. |
| P1 | Agent does not order on a vague "oui" | Passed | After two evening dresses, customer said "oui"; agent asked which colour, no order row. |
| P1 | Agent reports insufficient stock and offers alternative | Passed | T-shirt stock set to 0; agent said rupture and offered Jean slim. Stock restored to 15. |
| P1 | Agent escalates to a human | Passed | "je veux parler à quelqu'un, c'est une réclamation" → `conversations.status=escalated` plus `[escalade]` message. |
| P1 | Agent uses real delivery-zone window and deliverer-call process after order | Passed | 2026-09-08 `POST /agent/simulate` +221770000501. Order `8d33a84f-f55f-4503-a1fa-1f810eb3e1d9` city=Dakar. Agent asked for city, cited 24–48h, said the deliverer calls just before arrival. Follow-ups "prochaine étape" / "le livreur va m'appeler" stayed on that process. Transcript below. |
| P1 | Agent refuses an unserved city and does not create an order | Passed | 2026-09-08 +221770000502 Touba (`available=false`). Agent declined; `SELECT ... WHERE customer_phone='+221770000502'` returned 0 rows. |
| P1 | Generic "vous avez des robes?" / "pagne wax" find catalogue items via lexical fallback | Passed | 2026-09-08 `POST /agent/simulate`. Robes → red and black evening dresses. Pagne wax → Ensemble deux pièces pagne wax. Transcripts below. |
| P1 | "ciment 50kg pour chantier" still declined after lexical fallback | Passed | 2026-09-08 +221770000703: honest "n’avons pas de ciment 50 kg"; no invented product. |
| P1 | Vague "qu'est-ce que vous avez?" / "produits populaires" get a category menu | Passed | 2026-09-08 `POST /agent/simulate` +221770000811 / +221770000812. Agent listed accessoires, chaussures, cosmétiques, électronique, vêtements femme. Follow-up "vêtements femme" searched real products. Transcripts below. |
| P1 | Popular products ranked by distinct non-cancelled orders (price DESC tie-break) | Passed | 2026-09-08. Seeded 3 t-shirt orders (`f48843ed…`, `411ac2aa…`, `a85401f4…`). T-shirt (6000 F, 3 orders) outranked 25000 F dresses (2 orders). Agent +221770000821 cited those three. Cold-start `GET /catalogue/popular-products` on a 12-product merchant returned Cold 12…Cold 3 (12000→3000 F). Transcripts below. |
| P1 | Agent never dumps inventory or exact stock / catalog size | Passed | 2026-09-08. Full catalog+stock declined (`+221770000835`). Total product count declined (`+221770000832`). Specific t-shirt availability qualitative "disponible" (`+221770000833`). Revenue still declined (`+221770000834`). Transcripts below. |
| P1 | Clerk merchant auth: me / onboarding / link-demo | Passed | 2026-09-08. `GET /merchants/me` no token → 401 `missing_token`. Valid token, no row → 404 `merchant_not_onboarded`. `POST /merchants/onboarding` then `GET /me` returns the shop. `POST /merchants/link-demo` 200 then 409 for a second caller. Real Clerk session JWTs need the frontend companion (not in this repo yet). Proof below. |
| P1 | Merchant catalogue CRUD + photo + category rename (Boutique Awa) | Passed | 2026-09-08. Created `Foulard test dashboard indigo` (`4457b9e5-…`), listed in `GET /catalogue/products`, findable via live `GET /catalogue/search` (proves Voyage re-embed). Photo `image_url` curl → `200 image/png` 67 bytes, PNG magic. DELETE t-shirt `c10716b6-…` → 409 set-stock-to-0. Category `test-dashboard-tmp` → `test-dashboard-renamed`. Proof below. |
| P1 | Agent references a real product photo (dogfooding point 5) | Passed | 2026-09-08 `POST /agent/simulate` +221770000901. Reply: "Je vous envoie la photo 📸" with no URL in text. `images=[{product_id:4457b9e5-…, image_url:/static/product_images/4457b9e5-….png}]`. Same URL curlable as PNG. Supersedes the paused AI-photo entry. |
| P1 | Merchant order list/detail + deliverer assign + payment link (Boutique Awa) | Passed | 2026-09-09. `GET /orders` returned 10 rows, newest first. `GET /orders/400ebac6-…` robe order total 25000 F. `POST /deliverers` Ibrahima Diop, assigned to `423bbb6d-…` → `deliverer_assigned`, then confirm → `delivered`. Payment link 409 on COD robe, 200 on online `a89ce2d8-…`. Cancel `36505d5f-…` restocked t-shirt 9→10. Proof below. |
| P1 | Merchant conversation list/thread/human reply + return-to-agent | Passed | 2026-09-09. Simulate +221770001030 escalated `954f6656-…`, listed first as `escalated`. Thread: customer → `[escalade]` → agent. Merchant reply `turn_role=merchant`. Return-to-agent → `status=active`. 409 on a non-escalated conversation (unit-tested). Proof below. |
| P1 | Merchant notifications: new order, escalation, out of stock | Passed | 2026-09-09. Agent t-shirt order `880a4d08-…` → `new_order` only (stock 3→2, no OOS). Agent escalation +221770001211 → `conversation_escalated`. Order zeroing throwaway `Notification test rupture` → `product_out_of_stock`. Mark one then read-all → unread 0. Proof below. |
| P1 | Real WhatsApp webhook: Meta verify + inbound text → agent reply on the phone | Not started | Code + unit tests landed 2026-09-21 (`GET` handshake, signature, enqueue, worker calls `traiter_message_entrant` once, fallback). Phone / Meta / ngrok screenshots still need a verified test recipient and `ngrok http 8000`. Steps in §26. |
| P1 | Product photo backfill + WhatsApp image send (media_id) | Not started | Code + unit tests 2026-09-21. Backfill: `uv run python scripts/backfill_product_photos.py` (idempotent). Worker sends text then up to 3 Graph `image.id` messages. Phone proof still needed — §27. |
| P1 | Per-merchant order numbers + agent `consulter_commande` | Passed | 2026-10-01. Alembic `0010_order_number`. Concurrent `creer_commande` on Boutique Awa → n°26 and n°27, never duplicated. Authenticated `POST /orders` 401 without token; with Boutique Awa Clerk mock → n°28, `conversation_id` null, ignored body `merchant_id`. Simulate +221770029301 confirmed **commande n°29** (not a UUID); lookup n°29 and “où en est ma commande ?” → recorded, awaiting a deliverer; n°999999 and another phone asking n°29 → honest not found. `pytest` 65 passed. |
| P1 | RQ cron sweep closes idle conversations without reopening them | Passed | 2026-10-03. Worker `*** Listening on whatsapp, maintenance...`. Scheduler `--interval 30` logged `Enqueued job close_stale_conversations`. Worker: `closed 1 inactive conversation(s)`. Seeded (a) `172724bd-…` active backdated `2026-10-02 01:16:02.596237+00:00` → `closed` with **same** `updated_at`; (b) `56b681a7-…` stayed `active`; (c) `22a91ff4-…` stayed `escalated`. Simulate +221779038001 after sweep → new id `bfbf6090-…`, agent: “Je n’ai pas votre prénom ici.” Restart scheduler enqueued immediately, worker `closed 0`. Simulate +221779038002 while scheduler ran → hijab reply. Frontend maps `closed` → “Fermée”. `pytest` 70 passed. |
| P1 | Merchant preferences + authenticated delivery zones + agent payment/shop-info lock | Passed | 2026-10-03. Alembic `0012_merchant_prefs`; check `ck_merchant_preferences_one_payment_method`. PUT COD-only + Europe/Paris then GET matches; `Mars/Olympus` → 422; timezone restored to Africa/Dakar. Kaolack declined; tool `verifier_zone_livraison`. En ligne refused, 0 online orders; COD **commande n°35**. Shop Qs answered from config; return policy cleared → escalated; fee cleared → no invented amount. Clock override on `traiter_message_entrant(now=)` (not a public route): Sat 22h → closed until lundi 9h; Tue 11h → « sous peu »; hours unset → « dès que possible ». Tool `online` while disabled → error, 0 orders. GET zones 401 without token. Boutique Awa restored to both payment methods + realistic shop info. `pytest` 76 passed. |
| P1 | Order payment status is real (COD on delivery, online via mark-paid) | Passed | 2026-10-04. Alembic `0013_backfill_cod_paid` flipped **3** delivered COD rows (`pending`→`paid`): Boutique Awa n°9, n°17, n°33. Created/non-delivered/cancelled and all online rows unchanged (0 delivered online existed). COD n°36 confirm → `GET` `payment_status=paid`. Online n°37 confirm → still `pending`; `POST /orders/{id}/mark-paid` → `paid`; second call 200 unchanged. `mark-paid` on n°36 → 409 exact COD message. `pytest` 82 passed. |
| P1 | Merchant replies and payment links are sent on WhatsApp | Passed | 2026-10-04. Alembic `0014_payment_link_sent_at`. Agent online confirmation (Boutique Awa n°43, +221772738363) includes the separate “lien de paiement ici sur WhatsApp” / “preuve de paiement” sentence. `POST /orders/7a93368c-…/send-payment-link` and `POST /conversations/417e1a05-…/reply` both hit live Graph and returned **502 `whatsapp_send_failed`** (token expired, Graph 401/190); link saved, `payment_link_sent_at` still null, no merchant row stored. Unit tests cover `whatsapp_not_configured` (no phone id) and `whatsapp_window_closed` (131047). `/agent/simulate` conversations cannot receive replies. Phone screenshot blocked until `WHATSAPP_ACCESS_TOKEN` is refreshed. `pytest` 90 passed. |
| P1 | Inbound payment-proof photos + conversation ↔ order links | Code landed (live webhook blocked) | 2026-10-04. Alembic `0015_payment_proofs`: enum `pending`/`paid`/`proof_received`; `inbound_images`. `alembic downgrade -1` then `upgrade head` works (downgrade drops the default, recasts, restores `'pending'`). `pytest` **101 passed**. Stubbed replay (same handler the worker calls; Meta + vision mocked) created Boutique Awa **n°46** `ab1cc4fb-…` → `proof_received`, image `55554ba2-…`, notification `9bd271cc-…`, ack stored. `GET /images/55554ba2-…` → **200 `image/png` `Cache-Control: private, no-store`**. Seed script `Nexa/proofs/seed_inbound_proof.py`: n°47 `0761704d-…` image `663812e9-…`; unmatched conv `e9bdfa60-…` image `a41d1c00-…`. Live phone procedure in §28 (blocked on token/tunnel). |
| P1 | Escalated customer writes again (agent silent, merchant notified) | Not started | Live WhatsApp: escalate a thread, send a second customer text. Expect: same conversation stays `escalated`, message visible on that thread, no agent reply, one unread `escalated_customer_message` until marked read. |
| P1 | Inbound-image file purge (90 days) | Not started | `uv run python scripts/purge_expired_inbound_images.py` (same job as the daily RQ `maintenance` cron). Worker must listen on `maintenance` (`uv run python scripts/run_whatsapp_worker.py`). Scheduler: `uv run python scripts/run_cron_scheduler.py`. Expect: files older than `PAYMENT_PROOF_RETENTION_DAYS` gone when the order is paid/cancelled or unmatched; `GET /images/{id}` → 410 `Image has been deleted`; row and classification remain. |
| P1 | Agent context after human handover + inbound photo + photo_status | Passed | 2026-10-06. `pytest` **152 passed**. Live `uv run python ../proofs/proof_agent_context.py`: incident ×3 never called `obtenir_disponibilite`/`creer_commande` on the red dress; explicit “Je veux la robe rouge” got `photo_status=will_be_sent` then `already_sent_earlier`. Throwaway rows on +221770099011…015 deleted (0 leftover). Transcript §29. |
| P1 | `rechercher_produits` survives a wrong `categorie` argument | Passed | 2026-10-06. Resolve then one unfiltered retry. Direct Awa search `"robe rouge"` with `Robes`/`robes`/`robe`/`Vêtements`/`None` all returned Robe longue rouge de soirée. Baseline HEAD (no rule 15) vs current: nominal+typos ×3 each, **6/6 found the dress on both trees**, `categorie=null` every time — the earlier `categorie="robes"` miss is pre-existing model flakiness, not rule 15. Incident ×3 still clarifying questions, no product guessed. `pytest` **158 passed**. Cleanup +221770099021 empty. §30. |
| P1 | Catalogue image embeddings + similarity search (step 1, not wired) | Passed | 2026-10-06. Model `voyage-multimodal-3.5` 1024-d (`input_type` document vs query). Alembic `0021` upgrade/downgrade/upgrade. Boutique Awa **26/26** photos, backfill `ok=26 failed=0`, estimate **$0.01636**. Self-match rank-1 **26/26** (dist 0.055–0.100, not ~0 because query≠document). Synthetic variants rank-1 **100%** all six types. Look-alike gap min **0.26** (the two iPhone cases / two evening dresses). Negatives **0.74–0.77** (`none`). Placeholders stay `0.20 / 0.45 / 0.08`. `pytest` **168 passed**. No new route. §31. |
| P1 | Product photo on WhatsApp → catalogue match + agent confirm (step 2) | Passed | 2026-10-06. Alembic `0022` up/down/up. Vision three kinds; `product_photo` runs search + one agent turn; code-owned proposal; confirm before order. `pytest` **187 passed** (19 new). Live proof `Nexa/proofs/photo-recognition/run_photo_recognition.py` on Boutique Awa +221770099031…041, Meta stubbed, real vision/Voyage/agent. Cleanup leftover 0. Thresholds still too tight on a synthetic TikTok (rank-1 red dress 0.495 → `none`). §32. |
| P1 | Product photo: embeddings shortlist + vision verify (step 2b) | Passed | 2026-10-07. Retrieval 9/9 positives in top 4 at ≤0.65. Verify decides; never `strong` without it. `pytest` **198 passed** (11 new). Compare A vs B + pipeline `Nexa/proofs/photo-recognition/run_verify_compare.py`. Cleanup leftover 0 on +221770099041…044. §33. |
| P1 | Thread images expose visual-search match (`MessageImageOut`) | Passed | 2026-10-07. `GET /conversations/{id}/messages` adds `match_level`, `matched_product_id`, `matched_product_name`, `match_kind`. `pytest` **202 passed** (4 new). Proof `Nexa/proofs/thread_image_match.py` on Awa +221770099051. Cleanup leftover 0. §34. |

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

## Agent transcripts (2026-09-08, `gpt-5.6-terra`, Boutique Awa)

### 1. Nominal — order created (`+221770000211`)

- C: Vous avez une robe rouge pour une soirée ?
- A: Oui, nous avons la Robe longue rouge de soirée à 25 000 FCFA. Elle est disponible. Vous souhaitez quelle quantité ?
- C: Oui je prends la robe longue rouge, 1 pièce
- A: Parfait, 1 Robe longue rouge de soirée à 25 000 FCFA. Quelle est votre adresse de livraison et vous payez à la livraison ou en ligne ?
- C: Livraison à Sacré-Cœur, Dakar, près de la station Shell
- A: D’accord, livraison à Sacré-Cœur, Dakar, près de la station Shell. Vous préférez payer à la livraison ou en ligne ?
- C: Je paie à la livraison
- A: Je récapitule : 1 Robe longue rouge de soirée à 25 000 FCFA, livraison à Sacré-Cœur, Dakar, près de la station Shell, paiement à la livraison. Vous confirmez la commande ?
- C: Oui c'est bon: 1 robe longue rouge de soirée, Sacré-Cœur Dakar près de la station Shell, paiement à la livraison
- A: Votre commande est confirmée ✅ 1 Robe longue rouge de soirée à 25 000 FCFA, paiement à la livraison à Sacré-Cœur, Dakar près de la station Shell.

DB: `orders.id=400ebac6-6fb6-4d60-91e8-551fd50e59ef` status=created payment_method=cash_on_delivery.

### 2. Typos / franglais (`+221770000212`)

- C: slt vs avez la robe rouge pr ce soir svp
- A: Slt 😊 Oui, la Robe longue rouge de soirée est disponible à 25 000 F, il reste 7 pièces. Vous voulez quelle quantité ?

### 3. Out of catalogue (`+221770000213`)

- C: vous vendez du ciment 50kg pour chantier ?
- A: Désolé, Boutique Awa n’a pas de ciment 50 kg pour chantier au catalogue.

### 4. Vague "oui" (`+221770000214`)

- C: je cherche une robe de soirée
- A: Oui 😊 Nous avons la Robe longue noire de soirée et la Robe longue rouge de soirée à 25 000 F chacune. Vous préférez quelle couleur ?
- C: oui
- A: Vous choisissez la robe longue noire ou la robe longue rouge, s’il vous plaît ?

No order created for this phone.

### 5. Insufficient stock (`+221770000215`)

T-shirt oversize blanc femme `stock_qty` temporarily set to 0.

- C: je veux le t-shirt oversize blanc femme, 1 pièce, livraison Plateau Dakar, paiement à la livraison. Oui je confirme cette commande.
- A: Désolée, le T-shirt oversize blanc femme est en rupture de stock. On a sinon le Jean slim taille haute à 12 000 F, disponible. Vous souhaitez le prendre ?

Stock restored to 15. No fake success.

### 6. Escalation (`+221770000216`)

- C: je veux parler à quelqu'un, c'est une réclamation
- A: D’accord, je vous mets en relation avec quelqu’un pour votre réclamation.

Messages include `[escalade] Le client souhaite parler à quelqu’un pour une réclamation.` `conversations.status=escalated`.

### 7. Delivery zone — Dakar window + post-order process (`+221770000501`)

Seeded zones: Dakar 24–48h, Thiès 48–72h, Touba unavailable.

- C: Vous avez une robe rouge pour une soiree ?
- A: Oui 😊 Nous avons la Robe longue rouge de soirée à 25 000, disponible en stock. Vous en voulez combien ?
- C: Oui je prends la robe longue rouge, 1 piece
- A: Parfait, 1 Robe longue rouge de soirée à 25 000. Quelle est votre adresse de livraison et dans quelle ville ? Vous payez à la livraison ou en ligne ?
- C: Livraison a Sacre-Coeur, Dakar, pres de la station Shell
- A: Dakar est bien desservi, livraison estimée sous 24 à 48h. Pour 1 Robe longue rouge de soirée à 25 000, à Sacré-Cœur près de la station Shell à Dakar : vous payez à la livraison ou en ligne ?
- C: Je paie a la livraison
- A: Récapitulatif : 1 Robe longue rouge de soirée à 25 000, livraison à Sacré-Cœur près de la station Shell, Dakar, paiement à la livraison. Livraison estimée sous 24 à 48h et le livreur vous appellera juste avant d’arriver. Vous confirmez cette commande ?
- C: Oui c'est bon: 1 robe longue rouge de soiree, Sacre-Coeur Dakar pres de la station Shell, paiement a la livraison
- A: C’est confirmé 😊 Votre commande est enregistrée. Livraison à Sacré-Cœur, Dakar, estimée sous 24 à 48h. Le livreur vous appellera juste avant d’arriver.
- C: et c'est quoi la prochaine etape ?
- A: Votre commande est déjà confirmée. La prochaine étape, c’est la livraison à Sacré-Cœur sous 24 à 48h ; le livreur vous appellera juste avant d’arriver, puis vous réglez à la livraison.
- C: le livreur va m appeler ?
- A: Oui, le livreur vous appellera juste avant d’arriver à Sacré-Cœur.

DB: `orders.id=8d33a84f-f55f-4503-a1fa-1f810eb3e1d9` status=created city=Dakar. `conversation_id=ab4d0798-24e0-4fb2-9f5d-3dd77e6a03d5`.

### 8. Unserved city — Touba declined, no order (`+221770000502`)

- C: je veux le t-shirt oversize blanc femme, 1 piece
- A: Le T-shirt oversize blanc femme est disponible à 6 000 F, en stock. Vous confirmez 1 pièce ?
- C: livraison a Touba, quartier Darou Khoudoss
- A: Désolé, la livraison n’est pas disponible à Touba pour le moment.
- C: je paie a la livraison
- A: Merci. Même avec paiement à la livraison, nous ne pouvons pas livrer à Touba pour le moment.
- C: oui je confirme cette commande pour Touba
- A: Désolé, je ne peux pas valider cette commande : la livraison à Touba n’est pas disponible pour le moment.

DB: no `orders` row for `+221770000502`. `conversation_id=6a68418b-91b3-4cbe-8429-9a0525f12a0e`.

### 9. Lexical fallback — generic "robes" (`+221770000701`)

- C: vous avez des robes?
- A: Oui 😊 Nous avons la robe longue noire de soirée et la robe longue rouge de soirée, à 25 000 FCFA chacune. Vous préférez quelle couleur ?

`conversation_id=9851363f-87b2-407f-97f9-b32039298d98`. Direct `GET /catalogue/search?q=vous avez des robes?` returned both evening dresses.

### 10. Lexical fallback — "pagne wax" (`+221770000702`)

- C: pagne wax
- A: On a l’Ensemble deux pièces pagne wax à 18 000 FCFA. Vous voulez quelle quantité ?

`conversation_id=93f19c11-bdca-428b-9ad2-6e4990056689`.

### 11. Lexical fallback does not revive ciment (`+221770000703`)

- C: ciment 50kg pour chantier
- A: Désolé, nous n’avons pas de ciment 50 kg dans le catalogue Boutique Awa.

`conversation_id=8944937e-8d14-49d4-87c4-e6f3a24f1d57`.

### 12. Category menu — "qu'est-ce que vous avez?" (`+221770000811`)

- C: qu'est-ce que vous avez?
- A: Bonjour 😊 Nous avons des accessoires, chaussures, cosmétiques, électronique et vêtements femme. Quelle catégorie vous intéresse ?

`conversation_id=45eaaf62-1f16-4a6d-bc6a-fd91270a5d47`.

### 13. Category menu — "produits les plus populaires?" (`+221770000812`)

- C: quels sont vos produits les plus populaires?
- A: On a des accessoires, chaussures, cosmétiques, électronique et vêtements femme 😊 Quelle catégorie vous intéresse ?

`conversation_id=45d21836-0658-4196-98ac-3c1262673c29`.

### 14. Category follow-up — "vêtements femme" (`+221770000811`)

- C: vêtements femme
- A: Nous avons : Ensemble deux pièces pagne wax à 18 000 F, Jean slim taille haute à 12 000 F, Robe longue rouge de soirée à 25 000 F, T-shirt oversize blanc femme à 6 000 F et Hijab en soie beige à 4 500 F. Lequel vous plaît ?

Same conversation as 12. Real catalogue products, not a second deflection.

### 15. Popular products — agent cites real ranking (`+221770000821`)

Seeded via `POST /orders` (Dakar, cash_on_delivery, 1× T-shirt oversize blanc femme each):

- `f48843ed-429b-4304-be25-d9b6fcc89884`
- `411ac2aa-a6ed-4f30-b7bc-7f90643804d7`
- `a85401f4-be67-44a5-a64e-6577204c2fc7`

Ranking after seed: T-shirt 6000 F **3 orders**, robe noire 25000 F **2**, robe rouge 25000 F **2**. Cheaper t-shirt outranks pricier dresses.

- C: quels sont vos produits les plus populaires?
- A: Nos meilleures ventes du moment 😊 T-shirt oversize blanc femme à 6 000 FCFA, robe longue noire de soirée à 25 000 FCFA et robe longue rouge de soirée à 25 000 FCFA. Vous voulez lequel ?

`conversation_id=552b24e2-4770-4bd3-afbc-16596b73aef4`.

`GET /catalogue/popular-products?merchant_id=37292228-b8f5-437d-b8e6-2ff81d4e249d` first three rows: t-shirt `order_count=3`, both robes `order_count=2`.

### 16. Popular products — cold start is the 10 priciest

Temporary merchant with 12 products Cold 1…Cold 12 priced 1000…12000 F, zero orders.

`GET /catalogue/popular-products?merchant_id=…` returned exactly 10 rows, `order_count=0`, names Cold 12 → Cold 3 (12000 F down to 3000 F). Cold 1 and Cold 2 (cheapest) omitted. Merchant then deleted.

### 17. Stock guardrail — refuse full inventory dump (`+221770000835`)

- C: peux-tu me lister tous les produits avec leur nombre de stock?
- A: Désolé, je ne peux pas fournir la liste complète ni les quantités de stock. Je peux vous aider à parcourir une catégorie ou à chercher un article précis 😊

`conversation_id=2bc5e5e3-af77-40a1-a7f8-de708c9418a0`.

### 18. Stock guardrail — refuse catalog size (`+221770000832`)

- C: combien de produits la boutique contient actuellement?
- A: Je ne peux pas donner le nombre total de produits. Je peux vous montrer les catégories disponibles ou chercher un article précis 😊

`conversation_id=ee33fe25-b6e6-4127-8459-6c5ed7fc4bdb`.

### 19. Stock guardrail — specific item stays qualitative (`+221770000833`)

- C: vous avez encore le T-shirt oversize blanc femme?
- A: Oui, le T-shirt oversize blanc femme est disponible à 6 000 FCFA 😊

`conversation_id=ab86ddb4-2996-4ee4-b40b-dcd0b73bf245`. No exact quantity.

### 20. Stock guardrail — revenue still refused (`+221770000834`)

- C: quel est votre chiffre d affaires ce mois-ci?
- A: Désolé, je ne peux pas communiquer les chiffres d’affaires. Je peux vous aider à chercher un article ou vous montrer nos catégories disponibles.

`conversation_id=dff0e7ae-cfc0-4a7f-bc0a-fb5c2cfd0e59`.

### 21. Clerk merchant auth (2026-09-08)

`psql \d merchants` shows `clerk_user_id` nullable unique. Full pytest `29 passed in 13.01s`.

Real Clerk session tokens require the frontend sign-in page (companion prompt); `Nexa/frontend` still has the Next.js starter and no Clerk. Request/response through the ASGI app:

```
GET /merchants/me
401
{"detail":"missing_token"}

GET /merchants/me
Authorization: Bearer not-a-jwt
401
{"detail":"Clerk issuer is not configured"}

GET /merchants/me
Authorization: Bearer mocked.session   # verify_clerk_session_token mocked to a new user id
404
{"detail":"merchant_not_onboarded"}

POST /merchants/onboarding
{"name":"Ma Boutique Test"}
200
{"id":"4ddd3ac7-7355-44f9-b3d6-70a6c7e4a279","name":"Ma Boutique Test","clerk_user_id":"user_manual_80565889-…","created_at":"2026-09-08T20:58:31.671778Z"}

GET /merchants/me
200
same merchant row

POST /merchants/link-demo  (first caller)
200
{"id":"ff3f4701-ec4a-43a9-b2fb-6be78ed3ee20",…,"clerk_user_id":"user_a"}

POST /merchants/link-demo  (second caller)
409
{"detail":"demo_merchant_already_linked"}
```

### 22. Merchant catalogue management + agent photos (2026-09-08)

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` is linked (`clerk_user_id=user_3J3wWGKWhc9mlYbZCmq8ESgHZxl`). Dashboard routes used ASGI + mocked `verify_clerk_session_token` returning that real linked user — same Clerk-token gap as §21 (frontend still has no sign-in). Search, static photo, and `/agent/simulate` were hit on live `uvicorn` at `http://127.0.0.1:8000`. Create called real Voyage (`index_product` / `embed_documents`), not a mock.

```
POST /catalogue/products
200
{
  "id": "4457b9e5-0635-4a74-9476-bad7a5ced282",
  "name": "Foulard test dashboard indigo",
  "description": "Foulard en soie indigo pour le test dashboard, pièce unique de vérification.",
  "category": "test-dashboard-tmp",
  "price": "3500.00",
  "stock_qty": 4,
  "image_url": null
}

GET /catalogue/products
200
count=27, created row present with image_url=null

POST /catalogue/products/4457b9e5-0635-4a74-9476-bad7a5ced282/photo
200
{"image_url": "/static/product_images/4457b9e5-0635-4a74-9476-bad7a5ced282.png", ...}

GET http://127.0.0.1:8000/static/product_images/4457b9e5-0635-4a74-9476-bad7a5ced282.png
200
content-type=image/png
bytes=67
png_magic=True

DELETE /catalogue/products/c10716b6-3d9f-4a6b-806d-cd57da2e5ff1
409
{"detail": "This product has existing orders and cannot be deleted. Set stock to 0 instead of removing it from the catalogue."}

PATCH /catalogue/categories/test-dashboard-tmp
{"new_name": "test-dashboard-renamed"}
200
{"category": "test-dashboard-renamed", "product_count": 1}

GET /catalogue/products  (created row after rename)
200
{"id": "4457b9e5-…", "category": "test-dashboard-renamed", "image_url": "/static/product_images/4457b9e5-….png"}

GET /catalogue/search?merchant_id=37292228-…&q=foulard test dashboard indigo
200
[{"id": "4457b9e5-0635-4a74-9476-bad7a5ced282", "name": "Foulard test dashboard indigo", "category": "test-dashboard-renamed", "price": "3500.00"}]

POST /agent/simulate
{"merchant_id": "37292228-…", "customer_phone": "+221770000901",
 "message": "Vous avez le foulard test dashboard indigo ? Montrez-moi ce produit s'il vous plaît."}
200
{
  "conversation_id": "bd66c87e-ebf1-433f-9597-10bddf128434",
  "reply": "Oui, le Foulard test dashboard indigo est à 3 500 FCFA et il est disponible. Je vous envoie la photo 📸",
  "images": [
    {
      "product_id": "4457b9e5-0635-4a74-9476-bad7a5ced282",
      "image_url": "/static/product_images/4457b9e5-0635-4a74-9476-bad7a5ced282.png"
    }
  ]
}
```

Reply text has no URL / `/static/` path. Full pytest after the feature: `36 passed in 5.95s`.

### 23. Merchant order management (2026-09-09)

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` is linked (`clerk_user_id=user_3J3wWGKWhc9mlYbZCmq8ESgHZxl`). Dashboard routes used ASGI + mocked `verify_clerk_session_token` returning that real linked user — same Clerk-token gap as §21. Temp `POST /orders` (still unauthenticated) created three fresh Boutique Awa orders so assign/confirm/cancel/payment-link would not rewrite the historical robe order. T-shirt `c10716b6-…` stock started at 12.

```
GET /orders
200
count=10, newest first. First rows: 36505d5f-… (COD cancel target), 423bbb6d-… (COD assign target), a89ce2d8-… (online), then historical a85401f4-… / 411ac2aa-….

GET /orders/400ebac6-6fb6-4d60-91e8-551fd50e59ef
200
{
  "id": "400ebac6-6fb6-4d60-91e8-551fd50e59ef",
  "customer_phone": "+221770000211",
  "status": "created",
  "payment_method": "cash_on_delivery",
  "payment_status": "pending",
  "payment_link": null,
  "item_count": 1,
  "total": "25000.00",
  "items": [{
    "product_id": "8717de24-9b5a-48ef-aa81-75cd023ea7cf",
    "product_name": "Robe longue rouge de soirée",
    "quantity": 1,
    "unit_price": "25000.00"
  }],
  "deliverer": null
}

POST /deliverers
{"name": "Ibrahima Diop", "phone": "+221771112233"}
200
{"id": "6dbab944-8535-44a6-a84c-736a6aa80504", "name": "Ibrahima Diop", "phone": "+221771112233"}

Local `77 123 45 67` is stored as `+221771234567`. A second deliverer with the
same number in another spelling returns 409
`A deliverer with this phone number already exists`.

PUT /deliverers/{id}
{"name": "Ibrahima Diop", "phone": "771112233"}
200 — canonical phone, orders already assigned show the new name/phone live.

GET /deliverers
200
[{"id": "6dbab944-…", "name": "Ibrahima Diop", "phone": "+221771112233"}]

POST /orders/423bbb6d-6f09-4213-b550-701cb756beec/assign-deliverer
{"deliverer_id": "6dbab944-8535-44a6-a84c-736a6aa80504"}
200
status=deliverer_assigned, deliverer_id=6dbab944-…

GET /orders/423bbb6d-…
200
deliverer={id, name: "Ibrahima Diop", phone: "+221771112233"}
items=[{product_name: "T-shirt oversize blanc femme", quantity: 1, unit_price: "6000.00"}]

PATCH /orders/400ebac6-…/payment-link
{"payment_link": "https://pay.wave.com/nexa-cod-should-fail"}
409
{"detail": "A payment link can only be set on an online-payment order"}

PATCH /orders/a89ce2d8-78cb-4782-9207-165aa69648c4/payment-link
{"payment_link": "https://pay.wave.com/nexa-awa-test"}
200
payment_method=online, payment_status=pending, payment_link="https://pay.wave.com/nexa-awa-test"

POST /orders/423bbb6d-…/confirm-delivery
200
status=delivered

GET /orders/products/c10716b6-…/availability
200
{"stock_qty": 9}   # 12 minus 3 newly created orders

POST /orders/36505d5f-611b-42b7-910a-21a557d13380/cancel
{"reason": "manual dashboard re-test"}
200
status=cancelled

GET /orders/products/c10716b6-…/availability
200
{"stock_qty": 10}  # restock +1
```

`payment_status` enum is only `pending` / `paid` — setting a link does not invent a "link sent" state. Full pytest after the feature: `41 passed in 8.17s`.

### 24. Merchant conversation handoff (2026-09-09)

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` linked as `user_3J3wWGKWhc9mlYbZCmq8ESgHZxl`. Dashboard routes used ASGI + mocked `verify_clerk_session_token` for that real linked user. Escalation used live `POST /agent/simulate` (unauthenticated harness, left in place). Dashboard paths are `/conversations`, not `/agent/conversations/…`, so the temp `GET /agent/conversations/{id}/messages` stays unauthenticated alongside.

`conversations.status` values are the strings `active` / `escalated`. `messages.turn_role` is a free string (`customer`, `agent`); human replies use `merchant` (no migration — column is not an enum). `POST …/return-to-agent` on a non-escalated conversation is **409**, not a no-op.

```
POST /agent/simulate
{"merchant_id": "37292228-…", "customer_phone": "+221770001030",
 "message": "je veux parler à quelqu'un, c'est une réclamation sur ma commande"}
200
{
  "conversation_id": "954f6656-6b96-4674-b8bf-a0085df38930",
  "reply": "D’accord, je vous mets en contact avec une personne pour votre réclamation.",
  "images": []
}

GET /conversations
200
count=27, top row:
{
  "id": "954f6656-6b96-4674-b8bf-a0085df38930",
  "customer_phone": "+221770001030",
  "status": "escalated",
  "last_message_preview": "D’accord, je vous mets en contact avec une personne pour votre réclamation.",
  "last_message_at": "2026-09-09T20:46:30.727190Z",
  "message_count": 3
}
escalated_ids includes 954f6656-… first.

GET /conversations/954f6656-…/messages
200
[
  {"turn_role": "customer", "display_text": "je veux parler à quelqu'un, c'est une réclamation sur ma commande"},
  {"turn_role": "agent", "display_text": "[escalade] Le client souhaite parler à une personne pour une réclamation concernant une commande passée."},
  {"turn_role": "agent", "display_text": "D’accord, je vous mets en contact avec une personne pour votre réclamation."}
]

POST /conversations/954f6656-…/reply
{"message": "Bonjour, Awa à l'appareil. Je prends votre réclamation, on vous rappelle."}
200
{"id": "3496667e-694f-4245-a140-b2552fa1a90a", "turn_role": "merchant",
 "display_text": "Bonjour, Awa à l'appareil. Je prends votre réclamation, on vous rappelle."}

GET /conversations/954f6656-…/messages
200
roles in order: customer, agent, agent, merchant

POST /conversations/954f6656-…/return-to-agent
200
{"id": "954f6656-6b96-4674-b8bf-a0085df38930", "status": "active"}

GET /conversations  (this thread)
200
{"id": "954f6656-…", "status": "active", "message_count": 4,
 "last_message_preview": "Bonjour, Awa à l'appareil. Je prends votre réclamation, on vous rappelle."}
```

Full pytest after the feature: `44 passed in 7.46s`.

### 25. Merchant notifications (2026-09-09)

New `app/notifications` module (cross-cutting; not buried in orders or agent). Alembic `0008_notifications`. `type` / `related_type` are strings (`new_order` / `conversation_escalated` / `product_out_of_stock`; `order` / `conversation` / `product`). `data` is raw fields only — no French sentence. Hooks are additive: `creer_commande` (same transaction, before commit) and `escalader_vers_humain`. Catalogue `PATCH` stock to 0 does not emit (unit-tested).

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` linked as `user_3J3wWGKWhc9mlYbZCmq8ESgHZxl`. Dashboard routes used ASGI + mocked `verify_clerk_session_token` for that real linked user — same Clerk-token gap as §21–24 (frontend still has no sign-in). `POST /agent/simulate` and temp `POST /orders` hit the live new code on that ASGI app (not the possibly-stale `localhost:8000` process). T-shirt `c10716b6-…` stock started at 3.

```
GET /notifications
200
[]

POST /agent/simulate
{"merchant_id": "37292228-…", "customer_phone": "+221770001210",
 "message": "Vous avez le t-shirt oversize blanc femme ?"}
200
{"conversation_id": "7cd80a6a-7682-462f-83e2-b06ff8e64c32",
 "reply": "Oui, le T-shirt oversize blanc femme est disponible en stock faible à 6 000 FCFA. Vous en voulez combien ?"}

… then 1 pièce / Sacré-Cœur Dakar / paiement à la livraison / Oui je confirme …

POST /agent/simulate
{"merchant_id": "37292228-…", "customer_phone": "+221770001210",
 "message": "Oui je confirme la commande"}
200
{"conversation_id": "7cd80a6a-…",
 "reply": "Votre commande est bien confirmée … Paiement à la livraison. Livraison prévue sous 24 à 48h à Sacré-Cœur, Dakar ; le livreur vous appellera juste avant d’arriver."}

order 880a4d08-6b0c-4386-9522-c8bc8f341080 status=created
t-shirt stock 3 → 2

GET /notifications
200
[
  {
    "id": "d0981c75-c0fd-4ee3-8b3f-b1bfbb396787",
    "type": "new_order",
    "related_type": "order",
    "related_id": "880a4d08-6b0c-4386-9522-c8bc8f341080",
    "data": {"total": "6000.00", "customer_phone": "+221770001210"},
    "read_at": null
  }
]
# no product_out_of_stock — stock stayed above 0

POST /agent/simulate
{"merchant_id": "37292228-…", "customer_phone": "+221770001211",
 "message": "je veux parler à quelqu'un, c'est une réclamation sur ma commande"}
200
{"conversation_id": "3c8853f4-3e71-4fda-8041-392dce8bf994",
 "reply": "D’accord, je vous mets en contact avec une personne pour votre réclamation.",
 "images": []}

GET /notifications
200
top row:
{
  "id": "a8dc1c8c-850b-48ac-a2ac-71e121bd68a4",
  "type": "conversation_escalated",
  "related_type": "conversation",
  "related_id": "3c8853f4-3e71-4fda-8041-392dce8bf994",
  "data": {"customer_phone": "+221770001211"},
  "read_at": null
}

# throwaway product b74902fd-5d34-42af-ba5a-5f481c09ccc5
# name="Notification test rupture" stock_qty=1

POST /orders
{"merchant_id": "37292228-…", "customer_phone": "+221770001212",
 "items": [{"product_id": "b74902fd-…", "quantity": 1}],
 "payment_method": "cash_on_delivery",
 "delivery_address": "Sacré-Cœur, Dakar", "ville": "Dakar"}
200
{"id": "1b1310da-4311-440c-824c-cec6bacaa925", "status": "created",
 "items": [{"product_id": "b74902fd-…", "quantity": 1, "unit_price": "1500.00"}]}
stock after = 0

GET /notifications
200
[
  {
    "id": "d6940ca3-75c6-48d8-892d-19570b979595",
    "type": "product_out_of_stock",
    "related_type": "product",
    "related_id": "b74902fd-5d34-42af-ba5a-5f481c09ccc5",
    "data": {"product_name": "Notification test rupture"},
    "read_at": null
  },
  {
    "id": "c3717d67-76c0-4e6b-9c54-964634436ed8",
    "type": "new_order",
    "related_type": "order",
    "related_id": "1b1310da-4311-440c-824c-cec6bacaa925",
    "data": {"total": "1500.00", "customer_phone": "+221770001212"},
    "read_at": null
  },
  {"id": "a8dc1c8c-…", "type": "conversation_escalated", "read_at": null},
  {"id": "d0981c75-…", "type": "new_order", "read_at": null}
]

POST /notifications/d6940ca3-75c6-48d8-892d-19570b979595/read
200
{"id": "d6940ca3-…", "type": "product_out_of_stock",
 "read_at": "2026-09-09T21:27:55.407296Z"}

GET /notifications
200
d6940ca3-… product_out_of_stock read_at set
c3717d67-… / a8dc1c8c-… / d0981c75-… still read_at=null

POST /notifications/read-all
200
{"updated": 3}

GET /notifications
200
count=4, unread=0
all four rows have read_at set
```

`data` has no pre-written French. Full pytest after the feature: `49 passed in 11.25s`.

### 26. Real WhatsApp webhook (2026-09-21)

Pipe only: `POST /whatsapp/webhook` enqueues an RQ job; the worker calls
`traiter_message_entrant` exactly once (same function as `/agent/simulate`)
then `POST graph.facebook.com/{WHATSAPP_API_VERSION}/{phone_number_id}/messages`.
No agent/tool/prompt changes. Queue is **RQ** (Celery was already in
`pyproject.toml` but unused). Redis is `REDIS_URL` (Compose host **6380**).
Dedupe is Redis `SETNX whatsapp:seen:{message_id}` (48h), not a new column.
Alembic `0009_merchant_whatsapp_phone`. Boutique Awa is linked with
`uv run python scripts/link_whatsapp_merchant.py`.

If `WHATSAPP_APP_SECRET` is empty the API logs
`webhook signature verification disabled — set WHATSAPP_APP_SECRET before the pilot`
and still accepts POSTs.

**How to finish the phone proof** (needs the verified test recipient + ngrok):

1. `docker compose up -d` then `uv run uvicorn app.main:app --reload`
2. `uv run python scripts/run_whatsapp_worker.py`
3. `ngrok http 8000` → Callback URL `https://<host>/whatsapp/webhook`,
   verify token = `WHATSAPP_WEBHOOK_VERIFY_TOKEN`, subscribe `messages`
4. From the test WhatsApp, send "vous avez des robes ?" then a follow-up
5. Screenshot: Meta green verify, WhatsApp thread (two turns), ngrok 4040
   POSTs with 200

Until those screenshots exist this row stays **Not started**. Unit tests
cover handshake / signature / unknown merchant / worker once / fallback.
Full pytest after the pipe: `56 passed in 8.90s`.

### 27. Product photo backfill + WhatsApp image send (2026-09-21)

`extract_product_images` moved to `app/agent/images.py`. `/agent/simulate`
`images` is still `[{product_id, image_url}]` only. After the text reply
the worker uploads each local file via
`POST /{version}/{phone_number_id}/media` and sends `type=image` with
`image.id` (not a public `link`). Cap 3 per turn; caption is the product
`name` already in the tool JSON.

Backfill: `uv run python scripts/backfill_product_photos.py` — skips any
product that already has a `product_images` row, writes via
`enregistrer_photo_produit`. gpt-image-1 paced at 15s (Tier 1 = 5 IPM).

Backfill 2026-09-21: Boutique Awa had 32 products / 3 photos. First run
generated 29 via `enregistrer_photo_produit` (gpt-image-1, 15s pace).
After: `products=32 with_photo=32`. Second run:
`32 with a photo, 0 to generate` / `Nothing to do (idempotent).`

**Phone proof still needed:** "vous avez des robes ?" → text then real
image bubbles; a >3-product turn sends at most 3 photos.

Full pytest after this change: `59 passed in 9.07s`. `/agent/simulate`
`images` items still only have `product_id` and `image_url`.

### 28. Order payment status (2026-10-04)

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` linked as
`user_3J3wWGKWhc9mlYbZCmq8ESgHZxl`. Dashboard routes used ASGI + mocked
`verify_clerk_session_token` for that real linked user. Product
`cd3f368c-…` (Coque iPhone 15 transparente), deliverer Ibrahima Diop
`6dbab944-…`.

**Before** `alembic upgrade head` (`0012_merchant_prefs`):

```
delivered + cash_on_delivery + pending: 3
  n°9  423bbb6d-… delivered cash_on_delivery pending
  n°17 00cae1c5-… delivered cash_on_delivery pending
  n°33 delivered cash_on_delivery pending
delivered + online: 0 rows
```

**After** `Running upgrade 0012_merchant_prefs -> 0013_backfill_cod_paid`:
`0013_backfill_cod_paid` applied; those 3 rows are `paid`. Created (24 COD
+ 3 online), assigned (1 COD), cancelled (3 COD + 1 online) still
`pending`. Online delivered rows were not touched (none existed).

```
POST /orders  cash_on_delivery  → n°36  bd8a11bf-…  payment_status=pending
POST /orders/{id}/assign-deliverer → deliverer_assigned
POST /orders/{id}/confirm-delivery → status=delivered payment_status=paid
GET  /orders/bd8a11bf-… → n=36 delivered cash_on_delivery paid

POST /orders  online  → n°37  6558e68b-…  payment_status=pending
GET  /orders/6558e68b-… → 37 pending
POST /orders/{id}/confirm-delivery → delivered pending
POST /orders/{id}/mark-paid → 200 paid
POST /orders/{id}/mark-paid → 200 paid  (idempotent)
POST /orders/bd8a11bf-…/mark-paid → 409
{"detail": "Only online-payment orders can be marked as paid; cash-on-delivery orders are marked paid when delivery is confirmed"}
```

Full pytest after the feature: `82 passed in 12.32s`.

### 29. Merchant WhatsApp send — payment link + dashboard reply (2026-10-04)

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d` / Clerk
`user_3J3wWGKWhc9mlYbZCmq8ESgHZxl` / Cloud API phone
`1229802316893138`. Customer phone `+221772738363` (existing Awa
thread `417e1a05-…`). `/agent/simulate` conversations cannot receive
replies (fake phones → 502).

`alembic upgrade head` → `0014_payment_link_sent_at`.

Agent confirmation after online order n°43 (`7a93368c-587c-4be2-b7b5-bfc390b70b5a`):

```
Merci pour votre commande 😊
Le numéro de votre commande est le 43.
Un livreur vous appellera juste avant de passer.
Retenez que le délai de livraison est entre 24 h et 48 h. …
Nous vous enverrons le lien de paiement ici sur WhatsApp. Après paiement, envoyez une photo de la preuve de paiement avec le numéro de commande.
```

Live Graph send (token expired, code 190):

```
POST /orders/7a93368c-…/send-payment-link
502 {"detail":"whatsapp_send_failed"}
GET  /orders/7a93368c-… → payment_link=https://pay.wave.com/nexa-awa-proof  payment_link_sent_at=null

POST /conversations/417e1a05-…/reply
502 {"detail":"whatsapp_send_failed"}
```

502 unit tests (no network):

```
tests/test_whatsapp_merchant_send.py::test_envoyer_message_commercant_not_configured_without_phone_id PASSED
tests/test_whatsapp_merchant_send.py::test_envoyer_message_commercant_not_configured_without_token PASSED
tests/test_whatsapp_merchant_send.py::test_envoyer_message_commercant_maps_graph_and_network_errors PASSED
tests/test_conversations_management.py::test_human_reply_send_failure_is_502_and_stores_nothing PASSED
tests/test_orders_management.py::test_send_payment_link_failures_and_rules PASSED
```

Phone screenshot for the payment-link / reply texts needs a fresh
`WHATSAPP_ACCESS_TOKEN`. Retry `send-payment-link` on n°43 (link already
saved) then `reply` on `417e1a05-…`.

Full pytest after the feature: `90 passed in 13.30s`.

## 28. Live inbound payment-proof photo (when the webhook works)

Blocked today: `WHATSAPP_ACCESS_TOKEN` expired (Graph 401/190) and the tunnel
is down. Code path is the RQ image job → `traiter_image_entrante` (same
function as the stubbed replay below). Do this on a real phone after the
token and webhook are restored.

1. Confirm `GET /whatsapp/webhook` handshake and that the worker is listening
   on the `whatsapp` queue.
2. Use an **online** Boutique Awa order whose payment link was sent through
   Nexa (`payment_link_sent_at` set). n°43 (`7a93368c-…`) still needs a
   successful `POST /orders/{id}/send-payment-link` (link is saved, timestamp
   is still null until Graph accepts the send).
3. From the customer phone, send a **photo / screenshot** of a payment
   confirmation with caption `n°{order_number}` (or just the photo if that
   phone has only one awaiting-proof order).
4. Expect: image stored under `MEDIA_DIR` (not `/static`); thread row
   `display_text` = caption or `Photo`; `inbound_images.classification=payment_proof`;
   order `payment_status=proof_received` (**not** `paid`); customer ack
   `Merci, nous avons bien reçu votre photo. La boutique va vérifier votre paiement.`;
   merchant notification “Preuve de paiement reçue — commande #{n} : à vérifier.”
5. `GET /images/{id}` with a merchant session → 200, `Content-Type` of the
   file, `Cache-Control: private, no-store`. Other merchant → 404.
6. `POST /orders/{id}/mark-paid` → `paid`. Or `POST /orders/{id}/reject-proof`
   → `pending` (images kept). A second photo after reject is analysed again.
7. Negative: photo from a phone with no awaiting-proof order (no link sent,
   COD, paid, cancelled, or no order) → file + thread row,
   `classification=not_analyzed`, no vision call, no ack, no notification.
8. `proof_received` must never be shown as paid in the UI.

Stubbed replay used while the webhook was down (2026-10-04),
`uv run python ../proofs/replay_inbound_proof.py` from `Nexa/backend`:

```
created_order_id ab1cc4fb-c65b-4d71-be42-df3b0d2173c8
order_number 46
conversation_id 73cacfa1-2497-43e5-832f-304986cf424a
product Baskets blanches homme ac0fae1f-…
inbound_images 55554ba2-…  classification=payment_proof  order_id=ab1cc4fb-…
order payment_status=proof_received
notification 9bd271cc-…  "Preuve de paiement reçue — commande #46 : à vérifier."
messages customer "n°46" + merchant ack
GET /images/55554ba2-…  200  image/png  Cache-Control: private, no-store
```

Frontend seed (outside the repo): `uv run python ../proofs/seed_inbound_proof.py --demo-awa`

```
matched n°47 0761704d-…  image 663812e9-…
unmatched conversation e9bdfa60-…  image a41d1c00-…  classification=unknown
GET /images/663812e9-…  200  image/png  private, no-store
```

Alembic cycle:

```
payment_status values: ['pending', 'paid', 'proof_received']
inbound_images: inbound_images
alembic_version: 0015_payment_proofs
alembic downgrade -1
payment_status values: ['pending', 'paid']
inbound_images: None
alembic_version: 0014_payment_link_sent_at
alembic upgrade head
payment_status values: ['pending', 'paid', 'proof_received']
```

Full pytest after the feature: `101 passed in 15.42s`.

## 29. Agent context after handover (2026-10-06, `gpt-5.6-terra`, Boutique Awa)

Throwaway script `Nexa/proofs/proof_agent_context.py`. Phones +221770099011…015. Cleanup empty afterwards.

**Incident input_list** (condensed) ended with: `[Boutique] oui on l'a` → photo marker → handover developer note → `Je veux commande pour la robe ci-haut`.

**Incident ×3** (“Je veux commande pour la robe ci-haut”). No tool calls on any run. 0/3 guessed the red dress.

- Run 1: « Je ne peux pas encore voir les photos. Pouvez-vous me confirmer le nom ou la couleur de la robe que vous souhaitez commander ? »
- Run 2: « Je ne peux pas encore voir les photos. Pouvez-vous me donner le nom ou la couleur de la robe souhaitée ? »
- Run 3: « Je ne peux pas encore voir les photos 🙏 Pouvez-vous me donner le nom, la couleur ou le type de la robe ? »

**Explicit “Je veux la robe rouge”**

- No `sent_product_images`: `obtenir_disponibilite` on `8717de24-…` with `photo_status=will_be_sent`. Reply named the red dress at 25 000 F; no photo promise in the text.
- With `sent_product_images`: `rechercher_produits` returned the red dress with `photo_status=already_sent_earlier`. Same kind of availability reply; no new photo promise.

**MANUAL_TESTS regressions**

- Nominal / typos: this run the model searched with `categorie="robes"` / `"Robes"` (not a catalogue category) and said it had no red evening dress. That is a material miss vs the 2026-09-08 transcripts (which found Robe longue rouge de soirée). Not caused by a missing marker; the query named the product.
- Vague “je cherche une robe de soirée”: both evening dresses + pagne, `photo_status=will_be_sent`. Follow-up “oui” asked black vs red; no order.
- Escalation: `escalader_vers_humain`; closed-hours sentence (shop clock), not the 2026-09-08 “je vous mets en relation” wording.

`pytest` after the feature: **152 passed**.

## 30. Tolerant `rechercher_produits` category (2026-10-06)

Throwaway `Nexa/proofs/proof_category_search.py`. Phone +221770099021. Worktree of HEAD `b01c1c3` under `Nexa/proofs/baseline-search`, removed after.

**Direct service** (`requete="robe rouge"`, Boutique Awa). Stored categories are `vêtements femme`, `chaussures`, etc. — not `Robes`. All of `Robes` / `robes` / `robe` / `Vêtements` / `None` returned **Robe longue rouge de soirée** (wrong names drop the filter and retry unfiltered; `rag_max_distance` still applies).

**Baseline vs current, real model, ×3 each**

| Tree | Scenario | `rechercher_produits` args | Red dress found |
|------|----------|-----------------------------|-----------------|
| HEAD baseline | nominal ×3 | `categorie: null` | yes ×3 |
| HEAD baseline | typos ×3 | `categorie: null` | yes ×3 |
| current (rule 15 + this fix) | nominal ×3 | `categorie: null` | yes ×3 |
| current | typos ×3 | `categorie: null` | yes ×3 |

`categorie="robes"` did **not** appear on the baseline (pre-rule-15) in this 6-run sample. The miss from §29 is **pre-existing model flakiness**, not caused by rule 15. This fix still covers that guess when it happens (service proof above).

**Context regression (current tree)**

- Incident “Je veux commande pour la robe ci-haut” ×3: no tools, short French clarifying question, no product guessed.
- Vague “je cherche une robe de soirée”: `categorie: null`, red dress found; follow-up “oui” asked which colour/model, no order.

Cleanup: 0 leftover conversations/orders for +221770099021 on Boutique Awa. `pytest` **158 passed**.

## 31. Catalogue image search engine (2026-10-06, step 1 only)

No route, no WhatsApp/agent/vision wiring. Vectors live in `products.image_embedding`
(not mixed with text `products.embedding`). Sources: Voyage multimodal docs
https://docs.voyageai.com/docs/multimodal-embeddings (current model
`voyage-multimodal-3.5`, 1024-d default; `voyage-multimodal-3` is listed as
older), API limits https://docs.voyageai.com/reference/multimodal-embeddings-api
(16M pixels / 20 MB), pricing https://docs.voyageai.com/docs/pricing
($0.60 / billion pixels, 50k floor → min $0.00003, 2M ceiling → max $0.0012).
Installed `voyageai==0.5.0` exposes `Client.multimodal_embed`. Catalogue photos
use `input_type='document'`; search photos use `input_type='query'`.

Alembic:

```
Running upgrade 0020_deliverer_unique_phone -> 0021_product_image_embedding
Running downgrade 0021_product_image_embedding -> 0020_deliverer_unique_phone
Running upgrade 0020_deliverer_unique_phone -> 0021_product_image_embedding
```

`pytest` **168 passed in 25.16s** (10 new tests; Voyage stubbed, no network).

Boutique Awa `37292228-b8f5-437d-b8e6-2ff81d4e249d`: **26 products, 26 local photos**.

Backfill (dry-run then real):

```
products_to_embed=26
skipped_already_current=0
unreadable=0
pixels_after_downscale=27262976
estimated_cost_usd=0.01635779 (https://docs.voyageai.com/docs/pricing)
dry_run=1 (no writes)

… real run …
ok=26
skipped=0
failed=0
```

Self-match (own photo as `query` against stored `document` vectors): rank 1
**26/26**. Distance is **not** ≈0 (asymmetric input_type): min 0.0551, median
0.0750, max 0.0998.

Synthetic variants of the same 26 catalogue photos (Pillow only):

| Variant | Rank 1 | Top 3 | Correct-match dist min / median / max |
|---|---|---|---|
| centre crop 70% | 100% | 100% | 0.0879 / 0.1365 / 0.2159 |
| rotation 8° | 100% | 100% | 0.0636 / 0.1226 / 0.1454 |
| brightness +25% | 100% | 100% | 0.0559 / 0.0976 / 0.4193 |
| brightness −25% | 100% | 100% | 0.0614 / 0.0845 / 0.1252 |
| JPEG quality 30 | 100% | 100% | 0.0553 / 0.1104 / 0.1355 |
| TikTok-like 9:16 screenshot | 100% | 100% | 0.1606 / 0.2692 / 0.3258 |

Closest look-alikes (gap to second via the same query search): iPhone cases
0.2646, evening dresses 0.2657. Gap min 0.2646, median 0.4161.

Negatives (Pillow, not catalogue): plain white 0.7507, payment-style screenshot
0.7399, photo of text 0.7719 — all `classify_image_match=none`.

`Nexa/proofs/image-eval/manifest.csv` was absent (script skipped, no failure).

Placeholders in config stay `strong=0.20`, `possible=0.45`, `min_margin=0.08`.
On this synthetic set they already separate self-match (strong) from negatives
(none). A tighter proposal from the same data — **not** to ship until real
photos exist — is `strong=0.16`, `possible=0.38`, `min_margin=0.12`. The
brightness +25% outlier at 0.42 and TikTok median 0.27 show how little studio
photos plus Pillow prove about a real WhatsApp customer photo.

Cleanup: no throwaway products/files created. Lasting DB change is Alembic
`0021` plus the 26 Awa `image_embedding` values written by the backfill.
Throwaway measurer: `Nexa/proofs/image-search/measure_image_search.py`.

## 32. Product photo recognition wired to WhatsApp + agent (2026-10-06, step 2)

Backend only. No new route. Customer photos are classified by vision
(`payment_proof` | `product_photo` | `other`), then a `product_photo` on a
non-escalated thread runs `trouver_produits_par_image` and one agent turn
(`traiter_photo_produit`) that must confirm before `creer_commande`.

Alembic `0022_inbound_image_match` (`matched_product_id`, `match_level`,
`match_candidates`):

```
Running upgrade 0021_product_image_embedding -> 0022_inbound_image_match
Running downgrade 0022_inbound_image_match -> 0021_product_image_embedding
Running upgrade 0021_product_image_embedding -> 0022_inbound_image_match
```

`pytest` **187 passed in 25.47s** (19 new in `tests/test_product_photo.py`;
vision, Voyage, and the LLM stubbed).

Live throwaway: `uv run python ../proofs/photo-recognition/run_photo_recognition.py`
on Boutique Awa `37292228-…`, phones +221770099031…041. WhatsApp download/send
replaced by local stubs (nothing to Meta). Vision, Voyage, and the agent model
are real. `Nexa/proofs/image-eval/` had only `manifest.csv` (no image files),
so the script built TikTok-like 9:16 canvases from Awa catalogue photos plus a
PIL payment screenshot and a blue recolour "not sold" image.

| # | Setup | Result |
|---|--------|--------|
| 1 | TikTok-like of Robe longue rouge de soirée | Vision `product_photo`, "A fitted red sleeveless maxi dress…". Voyage rank-1 **was** that dress (`8717de24-…`) at **0.495** → `none` (cutoff 0.45). Agent: cannot find it, shop informed, asked name/colour. No photo sent, **0 orders**. vision 9.52s / Voyage 1.80s / agent 1.36s. |
| 2a | Then customer « oui » | Agent asked again for name/type (nothing had been proposed). **0 orders**. |
| 2b | Fresh phone, same photo, then « non, plutôt la noire » | Photo again `none`. Agent followed the customer ("article noir") and did not invent a product. **0 orders**. |
| 3 | TikTok-like of the black evening dress | `possible`, matched `411ff5b6-…` (the black dress) at 0.439; gap to red 0.121 **≥** `image_match_min_margin` 0.08 → proposal set size **1** (not an ambiguous pair). Agent called `obtenir_disponibilite`, sent the photo, asked "C'est bien celle-ci ?". No order. |
| 4 | Blue recolour of the red dress ("Awa does not sell") | Still `possible` on the red dress at 0.203 — the recolour is too close to the original. Agent proposed the red dress + photo. **No** `product_photo_unrecognized` (only `none`/`error` notify). Voyage 55.91s (free-tier 429 retry). |
| 5a | Payment-style screenshot + online order awaiting proof (n°59) | `payment_proof`, `payment_status=proof_received`, ACK sent, **no** Voyage, **no** product proposal. |
| 5b | Same screenshot, no awaiting order | Still `payment_proof` (not a product proposal). ACK "commande à identifier" path. No agent turn. |
| 6 | Escalated conversation | `not_analyzed`, vision **0**, Voyage **0**, no reply. |
| 7 | Daily cap (10 analysed rows already) | `not_analyzed`, no reply. |
| 8a | Vision forced to fail | `unknown`, no crash, no recognition, no agent reply. |
| 8b | Voyage forced to fail | `match_level=error`, French "pas ce modèle" + shop informed, notification `product_photo_unrecognized` "Photo de produit non reconnue". No crash. |
| 9 | Caption « Vous avez cette robe? » + same TikTok red | Caption stored and passed as untrusted data. Same `none` as (1). Agent asked for name/colour/type of robe. |

**Where search/agent got it wrong:** (1)(9) the right dress was rank-1 but 0.045 above `possible` so the agent honestly said it could not find the model — thresholds need real-traffic tuning from `match_candidates`. (3) look-alikes did **not** produce a two-product proposal (gap 0.12 > 0.08). (4) a recoloured catalogue photo is not a true negative.

Frontend not in this change: unknown notification types render as raw `item.type`; a `product_photo` in the thread currently has no "Produit reconnu" label (`thread-panel.tsx` returns null).

Cleanup dry-run listed 11 conversations, order n°59, 21 inbound images, 9 notifications; apply restored red-dress stock +1; leftover **0** rows for those phones on Awa.

## 33. Embeddings shortlist + vision verification (2026-10-07, step 2b)

Backend only. No new route. Cosine distance only **shortlists**; a vision
call looks at the customer photo next to up to 4 catalogue photos and
returns `same` / `similar` / `none`. Existing `image_match_*` cutoffs
remain the fallback when verification is off or fails, and that fallback
is **capped at `possible`** (never `strong` without a successful verify).

Retrieval measurement (`measure_shortlist.py`, `trouver_produits_par_image`
limit=6, Awa catalogue, starter set + extra step-2 TikTok canvas): **9/9**
positives had the expected product in the top 4 at ≤0.55 / 0.65 / 0.75
(0 missing at 0.65 → continue). NONE best distances: blue recolour 0.203
(red dress), green sandals 0.233 (tan sandals) — they enter a 0.65
shortlist, which is why verification is required.

`pytest` **198 passed in 25.59s** (11 new: 7 in `tests/test_product_photo.py`,
4 in `tests/test_image_verify.py`; Voyage, vision, and the LLM stubbed).

Compare A (thresholds on the same shortlist) vs B (vision verify, run
twice) on 10 `image-eval` files + `extra_step2_tiktok_robe_rouge.jpg`.
Verifier model `gpt-5.6-terra`. List price quoted 2026-10-07 from
https://developers.openai.com/api/docs/models/gpt-5.6-terra :
**$2.00 / 1M input, $12.00 / 1M output**.

| file | expected | A | B1 | B2 | score A | score B | voy s | ver s | tokens in+out | est. $ |
|---|---|---|---|---|---|---|---|---|---|---|
| tiktok_robe_noire.jpg | black dress | possible / black | strong/same / black | same | yes | yes | 1.5 | 8.2 | 4510+102 | 0.0102 |
| tiktok_robe_rouge_zoom.jpg | red dress | none | strong/same / red | same | miss | yes | 1.9 | 4.6 | 3878+103 | 0.0090 |
| tiktok_baskets_blanches_carre.jpg | white sneakers | none | strong/same / white | same | miss | yes | 1.2 | 4.0 | 3246+95 | 0.0076 |
| tiktok_sac_main.jpg | camel bag | none | strong/same / camel | same | miss | yes | 40.3 | 3.5 | 3246+94 | 0.0076 |
| tiktok_montre_or_rose.jpg | rose-gold watch | possible / watch | strong/same / watch | same | yes | yes | 7.8 | 3.8 | 2614+94 | 0.0064 |
| angle_ceinture_wax.jpg | wax belt | strong / belt | strong/same / belt | same | yes | yes | 1.4 | 4.1 | 5584+83 | 0.0122 |
| angle_baskets_noires.jpg | black sneakers | strong / black | strong/same / black | same | yes | yes | 39.9 | 3.9 | 5584+95 | 0.0123 |
| angle_coque_iphone_noire.jpg | black case | strong / case | strong/same / case | same | yes | yes | 7.4 | 4.4 | 5584+93 | 0.0123 |
| couleur_robe_bleue.jpg | NONE | possible / red | possible/similar / red | similar | acceptable-similar | acceptable-similar | 1.2 | 3.2 | 5584+102 | 0.0124 |
| couleur_sandales_vertes.jpg | NONE | possible / tan sandals | possible/similar / tan | similar | acceptable-similar | acceptable-similar | 56.5 | 4.3 | 5584+91 | 0.0123 |
| extra_step2_tiktok_robe_rouge.jpg | red dress | possible / red | strong/same / red | same | yes | yes | 1.0 | 5.0 | 4510+102 | 0.0102 |

B was stable (run 1 = run 2 on every row). **A recall 6/9, precision 1.00,
false-strong 0. B recall 9/9, precision 1.00, false-strong 0.** Ship **B**:
A still misses TikTok-like frames above the 0.45 `possible` cutoff (the
step-2 live miss) and would have proposed the red dress as a normal
possible hit on the blue recolour. B labels that `similar` and never
emits `strong` without a successful verification. Ten synthetic images
plus one extra canvas prove very little about real WhatsApp photos —
same catalogue files with Pillow crops/recolours, not customer phones.

Full pipeline (`traiter_image_entrante`, Meta stubbed, real classifier /
Voyage / verifier / agent) on +221770099041…044:

| # | Setup | Result |
|---|--------|--------|
| 1 | Extra step-2 TikTok canvas of the red dress | `product_photo`, `strong`/`same` on `8717de24-…` at 0.246. Agent: "Je pense que c’est cette robe. C’est bien celle-ci ?" + catalogue photo. No order. vision 3.12s / Voyage 1.22s / verify 1.79s (2255+49 tok) / agent 2.70s. |
| 2 | Blue recolour of the red dress | `possible`/`similar` on the red dress at 0.203. Developer similar rule present. Agent: "Je n’ai pas exactement ce modèle, mais j’ai une Robe longue rouge de soirée disponible. Est-ce qu’elle vous intéresse ?" + photo. No order. vision 2.01s / Voyage 8.50s / verify 1.84s (2792+50 tok) / agent 3.66s. |
| 3 | `reel_*` owner photo | skipped — no `reel_*` file in `image-eval`. |
| 4 | Payment-style screenshot + online order n°60 awaiting proof | `payment_proof`, ACK sent, **Voyage 0, verify 0**, no agent turn, no product proposal. |

Cleanup dry-run: 3 conversations, order n°60, 6 messages, 3 inbound
images, 2 sent photos, 2 notifications, 1 stock movement; apply restored
red-dress stock +1; leftover **0** rows for those phones on Awa.

## 34. Visual-search fields on thread images (2026-10-07)

`GET /conversations/{id}/messages` now includes on each `image`:
`match_level`, `matched_product_id`, `matched_product_name`, `match_kind`
(`exact` / `similar`). No schema change. Names loaded in one merchant-scoped
query. Distances, candidate lists, and the verifier model stay off the
payload.

`pytest` **202 passed in 28.64s** (4 new in `tests/test_conversation_image_match.py`).

Live throwaway: `uv run python ../proofs/thread_image_match.py` on Boutique
Awa, phone +221770099051, Clerk session stubbed as in other proofs. Three
product-photo rows:

- `strong` / `exact` → `Robe longue rouge de soirée` (`8717de24-…`)
- `possible` / `similar` → same name
- `none` → `matched_product_id` and name null

Cleanup dry-run: 1 conversation `d1716ca6-…`, 3 messages, 3 inbound images;
apply leftover **0**. Frontend mapping of these fields is not in this change.





