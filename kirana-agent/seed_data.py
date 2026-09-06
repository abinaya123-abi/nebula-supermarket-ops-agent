"""Run once: python seed_data.py"""
import os
from dotenv import load_dotenv
from models import init_db, get_session_factory
import tools

load_dotenv()

engine = init_db(os.environ.get("DB_PATH", "kirana.db"))
Session = get_session_factory(engine)
s = Session()

PRODUCTS = [
    # name, unit, is_loose, cost, mrp, gst, hsn, reorder, opening_qty
    ("Aashirvaad Atta 5kg", "packet", False, 210, 245, 5, "1101", 5, 30),
    ("Tata Salt 1kg", "packet", False, 18, 24, 5, "2501", 10, 50),
    ("Amul Butter 100g", "packet", False, 52, 62, 12, "0405", 10, 40),
    ("Fortune Sunflower Oil 1L", "packet", False, 130, 155, 5, "1512", 8, 25),
    ("Maggi 70g", "packet", False, 11, 14, 12, "1902", 20, 100),
    ("Parle-G", "packet", False, 8, 10, 18, "1905", 20, 100),
    ("Surf Excel 1kg", "packet", False, 95, 115, 18, "3402", 5, 20),
    ("Sugar (loose)", "kg", True, 38, 45, 0, "1701", 15, 80),
    ("Rice (loose)", "kg", True, 40, 52, 0, "1006", 15, 80),
    ("Toor Dal (loose)", "kg", True, 110, 135, 0, "0713", 10, 40),
]

for name, unit, loose, cost, mrp, gst, hsn, reorder, opening in PRODUCTS:
    try:
        tools.add_product(s, name=name, unit=unit, cost_price=cost, mrp=mrp,
                           gst_rate=gst, hsn_code=hsn, is_loose=loose,
                           reorder_level=reorder, opening_qty=opening)
        print(f"Added {name}")
    except tools.ToolError as e:
        print(f"Skip {name}: {e}")

print("Seed complete.")
