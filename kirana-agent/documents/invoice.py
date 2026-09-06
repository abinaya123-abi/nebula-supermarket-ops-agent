"""Generates a clean, GST-correct PDF invoice for a finalized bill."""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def generate_invoice_pdf(bill_dict, shop_info, out_dir="invoices"):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"invoice_{bill_dict['bill_id']}.pdf")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Heading1"], fontSize=18)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9)

    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    elems = []

    elems.append(Paragraph(shop_info.get("name", "Kirana Store"), title_style))
    elems.append(Paragraph(shop_info.get("address", ""), small))
    elems.append(Paragraph(f"GSTIN: {shop_info.get('gstin', '')}", small))
    elems.append(Spacer(1, 8))
    elems.append(Paragraph(f"<b>Tax Invoice</b> — Bill #{bill_dict['bill_id']}", styles["Heading2"]))
    elems.append(Paragraph(f"Payment: {bill_dict.get('payment_mode', '-')}", small))
    elems.append(Spacer(1, 10))

    data = [["Item", "HSN", "Qty", "Rate (₹)", "Amount (₹)", "CGST", "SGST", "GST%"]]
    for it in bill_dict["items"]:
        data.append([
            it["name"], it.get("hsn_code", "-"), f'{it["qty"]}',
            f'{it["unit_price"]:.2f}', f'{it["line_amount"]:.2f}',
            f'{it["cgst"]:.2f}', f'{it["sgst"]:.2f}', f'{it["gst_rate"]}%',
        ])

    table = Table(data, repeatRows=1, colWidths=[105, 45, 35, 55, 60, 45, 45, 35])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5b1a3a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f0f2")]),
    ]))
    elems.append(table)
    elems.append(Spacer(1, 14))

    totals_data = [
        ["Subtotal", f"₹{bill_dict['subtotal']:.2f}"],
        ["Total Tax (CGST+SGST)", f"₹{bill_dict['tax_total']:.2f}"],
        ["Grand Total", f"₹{bill_dict['grand_total']:.2f}"],
    ]
    ttable = Table(totals_data, colWidths=[150, 100], hAlign="RIGHT")
    ttable.setStyle(TableStyle([
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, 2), (-1, 2), 1, colors.black),
    ]))
    elems.append(ttable)
    elems.append(Spacer(1, 20))
    elems.append(Paragraph("Thank you for shopping with us!", small))

    doc.build(elems)
    return path
