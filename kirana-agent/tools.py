"""
All store operations live here as plain Python functions.
These get wrapped as Claude tool calls in claude_agent.py.
Business rules (oversell guard, GST math, idempotency, below-cost guard)
are enforced HERE, at the data layer -- never trusted to the prompt.
"""
import datetime
from http.client import ALREADY_REPORTED
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from models import Product, StockLedger, Customer, KhataTransaction, Bill, BillItem, Preference


class ToolError(Exception):
    """Raised for business-rule violations. Message is shown to the model/owner."""
    pass


def _round2(x):
    return round(x + 1e-9, 2)


def gst_breakup(amount, gst_rate):
    """Given a line amount and total GST rate, return (cgst, sgst, total_tax)."""
    half = gst_rate / 2.0
    cgst = _round2(amount * half / 100.0)
    sgst = _round2(amount * half / 100.0)
    return cgst, sgst, _round2(cgst + sgst)


# ---------------------------------------------------------------- INVENTORY

def search_products(session, query: str):
    q = f"%{query.lower()}%"
    rows = session.execute(
        select(Product).where(Product.name.ilike(q))
    ).scalars().all()
    return [_product_dict(p) for p in rows]


def get_product(session, product_id: int):
    p = session.get(Product, product_id)
    if not p:
        raise ToolError(f"No product with id {product_id}")
    return _product_dict(p)


def _product_dict(p: Product):
    return {
        "id": p.id, "name": p.name, "unit": p.unit, "is_loose": p.is_loose,
        "cost_price": p.cost_price, "mrp": p.mrp, "gst_rate": p.gst_rate,
        "hsn_code": p.hsn_code, "qty": p.qty, "reorder_level": p.reorder_level,
    }


def add_product(session, name, unit, cost_price, mrp, gst_rate, hsn_code,
                 is_loose=False, reorder_level=0, opening_qty=0):
    existing = session.execute(select(Product).where(Product.name == name)).scalar_one_or_none()
    if existing:
        raise ToolError(f"Product '{name}' already exists (id {existing.id}). Use receive_stock to add quantity.")
    p = Product(name=name, unit=unit, cost_price=cost_price, mrp=mrp,
                gst_rate=gst_rate, hsn_code=hsn_code, is_loose=is_loose,
                reorder_level=reorder_level, qty=opening_qty)
    session.add(p)
    session.commit()
    return _product_dict(p)


