from fastapi import FastAPI

from app.catalogue.router import router as catalogue_router
from app.core.config import settings

app = FastAPI(title="Nexa API")
app.include_router(catalogue_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
