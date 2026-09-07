import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class OrderStatus(str, enum.Enum):
    created = "created"
    deliverer_assigned = "deliverer_assigned"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentMethod(str, enum.Enum):
    cash_on_delivery = "cash_on_delivery"
    online = "online"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda members: [item.value for item in members],
    )


class Deliverer(Base):
    __tablename__ = "deliverers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="deliverer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id"),
        nullable=False,
        index=True,
    )
    customer_phone: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[OrderStatus] = mapped_column(
        _pg_enum(OrderStatus, "order_status"),
        nullable=False,
        default=OrderStatus.created,
        server_default=text("'created'"),
        index=True,
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        _pg_enum(PaymentMethod, "payment_method"),
        nullable=False,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.pending,
        server_default=text("'pending'"),
    )
    payment_link: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_address: Mapped[str] = mapped_column(Text, nullable=False)
    deliverer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deliverers.id"),
        nullable=True,
    )
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

    deliverer: Mapped[Deliverer | None] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    stock_movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="order",
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=True,
        index=True,
    )
    movement_type: Mapped[str] = mapped_column(String, nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    order: Mapped[Order | None] = relationship(back_populates="stock_movements")