def receive_stock(session, product_id: int, qty: float, cost_price: float,
                   idempotency_key: str):
    """Stock-in. Atomic + idempotent."""
    existing = session.execute(
        select(StockLedger).where(StockLedger.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        p = session.get(Product, product_id)
        return {"status": "already_processed", "product": _product_dict(p)}

    p = session.get(Product, product_id)
    if not p:
        raise ToolError(f"No product with id {product_id}")

    p.qty = p.qty + qty
    p.cost_price = cost_price  # update to latest cost
    session.add(StockLedger(product_id=product_id, delta_qty=qty, reason="receive",
                             idempotency_key=idempotency_key))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ToolError("Duplicate stock receipt detected; ignored.")
    return {"status": "ok", "product": _product_dict(p)}


def check_stock(session, product_id: int = None, name: str = None):
    if product_id:
        return get_product(session, product_id)
    rows = search_products(session, name or "")
    if not rows:
        raise ToolError("Product not found.")
    return rows


def low_stock_report(session):
    rows = session.execute(
        select(Product).where(Product.qty <= Product.reorder_level)
    ).scalars().all()
    return [_product_dict(p) for p in rows]


# ------------------------------------------------------------------ BILLING

def start_bill(session):
    b = Bill(status="draft")
    session.add(b)
    session.commit()
    return {"bill_id": b.id, "status": b.status}


def get_bill_draft(session, bill_id: int):
    b = session.get(Bill, bill_id)
    if not b:
        raise ToolError(f"No bill {bill_id}")
    return _bill_dict(b)


def _bill_dict(b: Bill):
    items = []
    subtotal = 0.0
    tax_total = 0.0
    for it in b.items:
        line_amount = _round2(it.qty * it.unit_price)
        cgst, sgst, tax = gst_breakup(line_amount, it.gst_rate)
        subtotal += line_amount
        tax_total += tax
        items.append({
            "item_id": it.id, "product_id": it.product_id,
            "name": it.product.name if it.product else None,
            "qty": it.qty, "unit_price": it.unit_price, "line_amount": line_amount,
            "gst_rate": it.gst_rate, "cgst": cgst, "sgst": sgst, "hsn_code": it.hsn_code,
        })
    return {
        "bill_id": b.id, "status": b.status, "items": items,
        "subtotal": _round2(subtotal), "tax_total": _round2(tax_total),
        "grand_total": _round2(subtotal + tax_total),
        "payment_mode": b.payment_mode, "customer_id": b.customer_id,
    }


def add_item_to_bill(session, bill_id: int, product_id: int, qty: float):
    b = session.get(Bill, bill_id)
    if not b or b.status != "draft":
        raise ToolError("Bill not found or not editable (already finalized/void).")
    p = session.get(Product, product_id)
    if not p:
        raise ToolError(f"No product with id {product_id}")

    # OVERSELL GUARD: sum what's already on this draft bill for this product + new qty
    already_on_bill = sum(i.qty for i in b.items if i.product_id == product_id)
    if already_on_bill + qty > p.qty:
        raise ToolError(
            f"Cannot add {qty} {p.unit} of {p.name}: only {p.qty} in stock "
            f"({already_on_bill} already on this bill)."
        )
    # BELOW-COST GUARD
    if p.mrp < p.cost_price:
        raise ToolError(f"{p.name}'s sell price is below cost price. Refusing to bill until price is fixed.")

    existing_item = next((i for i in b.items if i.product_id == product_id), None)
    if existing_item:
        existing_item.qty += qty
    else:
        session.add(BillItem(bill_id=bill_id, product_id=product_id, qty=qty,
                              unit_price=p.mrp, gst_rate=p.gst_rate, hsn_code=p.hsn_code))
    session.commit()
    return get_bill_draft(session, bill_id)


def remove_item_from_bill(session, bill_id: int, product_id: int, qty: float = None):
    b = session.get(Bill, bill_id)
    if not b or b.status != "draft":
        raise ToolError("Bill not found or not editable.")
    item = next((i for i in b.items if i.product_id == product_id), None)
    if not item:
        raise ToolError("That item isn't on the bill.")
    if qty is None or qty >= item.qty:
        session.delete(item)
    else:
        item.qty -= qty
    session.commit()
    return get_bill_draft(session, bill_id)


def set_bill_customer(session, bill_id: int, customer_name: str):
    cust = get_or_create_customer(session, customer_name)
    b = session.get(Bill, bill_id)
    b.customer_id = cust["id"]
    session.commit()
    return get_bill_draft(session, bill_id)


def finalize_bill(session, bill_id: int, payment_mode: str, idempotency_key: str,
                   payment_ref: str = None):
    """
    Decrements stock ATOMICALLY and only once, guarded by idempotency_key.
    payment_mode 'credit' requires a customer already set on the bill (khata sale).
    """
    already = session.execute(
        select(Bill).where(Bill.finalize_idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if already:
        return {"status": "already_processed", **_bill_dict(already)}

    b = session.get(Bill, bill_id)
    if not b:
        raise ToolError(f"No bill {bill_id}")
    if b.status != "draft":
        raise ToolError(f"Bill {bill_id} is already {b.status}.")
    if not b.items:
        raise ToolError("Cannot finalize an empty bill.")
    if payment_mode == "credit" and not b.customer_id:
        raise ToolError("Credit sale needs a customer on the bill first (use set_bill_customer).")

    # Re-check oversell guard at finalize time too (in case of concurrent sales)
    for it in b.items:
        p = session.get(Product, it.product_id)
        if it.qty > p.qty:
            raise ToolError(f"Cannot finalize: only {p.qty} {p.unit} of {p.name} left in stock now.")

    for it in b.items:
        p = session.get(Product, it.product_id)
        p.qty = p.qty - it.qty
        session.add(StockLedger(
            product_id=p.id, delta_qty=-it.qty, reason="sale", ref_id=str(bill_id),
            idempotency_key=f"{idempotency_key}:sale:{p.id}"
        ))

    b.payment_mode = payment_mode
    b.payment_ref = payment_ref
    b.status = "final"
    b.finalize_idempotency_key = idempotency_key
    b.finalized_at = datetime.datetime.utcnow()

    if payment_mode == "credit":
        total = _bill_dict(b)["grand_total"]
        session.add(KhataTransaction(
            customer_id=b.customer_id, amount=total, type="credit",
            idempotency_key=f"{idempotency_key}:khata"
        ))

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ToolError("Duplicate finalize detected; bill was not double-billed.")
    return {"status": "ok", **_bill_dict(b)}


def void_bill(session, bill_id: int, reason: str = ""):
    b = session.get(Bill, bill_id)
    if not b:
        raise ToolError("Bill not found.")
    if b.status == "final":
        raise ToolError("Cannot void a finalized bill; restock manually via receive_stock if needed.")
    b.status = "void"
    session.commit()
    if ALREADY_REPORTED:
            return {**_bill_dict(already), "status": "already_processed"} # type: ignore


# -------------------------------------------------------------------- KHATA

def get_or_create_customer(session, name: str, phone: str = None):
    c = session.execute(select(Customer).where(Customer.name.ilike(name))).scalar_one_or_none()
    if not c:
        c = Customer(name=name, phone=phone)
        session.add(c)
        session.commit()
    return {"id": c.id, "name": c.name, "phone": c.phone}


def add_credit(session, customer_name: str, amount: float, idempotency_key: str):
    existing = session.execute(
        select(KhataTransaction).where(KhataTransaction.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return {"status": "already_processed"}
    cust = get_or_create_customer(session, customer_name)
    session.add(KhataTransaction(customer_id=cust["id"], amount=amount, type="credit",
                                  idempotency_key=idempotency_key))
    session.commit()
    return {"status": "ok", **get_balance(session, customer_name)}


def record_payment(session, customer_name: str, amount: float, idempotency_key: str):
    cust = session.execute(select(Customer).where(Customer.name.ilike(customer_name))).scalar_one_or_none()
    if not cust:
        raise ToolError(f"No khata found for '{customer_name}'. Can't record a payment against a customer that doesn't exist.")
    existing = session.execute(
        select(KhataTransaction).where(KhataTransaction.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return {"status": "already_processed"}
    session.add(KhataTransaction(customer_id=cust.id, amount=amount, type="payment",
                                  idempotency_key=idempotency_key))
    session.commit()
    return {"status": "ok", **get_balance(session, customer_name)}


def get_balance(session, customer_name: str):
    cust = session.execute(select(Customer).where(Customer.name.ilike(customer_name))).scalar_one_or_none()
    if not cust:
        raise ToolError(f"No khata found for '{customer_name}'.")
    txns = session.execute(select(KhataTransaction).where(KhataTransaction.customer_id == cust.id)).scalars().all()
    balance = sum(t.amount if t.type == "credit" else -t.amount for t in txns)
    return {"customer": cust.name, "balance": _round2(balance)}


# --------------------------------------------------------------- ANALYTICS

def daily_close(session, date: str = None):
    """date format YYYY-MM-DD; defaults to today (UTC)."""
    day = datetime.date.fromisoformat(date) if date else datetime.date.today()
    start = datetime.datetime.combine(day, datetime.time.min)
    end = datetime.datetime.combine(day, datetime.time.max)
    bills = session.execute(
        select(Bill).where(Bill.status == "final", Bill.finalized_at >= start, Bill.finalized_at <= end)
    ).scalars().all()

    total_sales = 0.0
    tax_collected = 0.0
    by_mode = {}
    item_qty = {}
    for b in bills:
        bd = _bill_dict(b)
        total_sales += bd["grand_total"]
        tax_collected += bd["tax_total"]
        by_mode[b.payment_mode] = by_mode.get(b.payment_mode, 0) + bd["grand_total"]
        for it in bd["items"]:
            item_qty[it["name"]] = item_qty.get(it["name"], 0) + it["qty"]

    top_items = sorted(item_qty.items(), key=lambda x: -x[1])[:5]
    return {
        "date": str(day), "num_bills": len(bills),
        "total_sales": _round2(total_sales), "tax_collected": _round2(tax_collected),
        "by_payment_mode": by_mode, "top_items": top_items,
    }


def sales_report(session, days: int = 7):
    end = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=days)
    bills = session.execute(
        select(Bill).where(Bill.status == "final", Bill.finalized_at >= start)
    ).scalars().all()
    daily = {}
    item_qty = {}
    gst_total = 0.0
    for b in bills:
        bd = _bill_dict(b)
        key = b.finalized_at.date().isoformat()
        daily[key] = daily.get(key, 0) + bd["grand_total"]
        gst_total += bd["tax_total"]
        for it in bd["items"]:
            item_qty[it["name"]] = item_qty.get(it["name"], 0) + it["qty"]
    top_items = sorted(item_qty.items(), key=lambda x: -x[1])[:8]
    low_stock = low_stock_report(session)
    return {
        "range_days": days, "daily_sales": daily, "top_items": top_items,
        "gst_collected": _round2(gst_total), "low_stock": low_stock,
        "total_sales": _round2(sum(daily.values())),
    }


# ----------------------------------------------------------------- PREFS

def set_preference(session, key: str, value: str):
    p = session.get(Preference, key)
    if p:
        p.value = value
    else:
        session.add(Preference(key=key, value=value))
    session.commit()
    return {"key": key, "value": value}


def get_preference(session, key: str, default=None):
    p = session.get(Preference, key)
    return p.value if p else default


def all_preferences(session):
    rows = session.execute(select(Preference)).scalars().all()
    return {p.key: p.value for p in rows}
