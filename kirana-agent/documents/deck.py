"""Generates a business-analysis PPTX deck with real charts."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

BRAND_DARK = RGBColor(0x5B, 0x1A, 0x3A)
BRAND_ACCENT = RGBColor(0xB5, 0x40, 0x2E)


def _title_slide(prs, title, subtitle):
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    slide.shapes.title.text_frame.paragraphs[0].font.size = Pt(40)
    slide.placeholders[1].text = subtitle
    return slide


def _chart_slide(prs, title, image_path):
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    slide.shapes.add_picture(image_path, Inches(0.7), Inches(1.4), width=Inches(8.6))
    return slide


def _bullets_slide(prs, title, bullets):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    body = slide.placeholders[1].text_frame
    body.clear()
    for i, b in enumerate(bullets):
        p = body.paragraphs[0] if i == 0 else body.add_paragraph()
        p.text = b
        p.font.size = Pt(20)
    return slide


def generate_analysis_deck(report, shop_name, out_dir="decks", tmp_dir="tmp_charts"):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    prs = Presentation()

    _title_slide(prs, f"{shop_name} — Sales Analysis",
                 f"Last {report['range_days']} days · ₹{report['total_sales']:.0f} total sales")

    # Chart 1: daily sales trend
    days = sorted(report["daily_sales"].keys())
    values = [report["daily_sales"][d] for d in days]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(days, values, marker="o", color="#5B1A3A", linewidth=2)
    ax.set_title("Daily Sales (₹)")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    p1 = os.path.join(tmp_dir, "daily_sales.png")
    fig.savefig(p1, dpi=150)
    plt.close(fig)
    _chart_slide(prs, "Daily Sales Trend", p1)

    # Chart 2: top items
    if report["top_items"]:
        names = [n for n, _ in report["top_items"]]
        qtys = [q for _, q in report["top_items"]]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.barh(names[::-1], qtys[::-1], color="#B5402E")
        ax.set_title("Top Selling Items (by qty)")
        fig.tight_layout()
        p2 = os.path.join(tmp_dir, "top_items.png")
        fig.savefig(p2, dpi=150)
        plt.close(fig)
        _chart_slide(prs, "Top Selling Items", p2)

    # Bullets: GST + stock health
    low_stock_names = [p["name"] for p in report["low_stock"]]
    bullets = [
        f"GST collected in period: ₹{report['gst_collected']:.2f}",
        f"Total bills contributing: covers last {report['range_days']} days",
        f"Items at/below reorder level: {len(low_stock_names)}",
    ]
    if low_stock_names:
        bullets.append("Reorder soon: " + ", ".join(low_stock_names[:6]))
    _bullets_slide(prs, "GST & Stock Health", bullets)

    path = os.path.join(out_dir, "sales_analysis.pptx")
    prs.save(path)
    return path
