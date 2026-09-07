from fastapi import FastAPI

from app.catalogue.router import router as catalogue_router
from app.core.config import settings
from app.orders.router import router as orders_router

app = FastAPI(title="Nexa API")
app.include_router(catalogue_router)
app.include_router(orders_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
