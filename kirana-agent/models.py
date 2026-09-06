"""
Database models for the Kirana Ops Agent.
Every stock/money mutation is auditable and idempotency-guarded here,
NOT in the prompt. This is the enforcement layer.
"""
import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, UniqueConstraint, CheckConstraint
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    unit = Column(String, nullable=False)  # kg, g, litre, ml, packet, dozen, piece
    is_loose = Column(Boolean, default=False)
    cost_price = Column(Float, nullable=False)   # per unit
    mrp = Column(Float, nullable=False)           # per unit, sell price
    gst_rate = Column(Float, nullable=False)      # e.g. 0, 5, 12, 18 (total, split CGST+SGST)
    hsn_code = Column(String, nullable=False)
    qty = Column(Float, nullable=False, default=0)
    reorder_level = Column(Float, nullable=False, default=0)

    __table_args__ = (CheckConstraint("qty >= 0", name="qty_non_negative"),)


class StockLedger(Base):
    """Append-only audit trail. Every qty change is a row here."""
    __tablename__ = "stock_ledger"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    delta_qty = Column(Float, nullable=False)  # + for receive, - for sale
    reason = Column(String, nullable=False)    # receive | sale | adjust | void_restock
    ref_id = Column(String)                    # e.g. bill id
    idempotency_key = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    phone = Column(String)


class KhataTransaction(Base):
    __tablename__ = "khata_transactions"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)  # credit | payment
    idempotency_key = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Bill(Base):
    __tablename__ = "bills"
    id = Column(Integer, primary_key=True)
    status = Column(String, nullable=False, default="draft")  # draft | final | void
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    payment_mode = Column(String)   # cash | upi | card | credit
    payment_ref = Column(String)
    finalize_idempotency_key = Column(String, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    finalized_at = Column(DateTime)

    items = relationship("BillItem", back_populates="bill", cascade="all, delete-orphan")


class BillItem(Base):
    __tablename__ = "bill_items"
    id = Column(Integer, primary_key=True)
    bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)   # snapshot of MRP at add-time
    gst_rate = Column(Float, nullable=False)      # snapshot of GST at add-time
    hsn_code = Column(String)

    bill = relationship("Bill", back_populates="items")
    product = relationship("Product")


class Preference(Base):
    """Owner-level, NOT chat-level. Survives /new and new sessions."""
    __tablename__ = "preferences"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


class ProcessedUpdate(Base):
    """Dedupe Telegram redelivered updates at the transport layer."""
    __tablename__ = "processed_updates"
    update_id = Column(Integer, primary_key=True)
    processed_at = Column(DateTime, default=datetime.datetime.utcnow)


def get_engine(db_path="kirana.db"):
    return create_engine(f"sqlite:///{db_path}", future=True)


def init_db(db_path="kirana.db"):
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine):
    return sessionmaker(bind=engine, future=True)
