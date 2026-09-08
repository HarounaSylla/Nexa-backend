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
| P2 | Photos and voice messages | Deferred | No WhatsApp webhook yet — Jalon 3 part 2, blocked on Cloud API access. |

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
