"""
The agent brain: a control loop on top of Claude's native tool-use.
This IS the harness -- observe (user msg + tool results) -> reason ->
act (call tool) -> feed result back -> continue, chaining multiple
tool calls in one turn until the model produces a final text reply.

We deliberately do NOT use a keyword/regex router. Every intent in
section 3 of the brief is resolved by the model choosing from the
tool list below.
"""
import json
import os
import anthropic
import tools
from documents.invoice import generate_invoice_pdf
from documents.deck import generate_analysis_deck

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are the ops agent for an Indian kirana (grocery) store, chatting
with the shop owner on Telegram. You run the ENTIRE back office: inventory, billing,
GST, customer credit (khata), daily close, invoices and analysis decks.

Hard rules, never break these:
1. NEVER invent a product name, price, stock quantity, or GST rate. Always call a tool
   to look it up. If a product isn't found, say so and offer to add it.
2. If a request is ambiguous (e.g. "add atta" when multiple atta products/brands could
   exist, or size isn't specified), ASK a clarifying question instead of guessing.
3. Bills are built incrementally: start_bill -> add_item_to_bill (repeat) ->
   [edits with remove_item_from_bill] -> finalize_bill. Stock is only decremented at
   finalize_bill, never before.
4. Every finalize_bill, receive_stock, add_credit, and record_payment call MUST include
   a stable idempotency_key you generate once per logical action (not per retry).
   If you are unsure whether an action already happened, re-check state with a read
   tool (get_bill_draft, check_stock, get_balance) before acting again.
5. Never delete stock or products directly -- only add_product, receive_stock, and
   sales through finalize_bill change quantities.
6. If a tool raises an error (oversell, below-cost, missing khata, duplicate), explain
   it plainly to the owner in one short sentence -- don't retry blindly.
7. Respect standing owner preferences (default payment mode, preferred brand, shop
   name/GSTIN for invoices) which are provided to you below. Apply them silently
   unless the owner's message overrides them for this one action.
8. Talk like a helpful assistant texting a busy shop owner: short, plain, no corporate
   tone, no long lists unless asked. Use ₹ for money.
9. When the owner asks for a bill "as a PDF" or "invoice", use generate_invoice_pdf on
   a FINALIZED bill. When they ask for an "analysis deck" / "sales deck" / "PPTX",
   use generate_analysis_deck.
10. When the owner states a standing preference ("always use UPI unless I say cash",
    "default atta is Aashirvaad 5kg", "shop name is X"), call set_preference to persist
    it. This must survive a /new chat -- it is not just conversation memory.
"""

TOOLS = [
    {"name": "search_products", "description": "Search products by (partial) name.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_product", "description": "Get a single product by id.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "add_product", "description": "Add a brand-new product/SKU to the catalog.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "unit": {"type": "string", "enum": ["kg", "g", "litre", "ml", "packet", "dozen", "piece"]},
         "cost_price": {"type": "number"}, "mrp": {"type": "number"}, "gst_rate": {"type": "number"},
         "hsn_code": {"type": "string"}, "is_loose": {"type": "boolean"},
         "reorder_level": {"type": "number"}, "opening_qty": {"type": "number"},
     }, "required": ["name", "unit", "cost_price", "mrp", "gst_rate", "hsn_code"]}},
    {"name": "receive_stock", "description": "Record stock received from a supplier (stock-in).",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "integer"}, "qty": {"type": "number"}, "cost_price": {"type": "number"},
         "idempotency_key": {"type": "string"},
     }, "required": ["product_id", "qty", "cost_price", "idempotency_key"]}},
    {"name": "check_stock", "description": "Check current stock for a product by id or name.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "low_stock_report", "description": "List products at/below their reorder level.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "start_bill", "description": "Start a new draft bill. Returns bill_id.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_bill_draft", "description": "Fetch current state (items, totals, tax) of a bill, draft or final.",
     "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]}},
    {"name": "add_item_to_bill", "description": "Add a product+qty line to a draft bill. Enforces oversell guard.",
     "input_schema": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "product_id": {"type": "integer"}, "qty": {"type": "number"},
     }, "required": ["bill_id", "product_id", "qty"]}},
    {"name": "remove_item_from_bill", "description": "Remove or reduce a line item on a draft bill.",
     "input_schema": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "product_id": {"type": "integer"}, "qty": {"type": "number"},
     }, "required": ["bill_id", "product_id"]}},
    {"name": "set_bill_customer", "description": "Attach a customer to a draft bill (needed for credit/khata sales).",
     "input_schema": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "customer_name": {"type": "string"},
     }, "required": ["bill_id", "customer_name"]}},
    {"name": "finalize_bill", "description": "Finalize a draft bill: decrements stock atomically, sets payment mode. Requires idempotency_key.",
     "input_schema": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "payment_mode": {"type": "string", "enum": ["cash", "upi", "card", "credit"]},
         "payment_ref": {"type": "string"}, "idempotency_key": {"type": "string"},
     }, "required": ["bill_id", "payment_mode", "idempotency_key"]}},
    {"name": "void_bill", "description": "Cancel a draft bill (not allowed once finalized).",
     "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["bill_id"]}},
    {"name": "add_credit", "description": "Put an amount on a customer's khata (credit ledger).",
     "input_schema": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "amount": {"type": "number"}, "idempotency_key": {"type": "string"},
     }, "required": ["customer_name", "amount", "idempotency_key"]}},
    {"name": "record_payment", "description": "Record a customer paying down their khata balance.",
     "input_schema": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "amount": {"type": "number"}, "idempotency_key": {"type": "string"},
     }, "required": ["customer_name", "amount", "idempotency_key"]}},
    {"name": "get_balance", "description": "Get a customer's current khata balance.",
     "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}}, "required": ["customer_name"]}},
    {"name": "daily_close", "description": "Summarize a day's sales: total, tax collected, cash vs UPI split, top items.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD, optional, defaults to today"}}}},
    {"name": "sales_report", "description": "Sales summary over N days: daily totals, top items, GST collected, low stock.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "generate_invoice_pdf", "description": "Generate a GST-correct PDF invoice for a FINALIZED bill. Returns a file path.",
     "input_schema": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]}},
    {"name": "generate_analysis_deck", "description": "Generate a PPTX sales-analysis deck with charts, over N days.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "set_preference", "description": "Persist a standing owner preference (survives new chats).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "get_preference", "description": "Read a standing owner preference.",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]


def _dispatch(session, name, inp, shop_info):
    """Runs the actual tool function and returns a JSON-serializable result."""
    try:
        if name == "search_products":
            return tools.search_products(session, **inp)
        if name == "get_product":
            return tools.get_product(session, **inp)
        if name == "add_product":
            return tools.add_product(session, **inp)
        if name == "receive_stock":
            return tools.receive_stock(session, **inp)
        if name == "check_stock":
            return tools.check_stock(session, **inp)
        if name == "low_stock_report":
            return tools.low_stock_report(session)
        if name == "start_bill":
            return tools.start_bill(session)
        if name == "get_bill_draft":
            return tools.get_bill_draft(session, **inp)
        if name == "add_item_to_bill":
            return tools.add_item_to_bill(session, **inp)
        if name == "remove_item_from_bill":
            return tools.remove_item_from_bill(session, **inp)
        if name == "set_bill_customer":
            return tools.set_bill_customer(session, **inp)
        if name == "finalize_bill":
            return tools.finalize_bill(session, **inp)
        if name == "void_bill":
            return tools.void_bill(session, **inp)
        if name == "add_credit":
            return tools.add_credit(session, **inp)
        if name == "record_payment":
            return tools.record_payment(session, **inp)
        if name == "get_balance":
            return tools.get_balance(session, **inp)
        if name == "daily_close":
            return tools.daily_close(session, **inp)
        if name == "sales_report":
            return tools.sales_report(session, **inp)
        if name == "generate_invoice_pdf":
            bill = tools.get_bill_draft(session, inp["bill_id"])
            if bill["status"] != "final":
                raise tools.ToolError("Bill must be finalized before generating an invoice.")
            path = generate_invoice_pdf(bill, shop_info)
            return {"file_path": path}
        if name == "generate_analysis_deck":
            report = tools.sales_report(session, days=inp.get("days", 7))
            path = generate_analysis_deck(report, shop_info.get("name", "Kirana Store"))
            return {"file_path": path}
        if name == "set_preference":
            return tools.set_preference(session, **inp)
        if name == "get_preference":
            return {"value": tools.get_preference(session, **inp)}
        return {"error": f"Unknown tool {name}"}
    except tools.ToolError as e:
        return {"error": str(e)}
    except Exception as e:
        session.rollback()
        return {"error": f"Internal error: {e}"}


def run_turn(session, conversation, user_text, shop_info):
    """
    conversation: list of {"role": ..., "content": ...} for this chat (in-memory,
    reset on /new). Preferences (persistent memory) are injected fresh each turn.
    Returns (assistant_text, file_paths_to_send, updated_conversation).
    """
    prefs = tools.all_preferences(session)
    prefs_block = "\n".join(f"- {k}: {v}" for k, v in prefs.items()) or "(none set yet)"
    system = SYSTEM_PROMPT + f"\n\nCurrent standing preferences:\n{prefs_block}\n\nShop info: {shop_info}"

    conversation.append({"role": "user", "content": user_text})
    file_paths = []

    for _ in range(8):  # cap tool-call chain length per turn
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=system,
            tools=TOOLS, messages=conversation,
        )
        conversation.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            return final_text, file_paths, conversation

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            result = _dispatch(session, block.name, block.input, shop_info)
            if isinstance(result, dict) and "file_path" in result:
                file_paths.append(result["file_path"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        conversation.append({"role": "user", "content": tool_results})

    return "Sorry, that took too many steps — could you rephrase or break it into smaller asks?", file_paths, conversation
