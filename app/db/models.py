"""SQLAlchemy ORM models for the OTC drug recommendation system."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Drug(Base):
    __tablename__ = "drugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generic_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    brand_names: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="感冒退烧")
    active_ingredients: Mapped[list] = mapped_column(JSON, default=list)
    dosage_form: Mapped[str] = mapped_column(String(50), nullable=False)
    strength: Mapped[str] = mapped_column(String(50), nullable=False)
    otc_type: Mapped[str] = mapped_column(String(10), nullable=False, default="甲类")
    indication_summary: Mapped[str] = mapped_column(Text, nullable=False)
    usage_adult: Mapped[str] = mapped_column(Text, nullable=False)
    usage_child: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_elderly: Mapped[str | None] = mapped_column(Text, nullable=True)

    inventory_items: Mapped[list["Inventory"]] = relationship(
        back_populates="drug", cascade="all, delete-orphan"
    )


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drugs.id"), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(100), nullable=False)
    specification: Mapped[str] = mapped_column(String(100), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    shelf_location: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    drug: Mapped["Drug"] = relationship(back_populates="inventory_items")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True,
        default=lambda: str(uuid.uuid4()),
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["Session"] = relationship(back_populates="messages")


class SafetyLog(Base):
    __tablename__ = "safety_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_rules: Mapped[list] = mapped_column(JSON, default=list)
    input_slots: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
