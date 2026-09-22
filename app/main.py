import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.agent.router import conversations_router
from app.agent.router import router as agent_router
from app.catalogue.router import router as catalogue_router
from app.core.config import settings
from app.merchants.router import router as merchants_router
from app.notifications.router import router as notifications_router
from app.orders.router import deliverers_router
from app.orders.router import router as orders_router
from app.whatsapp.router import router as whatsapp_router
from app.whatsapp.service import signature_verification_enabled

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
(_STATIC_DIR / "product_images").mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not signature_verification_enabled():
        logger.warning(
            "webhook signature verification disabled — set "
            "WHATSAPP_APP_SECRET before the pilot"
        )
    yield


app = FastAPI(title="Nexa API", lifespan=lifespan)
_origins = settings.clerk_authorized_party_list()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.include_router(catalogue_router)
app.include_router(orders_router)
app.include_router(deliverers_router)
app.include_router(agent_router)
app.include_router(conversations_router)
app.include_router(merchants_router)
app.include_router(notifications_router)
app.include_router(whatsapp_router)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
