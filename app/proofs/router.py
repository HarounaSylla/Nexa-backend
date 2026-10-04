import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.orders.service import NotFoundError
from app.proofs.service import obtenir_image_commercant
from app.proofs.storage import absolute_media_path
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/images", tags=["images"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Image was not found")


@router.get("/{image_id}")
async def get_inbound_image(
    image_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    try:
        image = await obtenir_image_commercant(db, merchant.id, image_id)
    except NotFoundError as exc:
        raise _not_found() from exc
    path = absolute_media_path(image.media_path)
    if not path.is_file():
        raise _not_found()
    return FileResponse(
        path,
        media_type=image.mime_type,
        headers={"Cache-Control": "private, no-store"},
    )
