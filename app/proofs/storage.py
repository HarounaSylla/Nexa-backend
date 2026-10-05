"""Private inbound media. Never written under /static."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import settings

ALLOWED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class RejectedMediaError(ValueError):
    """MIME type or size is not acceptable."""


def media_root() -> Path:
    return Path(settings.media_dir).resolve()


def extension_for_mime(mime_type: str) -> str | None:
    return ALLOWED_MIME_TYPES.get(mime_type)


def save_inbound_image(
    merchant_id: uuid.UUID, mime_type: str, content: bytes
) -> str:
    ext = extension_for_mime(mime_type)
    if ext is None:
        raise RejectedMediaError(f"Unsupported MIME type {mime_type}")
    if len(content) > MAX_IMAGE_BYTES:
        raise RejectedMediaError("Image exceeds 5 MB")
    relative = f"{merchant_id}/{uuid.uuid4()}{ext}"
    path = media_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return relative.replace("\\", "/")


def absolute_media_path(relative: str) -> Path:
    root = media_root()
    path = (root / relative).resolve()
    if root not in path.parents and path != root:
        raise FileNotFoundError(relative)
    return path


def delete_inbound_image_file(relative: str | None) -> None:
    """Unlink a file under the private media root. Missing files are fine."""
    if not relative:
        return
    path = absolute_media_path(relative)
    path.unlink(missing_ok=True)
