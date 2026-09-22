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
| P2 | WhatsApp inbound media / voice | Deferred | Inbound non-text is still logged and skipped. Outbound product photos now use `/media` + `media_id`. |

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



