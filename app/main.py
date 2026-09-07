from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(title="Nexa API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
