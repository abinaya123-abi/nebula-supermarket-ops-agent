"""
Same control loop and same tools as claude_agent.py, but calls Google's
Gemini API instead of Anthropic's. This is a drop-in swap: bot.py and
cli_chat.py only need to change their import line.
"""
import json
import os
import google.generativeai as genai
import tools
from documents.invoice import generate_invoice_pdf
from documents.deck import generate_analysis_deck

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
MODEL_NAME = "gemini-3.6-flash"

SYSTEM_PROMPT = """You are the ops agent for an Indian kirana (grocery) store, chatting
with the shop owner on Telegram. You run the ENTIRE back office: inventory, billing,
GST, customer credit (khata), daily close, invoices and analysis decks.

Hard rules, never break these:
1. NEVER invent a product name, price, stock quantity, or GST rate. Always call a tool
   to look it up. If a product isn't found, say so and offer to add it.
2. If a request is ambiguous, ASK a clarifying question instead of guessing.
3. Bills are built incrementally: start_bill -> add_item_to_bill (repeat) ->
   [edits with remove_item_from_bill] -> finalize_bill. Stock is only decremented at
   finalize_bill, never before.
4. Every finalize_bill, receive_stock, add_credit, and record_payment call MUST include
   a stable idempotency_key you generate once per logical action (not per retry).
5. Never delete stock or products directly -- only add_product, receive_stock, and
   sales through finalize_bill change quantities.
6. If a tool raises an error, explain it plainly to the owner in one short sentence.
7. Respect standing owner preferences provided below.
8. Talk like a helpful assistant texting a busy shop owner: short, plain. Use Rs for money.
9. Use generate_invoice_pdf on a FINALIZED bill for invoices; generate_analysis_deck for decks.
10. When the owner states a standing preference, call set_preference to persist it.
"""

TOOLS = [
    {"name": "search_products", "description": "Search products by (partial) name.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_product", "description": "Get a single product by id.",
     "parameters": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "add_product", "description": "Add a brand-new product/SKU to the catalog.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "unit": {"type": "string"},
         "cost_price": {"type": "number"}, "mrp": {"type": "number"}, "gst_rate": {"type": "number"},
         "hsn_code": {"type": "string"}, "is_loose": {"type": "boolean"},
         "reorder_level": {"type": "number"}, "opening_qty": {"type": "number"},
     }, "required": ["name", "unit", "cost_price", "mrp", "gst_rate", "hsn_code"]}},
    {"name": "receive_stock", "description": "Record stock received from a supplier (stock-in).",
     "parameters": {"type": "object", "properties": {
         "product_id": {"type": "integer"}, "qty": {"type": "number"}, "cost_price": {"type": "number"},
         "idempotency_key": {"type": "string"},
     }, "required": ["product_id", "qty", "cost_price", "idempotency_key"]}},
    {"name": "check_stock", "description": "Check current stock for a product by id or name.",
     "parameters": {"type": "object", "properties": {"product_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "low_stock_report", "description": "List products at/below their reorder level.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "start_bill", "description": "Start a new draft bill. Returns bill_id.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_bill_draft", "description": "Fetch current state (items, totals, tax) of a bill, draft or final.",
     "parameters": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]}},
    {"name": "add_item_to_bill", "description": "Add a product+qty line to a draft bill. Enforces oversell guard.",
     "parameters": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "product_id": {"type": "integer"}, "qty": {"type": "number"},
     }, "required": ["bill_id", "product_id", "qty"]}},
    {"name": "remove_item_from_bill", "description": "Remove or reduce a line item on a draft bill.",
     "parameters": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "product_id": {"type": "integer"}, "qty": {"type": "number"},
     }, "required": ["bill_id", "product_id"]}},
    {"name": "set_bill_customer", "description": "Attach a customer to a draft bill (needed for credit/khata sales).",
     "parameters": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "customer_name": {"type": "string"},
     }, "required": ["bill_id", "customer_name"]}},
    {"name": "finalize_bill", "description": "Finalize a draft bill: decrements stock atomically, sets payment mode. Requires idempotency_key.",
     "parameters": {"type": "object", "properties": {
         "bill_id": {"type": "integer"}, "payment_mode": {"type": "string"},
         "payment_ref": {"type": "string"}, "idempotency_key": {"type": "string"},
     }, "required": ["bill_id", "payment_mode", "idempotency_key"]}},
    {"name": "void_bill", "description": "Cancel a draft bill (not allowed once finalized).",
     "parameters": {"type": "object", "properties": {"bill_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["bill_id"]}},
    {"name": "add_credit", "description": "Put an amount on a customer's khata (credit ledger).",
     "parameters": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "amount": {"type": "number"}, "idempotency_key": {"type": "string"},
     }, "required": ["customer_name", "amount", "idempotency_key"]}},
    {"name": "record_payment", "description": "Record a customer paying down their khata balance.",
     "parameters": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "amount": {"type": "number"}, "idempotency_key": {"type": "string"},
     }, "required": ["customer_name", "amount", "idempotency_key"]}},
    {"name": "get_balance", "description": "Get a customer's current khata balance.",
     "parameters": {"type": "object", "properties": {"customer_name": {"type": "string"}}, "required": ["customer_name"]}},
    {"name": "daily_close", "description": "Summarize a day's sales: total, tax collected, cash vs UPI split, top items.",
     "parameters": {"type": "object", "properties": {"date": {"type": "string"}}}},
    {"name": "sales_report", "description": "Sales summary over N days: daily totals, top items, GST collected, low stock.",
     "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "generate_invoice_pdf", "description": "Generate a GST-correct PDF invoice for a FINALIZED bill.",
     "parameters": {"type": "object", "properties": {"bill_id": {"type": "integer"}}, "required": ["bill_id"]}},
    {"name": "generate_analysis_deck", "description": "Generate a PPTX sales-analysis deck with charts, over N days.",
     "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "set_preference", "description": "Persist a standing owner preference (survives new chats).",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "get_preference", "description": "Read a standing owner preference.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]


def _dispatch(session, name, inp, shop_info):
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
        return {"error": "Unknown tool " + name}
    except tools.ToolError as e:
        return {"error": str(e)}
    except Exception as e:
        session.rollback()
        return {"error": "Internal error: " + str(e)}


def run_turn(session, conversation, user_text, shop_info):
    prefs = tools.all_preferences(session)
    prefs_block = "\n".join("- " + k + ": " + v for k, v in prefs.items()) or "(none set yet)"
    system_instruction = SYSTEM_PROMPT + "\n\nCurrent standing preferences:\n" + prefs_block + "\n\nShop info: " + str(shop_info)

    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=system_instruction,
        tools=[{"function_declarations": TOOLS}],
    )

    conversation.append({"role": "user", "parts": [{"text": user_text}]})
    file_paths = []

    for _ in range(8):
        response = model.generate_content(contents=conversation)
        candidate = response.candidates[0]
        parts = candidate.content.parts

        function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

        conversation.append({"role": "model", "parts": parts})

        if not function_calls:
            final_text = "".join(p.text for p in parts if getattr(p, "text", None))
            return final_text, file_paths, conversation

        response_parts = []
        for fc in function_calls:
            result = _dispatch(session, fc.name, dict(fc.args), shop_info)
            if isinstance(result, dict) and "file_path" in result:
                file_paths.append(result["file_path"])
            response_parts.append({
                "function_response": {"name": fc.name, "response": {"result": json.loads(json.dumps(result, default=str))}}
            })
        conversation.append({"role": "user", "parts": response_parts})

    return "Sorry, that took too many steps. Could you rephrase or break it into smaller asks?", file_paths, conversation
