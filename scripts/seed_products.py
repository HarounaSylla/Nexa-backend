"""Seed a demo merchant and 26 French-language products with real embeddings.

Run once against a fresh DB (after `alembic upgrade head`):

    python scripts/seed_products.py

Requires VOYAGE_API_KEY in `.env`. If merchant "Boutique Awa" already exists,
the script indexes any products that still have a null embedding and exits
without duplicating rows.

Unpaid Voyage accounts are limited to 3 RPM. Pending rows are embedded in
one `embed_documents()` call (same function `index_product` uses) so a
rate-limit error does not roll back progress. Re-run to resume.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from app.catalogue.embeddings import embed_documents, product_index_text
from app.catalogue.models import Merchant, Product
from app.core.config import settings
from app.core.db import AsyncSessionLocal

MERCHANT_NAME = "Boutique Awa"

# Realistic catalogue for a Dakar TikTok / WhatsApp seller. Near-duplicates
# (two evening dresses, two iPhone cases) are intentional so similarity
# search has something real to rank.
PRODUCTS: list[dict[str, object]] = [
    {
        "name": "Robe longue rouge de soirée",
        "description": (
            "Robe longue rouge vif, coupe sirène, idéale pour un mariage "
            "ou une soirée à Dakar. Tissu fluide, taille ajustable."
        ),
        "category": "vêtements femme",
        "price": Decimal("25000.00"),
        "stock_qty": 8,
    },
    {
        "name": "Robe longue noire de soirée",
        "description": (
            "Robe longue noire élégante, même coupe sirène que la version "
            "rouge, parfaite pour une soirée chic ou un dîner."
        ),
        "category": "vêtements femme",
        "price": Decimal("25000.00"),
        "stock_qty": 6,
    },
    {
        "name": "Ensemble deux pièces pagne wax",
        "description": (
            "Haut et jupe en pagne wax coloré, motifs africains, "
            "taille unique avec ceinture."
        ),
        "category": "vêtements femme",
        "price": Decimal("18000.00"),
        "stock_qty": 12,
    },
    {
        "name": "Hijab en soie beige",
        "description": (
            "Voile hijab en soie beige clair, léger et opaque, "
            "facile à draper pour tous les jours."
        ),
        "category": "vêtements femme",
        "price": Decimal("4500.00"),
        "stock_qty": 20,
    },
    {
        "name": "T-shirt oversize blanc femme",
        "description": (
            "T-shirt oversize coton blanc, col rond, coupe ample "
            "pour un look casual."
        ),
        "category": "vêtements femme",
        "price": Decimal("6000.00"),
        "stock_qty": 15,
    },
    {
        "name": "Jean slim taille haute",
        "description": (
            "Jean slim bleu foncé, taille haute, stretch confortable "
            "pour le quotidien."
        ),
        "category": "vêtements femme",
        "price": Decimal("12000.00"),
        "stock_qty": 10,
    },
    {
        "name": "Baskets blanches homme",
        "description": (
            "Baskets blanches homme, semelle confort, style casual "
            "pour la ville et le sport léger."
        ),
        "category": "chaussures",
        "price": Decimal("15000.00"),
        "stock_qty": 9,
    },
    {
        "name": "Baskets noires homme running",
        "description": (
            "Baskets de running noires pour homme, mesh respirant, "
            "bon maintien pour la marche et le jogging."
        ),
        "category": "chaussures",
        "price": Decimal("17000.00"),
        "stock_qty": 7,
    },
    {
        "name": "Sandales plates femme",
        "description": (
            "Sandales plates femme en similicuir, brides ajustables, "
            "confortables pour la chaleur."
        ),
        "category": "chaussures",
        "price": Decimal("8000.00"),
        "stock_qty": 14,
    },
    {
        "name": "Escarpins noirs à talon",
        "description": (
            "Escarpins noirs talon 7 cm, bout pointu, pour bureau "
            "ou soirée."
        ),
        "category": "chaussures",
        "price": Decimal("14000.00"),
        "stock_qty": 5,
    },
    {
        "name": "Mocassins cuir marron",
        "description": (
            "Mocassins homme en cuir marron, semelle souple, "
            "style habillé décontracté."
        ),
        "category": "chaussures",
        "price": Decimal("16000.00"),
        "stock_qty": 6,
    },
    {
        "name": "Collier perles africaines",
        "description": (
            "Collier artisanal en perles colorées, inspiration "
            "africaine, fermoir métal."
        ),
        "category": "accessoires",
        "price": Decimal("7500.00"),
        "stock_qty": 11,
    },
    {
        "name": "Boucles d'oreilles créoles dorées",
        "description": (
            "Créoles dorées de taille moyenne, légères, pour un "
            "look quotidien ou une sortie."
        ),
        "category": "accessoires",
        "price": Decimal("3500.00"),
        "stock_qty": 18,
    },
    {
        "name": "Sac à main en cuir camel",
        "description": (
            "Sac à main camel, bandoulière amovible, plusieurs "
            "compartiments pour le téléphone et le portefeuille."
        ),
        "category": "accessoires",
        "price": Decimal("22000.00"),
        "stock_qty": 4,
    },
    {
        "name": "Montre femme or rose",
        "description": (
            "Montre analogique bracelet or rose, cadran minimaliste, "
            "pile incluse."
        ),
        "category": "accessoires",
        "price": Decimal("9500.00"),
        "stock_qty": 8,
    },
    {
        "name": "Ceinture en wax",
        "description": (
            "Ceinture large en pagne wax, boucle dorée, s'adapte "
            "à plusieurs tailles."
        ),
        "category": "accessoires",
        "price": Decimal("4000.00"),
        "stock_qty": 13,
    },
    {
        "name": "Beurre de karité brut",
        "description": (
            "Beurre de karité non raffiné 250 ml, hydratation "
            "corps et cheveux, issu du commerce local."
        ),
        "category": "cosmétiques",
        "price": Decimal("5000.00"),
        "stock_qty": 22,
    },
    {
        "name": "Huile de coco capillaire",
        "description": (
            "Huile de coco pressée à froid pour nourrir les cheveux "
            "crépus et bouclés, 200 ml."
        ),
        "category": "cosmétiques",
        "price": Decimal("4500.00"),
        "stock_qty": 16,
    },
    {
        "name": "Rouge à lèvres mat bordeaux",
        "description": (
            "Rouge à lèvres mat longue tenue, teinte bordeaux, "
            "fini velours sans dessécher."
        ),
        "category": "cosmétiques",
        "price": Decimal("3500.00"),
        "stock_qty": 19,
    },
    {
        "name": "Fond de teint teinte foncée",
        "description": (
            "Fond de teint liquide teinte foncée / peau noire, "
            "couvrance moyenne, fini naturel."
        ),
        "category": "cosmétiques",
        "price": Decimal("8000.00"),
        "stock_qty": 10,
    },
    {
        "name": "Parfum musc blanc 50 ml",
        "description": (
            "Eau de parfum musc blanc, sillage doux, flacon 50 ml "
            "idéal en cadeau."
        ),
        "category": "cosmétiques",
        "price": Decimal("12000.00"),
        "stock_qty": 7,
    },
    {
        "name": "Coque iPhone 15 transparente",
        "description": (
            "Coque souple transparente pour iPhone 15, coins "
            "renforcés, ne jaunit pas."
        ),
        "category": "électronique",
        "price": Decimal("4000.00"),
        "stock_qty": 25,
    },
    {
        "name": "Coque iPhone 15 noire mate",
        "description": (
            "Coque noire mate pour iPhone 15, même protection que "
            "le modèle transparent, fini antidérapant."
        ),
        "category": "électronique",
        "price": Decimal("4000.00"),
        "stock_qty": 21,
    },
    {
        "name": "Écouteurs Bluetooth TWS",
        "description": (
            "Écouteurs sans fil Bluetooth, boîtier de charge, "
            "autonomie environ 4 h, micro intégré."
        ),
        "category": "électronique",
        "price": Decimal("9000.00"),
        "stock_qty": 12,
    },
    {
        "name": "Powerbank 20000 mAh",
        "description": (
            "Batterie externe 20000 mAh, deux ports USB, charge "
            "rapide pour téléphone et écouteurs."
        ),
        "category": "électronique",
        "price": Decimal("13000.00"),
        "stock_qty": 9,
    },
    {
        "name": "Chargeur solaire portable",
        "description": (
            "Panneau solaire pliable avec sortie USB, pour recharger "
            "le téléphone en déplacement ou en voyage."
        ),
        "category": "électronique",
        "price": Decimal("18500.00"),
        "stock_qty": 3,
    },
]


async def _ensure_merchant(db) -> Merchant:
    result = await db.execute(
        select(Merchant).where(Merchant.name == MERCHANT_NAME)
    )
    merchant = result.scalar_one_or_none()
    if merchant is not None:
        return merchant
    merchant = Merchant(name=MERCHANT_NAME)
    db.add(merchant)
    await db.flush()
    return merchant


async def seed() -> None:
    if not settings.voyage_api_key:
        raise SystemExit(
            "VOYAGE_API_KEY is empty. Set it in .env before seeding "
            "(real embeddings are required; do not use placeholder vectors)."
        )
    async with AsyncSessionLocal() as db:
        merchant = await _ensure_merchant(db)
        existing = await db.execute(
            select(func.count()).select_from(Product).where(
                Product.merchant_id == merchant.id
            )
        )
        product_count = existing.scalar_one()
        if product_count == 0:
            for row in PRODUCTS:
                db.add(
                    Product(
                        merchant_id=merchant.id,
                        name=str(row["name"]),
                        description=str(row["description"]),
                        category=str(row["category"]),
                        price=row["price"],
                        stock_qty=int(row["stock_qty"]),
                    )
                )
            await db.flush()
            await db.commit()
            print(f"Inserted {len(PRODUCTS)} products for {MERCHANT_NAME}.")
        else:
            print(
                f"Merchant {MERCHANT_NAME} already has {product_count} products; "
                "not inserting duplicates."
            )

        result = await db.execute(
            select(Product).where(Product.merchant_id == merchant.id)
        )
        products = list(result.scalars().all())
        pending = [product for product in products if product.embedding is None]
        skipped = len(products) - len(pending)
        indexed = 0
        if pending:
            texts = [product_index_text(product) for product in pending]
            embeddings = await asyncio.to_thread(embed_documents, texts)
            for product, embedding in zip(pending, embeddings, strict=True):
                product.embedding = embedding
                indexed += 1
                print(f"Indexed: {product.name} ({product.id})")
            await db.commit()

        with_embedding = sum(1 for product in products if product.embedding is not None)
        print("---")
        print(f"merchant_id={merchant.id}")
        print(f"products={len(products)}")
        print(f"with_embedding={with_embedding}")
        print(f"indexed_this_run={indexed}")
        print(f"already_indexed={skipped}")
        for product in products:
            print(f"  {product.id}  [{product.category}] {product.name}")


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
