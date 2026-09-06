# Kirana Ops Agent

Telegram bot: **@your_bot_username_here** ← fill in after BotFather setup, keep it running.

## Harness

Custom control loop directly on Claude's native tool-use (Messages API), rather than a
higher-level framework. This is architecturally identical to what agent SDKs wrap:
observe (user message + prior tool results) → reason (model call) → act (execute the
tool the model chose) → feed the result back into the conversation → repeat until the
model returns plain text. See `claude_agent.py::run_turn`. Chosen for full control over
the idempotency and DB-transaction guarantees the "hard parts" require — those live in
`tools.py`, not in the prompt, so they hold regardless of what the model decides to do.

## Skill / tool design

Tools are grouped into five capability areas the model composes freely:
- **Inventory**: search_products, get_product, add_product, receive_stock, check_stock, low_stock_report
- **Billing**: start_bill, add_item_to_bill, remove_item_from_bill, set_bill_customer, get_bill_draft, finalize_bill, void_bill
- **Khata (credit)**: add_credit, record_payment, get_balance
- **Analytics**: daily_close, sales_report
- **Documents**: generate_invoice_pdf, generate_analysis_deck
- **Preferences**: set_preference, get_preference

Each tool is intentionally thin (one DB operation's worth of responsibility) so the
model does the composing — e.g. "make a bill: 2kg sugar, 1 atta, UPI" becomes
`start_bill → add_item_to_bill ×2 → finalize_bill`, chained in a single turn.

## How each hard part is solved

- **Grounding**: every price/stock/GST fact the model states must have come from a
  tool's JSON return value; the system prompt explicitly forbids fabrication.
- **Oversell guard**: enforced in `add_item_to_bill` and re-checked in `finalize_bill`
  against live DB stock — not a prompt instruction.
- **GST correctness**: `gst_breakup()` splits each line's rate into CGST/SGST halves,
  rounds per-line, and the bill total sums rounded lines (standard Indian invoicing
  convention). HSN + slab are snapshotted onto each bill line at add-time.
- **Multi-turn bills**: a draft `Bill`/`BillItem` row is the source of truth across
  turns; the model just passes `bill_id` back each time. Stock only moves in
  `finalize_bill`.
- **Idempotency**: two layers — (1) Telegram's `update_id` is deduped in
  `ProcessedUpdate` before the agent even runs, protecting against Telegram's own
  redelivery; (2) every mutating tool (`finalize_bill`, `receive_stock`, `add_credit`,
  `record_payment`) takes a model-generated `idempotency_key` with a DB unique
  constraint, protecting against the model retrying a tool call mid-turn.
- **Concurrency**: SQLite + SQLAlchemy session commits are transactional per request;
  the oversell check is re-verified at `finalize_bill` time (not just at
  `add_item_to_bill` time) to close the race between two bills drafted concurrently.
- **Guardrails**: below-cost sale is rejected in `add_item_to_bill`; there is no
  delete-stock/delete-product tool at all (only additive `receive_stock` and audited
  sales); `record_payment` raises if the customer has no khata history.
- **Real artifacts**: `generate_invoice_pdf` reads a *finalized* bill's live GST
  breakup from the DB and renders it with reportlab; `generate_analysis_deck` pulls
  `sales_report()` and renders real matplotlib charts (daily sales trend, top items)
  into a python-pptx deck.
- **Cross-session memory**: `Preference` rows are keyed globally (owner-level), not by
  chat id, and re-injected into the system prompt on every turn — so `/new` clears the
  visible conversation but not what the store remembers about how the owner works.

## Running locally

```bash
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
python seed_data.py    # loads real SKUs with correct GST slabs
pytest test_tools.py -v  # sanity-check the hard parts before touching Telegram
python bot.py
```

## Deployment

Deployed on [Railway/Render/Fly.io — fill in], polling mode, SQLite file persisted on
a mounted volume so stock/khata/bills/preferences survive restarts.
