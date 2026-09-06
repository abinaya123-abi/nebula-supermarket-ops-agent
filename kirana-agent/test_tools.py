"""
Run: pytest test_tools.py -v
Covers the 'hard parts' at the tool layer, independent of the LLM/bot.
"""
import pytest
from models import init_db, get_session_factory
import tools


@pytest.fixture
def session():
    engine = init_db(":memory:")
    Session = get_session_factory(engine)
    s = Session()
    yield s
    s.close()


def _seed_product(s, qty=10, cost=10, mrp=15, gst=12):
    return tools.add_product(s, name="Test Item", unit="piece", cost_price=cost,
                              mrp=mrp, gst_rate=gst, hsn_code="9999", opening_qty=qty)


def test_oversell_guard(session):
    p = _seed_product(session, qty=6)
    bill = tools.start_bill(session)
    with pytest.raises(tools.ToolError):
        tools.add_item_to_bill(session, bill["bill_id"], p["id"], 10)


def test_finalize_idempotent(session):
    p = _seed_product(session, qty=10)
    bill = tools.start_bill(session)
    tools.add_item_to_bill(session, bill["bill_id"], p["id"], 2)
    key = "test-key-1"
    r1 = tools.finalize_bill(session, bill["bill_id"], "cash", key)
    r2 = tools.finalize_bill(session, bill["bill_id"], "cash", key)
    assert r2["status"] == "already_processed"
    updated = tools.get_product(session, p["id"])
    assert updated["qty"] == 8  # decremented only once


def test_below_cost_guard(session):
    p = _seed_product(session, qty=10, cost=20, mrp=15)
    bill = tools.start_bill(session)
    with pytest.raises(tools.ToolError):
        tools.add_item_to_bill(session, bill["bill_id"], p["id"], 1)


def test_gst_split(session):
    amount = 100.0
    cgst, sgst, total = tools.gst_breakup(amount, 12)
    assert cgst == 6.0 and sgst == 6.0 and total == 12.0


def test_khata_cycle(session):
    tools.add_credit(session, "Ramesh", 500, "credit-1")
    bal = tools.get_balance(session, "Ramesh")
    assert bal["balance"] == 500
    tools.record_payment(session, "Ramesh", 300, "payment-1")
    bal = tools.get_balance(session, "Ramesh")
    assert bal["balance"] == 200


def test_payment_without_khata_fails(session):
    with pytest.raises(tools.ToolError):
        tools.record_payment(session, "Ghost Customer", 100, "x")


def test_multiturn_bill_edit(session):
    p1 = _seed_product(session, qty=10)
    bill = tools.start_bill(session)
    tools.add_item_to_bill(session, bill["bill_id"], p1["id"], 5)
    tools.remove_item_from_bill(session, bill["bill_id"], p1["id"], 2)
    draft = tools.get_bill_draft(session, bill["bill_id"])
    assert draft["items"][0]["qty"] == 3
