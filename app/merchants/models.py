import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

DEFAULT_SHOP_TIMEZONE = "Africa/Dakar"


class MerchantPreferences(Base):
    __tablename__ = "merchant_preferences"
    __table_args__ = (
        CheckConstraint(
            "accepts_cash_on_delivery OR accepts_online_payment",
            name="ck_merchant_preferences_one_payment_method",
        ),
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    accepts_cash_on_delivery: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    accepts_online_payment: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    shop_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_hours: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=DEFAULT_SHOP_TIMEZONE,
        server_default=text(f"'{DEFAULT_SHOP_TIMEZONE}'"),
    )
    delivery_fee_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
