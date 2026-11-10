#!/usr/bin/env python3
"""Generate the PS-1 invoice test pack: seed data, invoice PDFs, ground truth and expected results.

Everything here is fictional test data for the Zamp AI Solutions Associate case study.

Usage:
    pip install reportlab pdfplumber pillow numpy
    python generate_pack.py            # writes into the folder this script sits in

Outputs (next to this script):
    seed.json               buyer, vendors and purchase orders (money in paise)
    invoices/*.pdf          the test invoices
    ground_truth/*.json     what is printed on each invoice: what a perfect extractor returns
    expected_results.csv    expected decision and reason code per case
    expected_balances.csv   expected PO-line balances after each case
"""
from __future__ import annotations

import csv
import io
import json
import random
import shutil
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pdfplumber
from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent
PAGE_W, PAGE_H = A4
SCAN_DPI = 150

# --------------------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------------------
BUYER = {"name": "Meridian Threads Pvt Ltd", "city": "Indore", "state": "Madhya Pradesh", "state_code": "23"}

VENDORS = {
    "A": {"id": "V-A", "name": "Tapivastra Knits LLP", "city": "Surat", "state": "Gujarat",
          "state_code": "24", "approved": True, "layout": "A"},
    "B": {"id": "V-B", "name": "Malwa Fabricraft Pvt Ltd", "city": "Indore", "state": "Madhya Pradesh",
          "state_code": "23", "approved": True, "layout": "B"},
    "C": {"id": "V-C", "name": "Noyyal Threadworks Pvt Ltd", "city": "Tiruppur", "state": "Tamil Nadu",
          "state_code": "33", "approved": True, "layout": "C"},
    # Not on the approved list: used only by B9.
    "X": {"id": None, "name": "Riverband Textiles LLP", "city": "Ahmedabad", "state": "Gujarat",
          "state_code": "24", "approved": False, "layout": "A"},
}

ITEMS = {
    "TS180": {"description": "Cotton crew T-shirt 180 GSM", "hsn": "6109"},
    "HD300": {"description": "Fleece hoodie 300 GSM", "hsn": "6110"},
    "PL220": {"description": "Polo shirt 220 GSM", "hsn": "6105"},
    "FRT": {"description": "Freight charges", "hsn": "9965"},
}

PURCHASE_ORDERS = [
    {"po": "PO-104", "vendor": "A", "po_date": "2026-10-20",
     "lines": [{"line_no": 1, "item": "TS180", "qty": 500, "unit_price_paise": 20000, "gst_rate_pct": 5}]},
    {"po": "PO-107", "vendor": "A", "po_date": "2026-10-28",
     "lines": [{"line_no": 1, "item": "TS180", "qty": 300, "unit_price_paise": 20000, "gst_rate_pct": 5}]},
    {"po": "PO-110", "vendor": "B", "po_date": "2026-10-22",
     "lines": [{"line_no": 1, "item": "HD300", "qty": 200, "unit_price_paise": 45000, "gst_rate_pct": 5}]},
    {"po": "PO-112", "vendor": "C", "po_date": "2026-10-25",
     "lines": [{"line_no": 1, "item": "TS180", "qty": 400, "unit_price_paise": 19500, "gst_rate_pct": 5},
               {"line_no": 2, "item": "PL220", "qty": 200, "unit_price_paise": 32000, "gst_rate_pct": 5}]},
]


def expected_tax_type(vendor_key: str) -> str:
    return "CGST+SGST" if VENDORS[vendor_key]["state_code"] == BUYER["state_code"] else "IGST"


# --------------------------------------------------------------------------------------
# Invoices. Each dict describes what is PRINTED on the document.
#   lines: (item, qty, unit_price_paise)
#   tax_type: override the printed tax type (B6 prints the wrong one)
#   printed_total_paise: override the printed total (B7)
#   illegible: set of (line_index, field) made unreadable on the scan (B2)
#   scan: None for a text PDF, "phone" for an image-only phone-style scan
# --------------------------------------------------------------------------------------
INVOICES = [
    dict(file="R1", vendor="A", invoice_no="INV-101", date="2026-11-02", po="PO-104", lines=[("TS180", 300, 20000)]),
    dict(file="R2", vendor="A", invoice_no="INV-102", date="2026-11-04", po="PO-104", lines=[("TS180", 160, 25000)]),
    # R3 is a byte-for-byte copy of R1 under a new filename (made after rendering).
    dict(file="R4", vendor="A", invoice_no="INV-101", date="2026-11-02", po="PO-104", lines=[("TS180", 250, 24000)]),
    dict(file="B1", vendor="A", invoice_no="INV-105", date="2026-11-03", po="PO-104", lines=[("TS180", 100, 20000)],
         scan="phone"),
    dict(file="B2", vendor="A", invoice_no="INV-106", date="2026-11-03", po="PO-104", lines=[("TS180", 100, 20000)],
         scan="phone", illegible={(0, "unit_price")}),
    dict(file="B3", vendor="A", invoice_no="INV-108", date="2026-11-05", po=None, lines=[("TS180", 50, 20000)]),
    dict(file="B4", vendor="B", invoice_no="MFC/457", date="2026-11-06", po=None, lines=[("HD300", 40, 45000)]),
    dict(file="B5", vendor="A", invoice_no="INV-111", date="2026-11-05", po="PO-104", lines=[("TS180", 250, 20000)]),
    dict(file="B6", vendor="A", invoice_no="INV-112", date="2026-11-06", po="PO-104", lines=[("TS180", 100, 20000)],
         tax_type="CGST+SGST"),
    dict(file="B7", vendor="A", invoice_no="INV-113", date="2026-11-06", po="PO-104", lines=[("TS180", 100, 20000)],
         printed_total_paise=2150000),
    dict(file="B8", vendor="A", invoice_no="INV-114", date="2026-11-07", po="PO-104", lines=[("TS180", 100, 20000)]),
    dict(file="B9", vendor="X", invoice_no="RB-2291", date="2026-11-07", po="PO-104", lines=[("TS180", 100, 20000)]),
    dict(file="B10", vendor="A", invoice_no="INV-116", date="2026-11-02", po="PO-104", lines=[("TS180", 300, 20000)]),
    dict(file="B11", vendor="C", invoice_no="NTW/2026-27/0418", date="2026-11-06", po="PO-112",
         lines=[("TS180", 150, 19500), ("PL220", 100, 32000)]),
    dict(file="B12a", vendor="A", invoice_no="INV-118", date="2026-11-08", po="PO-104", lines=[("TS180", 150, 20000)]),
    dict(file="B12b", vendor="A", invoice_no="INV-119", date="2026-11-09", po="PO-104", lines=[("TS180", 150, 20000)]),
    dict(file="C1", vendor="A", invoice_no="INV-115", date="2026-11-07", po="PO-104",
         lines=[("TS180", 100, 20000), ("FRT", 1, 250000)]),
    dict(file="C2", vendor="A", invoice_no="INV-102", date="2026-11-09", po="PO-104", lines=[("TS180", 160, 20000)]),
]

# Expected outcomes are written by hand (an independent oracle), not computed by any rules engine.
# setup: what must happen in the session before this case runs.
EXPECTED = [
    ("R1", "R1.pdf", "Fresh session", "Approved", "APPROVED",
     "300 x Rs 200 matches PO-104 line 1.", [("PO-104", 1, 200, 40000)]),
    ("R2", "R2.pdf", "After R1", "Held", "PRICE_VARIANCE",
     "Billed Rs 250 per shirt against Rs 200 on PO-104: Rs 8,000 more for 160 shirts.", [("PO-104", 1, 200, 40000)]),
    ("R3", "R3.pdf", "After R2", "Duplicate", "DUPLICATE_SAME_FILE",
     "Same file as R1 (INV-101), already approved; linked to that decision.", [("PO-104", 1, 200, 40000)]),
    ("R4", "R4.pdf", "After R3", "Held", "CHANGED_VERSION",
     "INV-101 was approved as 300 x Rs 200; this version says 250 x Rs 240.", [("PO-104", 1, 200, 40000)]),
    ("B1", "B1.pdf", "Fresh session", "Approved", "APPROVED",
     "Phone-style scan read; 100 x Rs 200 matches PO-104.", [("PO-104", 1, 400, 80000)]),
    ("B2", "B2.pdf", "Fresh session", "Held", "FIELD_UNREADABLE",
     "Unit price on line 1 is smudged. Amount / quantity would give Rs 200 but must not be used.",
     [("PO-104", 1, 500, 100000)]),
    ("B3", "B3.pdf", "Fresh session", "Held", "PO_AMBIGUOUS",
     "No PO number; PO-104 and PO-107 both fit.", [("PO-104", 1, 500, 100000), ("PO-107", 1, 300, 60000)]),
    ("B4", "B4.pdf", "Fresh session (unseen layout: keep aside during build)", "Approved", "APPROVED_PO_INFERRED",
     "No PO number; PO-110 is the only open PO from this vendor for this item.", [("PO-110", 1, 160, 72000)]),
    ("B5", "B5.pdf", "After R1", "Held", "QTY_EXCEEDS_REMAINING",
     "Bills 250 shirts; only 200 left on PO-104.", [("PO-104", 1, 200, 40000)]),
    ("B6", "B6.pdf", "Fresh session", "Held", "TAX_TYPE_MISMATCH",
     "Supplier state 24, place of supply 23: IGST applies, but CGST + SGST is charged.", [("PO-104", 1, 500, 100000)]),
    ("B7", "B7.pdf", "Fresh session", "Held", "TOTAL_MISMATCH",
     "Printed total Rs 21,500; lines and tax add up to Rs 21,000.", [("PO-104", 1, 500, 100000)]),
    ("B8", "B8.pdf", "Fresh session; upload B8.pdf twice at the same moment", "Approved + Duplicate",
     "APPROVED+DUPLICATE_SAME_FILE", "One approval, one duplicate; deducted once.", [("PO-104", 1, 400, 80000)]),
    ("B9", "B9.pdf", "Fresh session", "Held", "VENDOR_NOT_APPROVED",
     "Riverband Textiles LLP is not on the approved vendor list.", [("PO-104", 1, 500, 100000)]),
    ("B10", "B10.pdf", "After R1", "Held", "POSSIBLE_REISSUE",
     "Same vendor, PO, total and date as INV-101 under a new number; also exceeds the 200 shirts left.",
     [("PO-104", 1, 200, 40000)]),
    ("B11", "B11.pdf", "Fresh session", "Approved", "APPROVED",
     "Two lines match PO-112 lines 1 and 2; Rs 0.50 round-off is within tolerance.",
     [("PO-112", 1, 250, 48750), ("PO-112", 2, 100, 32000)]),
    ("B12", "B12a.pdf;B12b.pdf", "After R1; upload B12a.pdf and B12b.pdf at the same moment",
     "Approved + Held", "APPROVED+QTY_EXCEEDS_REMAINING",
     "Both bill 150 shirts against 200 left; only the first to commit is approved, the other is held.",
     [("PO-104", 1, 50, 10000)]),
    ("C1", "C1.pdf", "Fresh session", "Held", "CHARGE_NOT_ON_PO",
     "Freight charges of Rs 2,500 are not on PO-104.", [("PO-104", 1, 500, 100000)]),
    ("C2", "C2.pdf", "After R1 and R2; reviewer rejects R2 (INV-102)", "Approved", "APPROVED_REPLACEMENT",
     "Corrected INV-102 at Rs 200 replaces the rejected version.", [("PO-104", 1, 40, 8000)]),
]


# --------------------------------------------------------------------------------------
# Money and words
# --------------------------------------------------------------------------------------
def inr(paise: int) -> str:
    """Indian digit grouping: 1,00,000.00"""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}{s}.{p:02d}"


def pct_of(paise: int, rate_pct: Decimal) -> int:
    return int((Decimal(paise) * rate_pct / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int, hyphen: bool) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (("-" if hyphen else " ") + _ONES[o] if o else "")


def _three(n: int, hyphen: bool) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " Hundred")
    if r:
        parts.append(_two(r, hyphen))
    return " ".join(parts)


def words(rupees: int, hyphen: bool = True) -> str:
    """Indian system: crore, lakh, thousand."""
    if rupees == 0:
        return "Zero"
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        q, rupees = divmod(rupees, div)
        if q:
            parts.append(_three(q, hyphen) + " " + name)
    if rupees:
        parts.append(_three(rupees, hyphen))
    return " ".join(parts)


# --------------------------------------------------------------------------------------
# Building the printed facts of one invoice
# --------------------------------------------------------------------------------------
def build_facts(inv: dict) -> dict:
    v = VENDORS[inv["vendor"]]
    lines = []
    for item, qty, unit in inv["lines"]:
        lines.append({"item_code": item, "hsn": ITEMS[item]["hsn"], "description": ITEMS[item]["description"],
                      "qty": qty, "unit_price_paise": unit, "amount_paise": qty * unit})
    subtotal = sum(l["amount_paise"] for l in lines)
    tax_type = inv.get("tax_type") or expected_tax_type(inv["vendor"])
    rate = Decimal(5)
    if tax_type == "IGST":
        taxes = [{"type": "IGST", "rate_pct": "5", "amount_paise": pct_of(subtotal, rate)}]
    else:
        half = rate / 2
        taxes = [{"type": "CGST", "rate_pct": "2.5", "amount_paise": pct_of(subtotal, half)},
                 {"type": "SGST", "rate_pct": "2.5", "amount_paise": pct_of(subtotal, half)}]
    exact_total = subtotal + sum(t["amount_paise"] for t in taxes)
    round_off = 0
    if v["layout"] == "C":  # Tally-style invoices round the total to whole rupees
        rounded = int((Decimal(exact_total) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100
        round_off = rounded - exact_total
    total = inv.get("printed_total_paise", exact_total + round_off)
    return {
        "file": inv["file"] + ".pdf",
        "layout": v["layout"],
        "scan": inv.get("scan"),
        "vendor_name": v["name"],
        "supplier_city": v["city"],
        "supplier_state": v["state"],
        "supplier_state_code": v["state_code"],
        "buyer_name": BUYER["name"],
        "place_of_supply_state_code": BUYER["state_code"],
        "invoice_no": inv["invoice_no"],
        "invoice_date": inv["date"],
        "po_ref": inv["po"],
        "currency": "INR",
        "lines": lines,
        "subtotal_paise": subtotal,
        "taxes": taxes,
        "round_off_paise": round_off,
        "total_paise": total,
        "amount_in_words": f"{words(total // 100, hyphen=v['layout'] != 'C')} Only",
    }


# --------------------------------------------------------------------------------------
# Layouts. Each returns {(line_index, field): (x0, y0, x1, y1)} in PDF points (origin bottom-left)
# --------------------------------------------------------------------------------------
INK = colors.HexColor("#1f2d3a")
RULE = colors.HexColor("#9aa5ae")


def _right(c, x, y, text, font="Helvetica", size=10):
    c.setFont(font, size)
    c.drawRightString(x, y, text)
    w = stringWidth(text, font, size)
    return (x - w - 2, y - 3, x + 2, y + size)


def layout_a(c, f) -> dict:
    """Vendor A style: 'SALES INVOICE', ruled item table with HSN column."""
    boxes = {}
    x0 = 50
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(x0, PAGE_H - 70, f["vendor_name"])
    c.setFont("Helvetica", 9.5)
    c.drawString(x0, PAGE_H - 87, f"{f['supplier_city']}, {f['supplier_state']}  |  State code: "
                                  f"{f['supplier_state_code']}  |  INDIA")
    c.setFont("Helvetica-Bold", 17)
    c.drawString(x0, PAGE_H - 128, "SALES INVOICE")

    d = f["invoice_date"]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    date_txt = f"{d[8:10]} {months[int(d[5:7]) - 1]} {d[:4]}"
    meta = [("Invoice number", f["invoice_no"]), ("Invoice date", date_txt)]
    if f["po_ref"]:
        meta.append(("Purchase order", f["po_ref"]))
    meta += [("Bill to", f"{BUYER['name']}, {BUYER['city']}, {BUYER['state']}"),
             ("Place of supply", f"{BUYER['state']} ({BUYER['state_code']})"),
             ("Currency", "INR")]
    y = PAGE_H - 155
    for label, val in meta:
        c.setFont("Helvetica", 10)
        c.drawString(x0, y, f"{label}:")
        c.drawString(x0 + 95, y, val)
        y -= 16

    # Item table
    y -= 14
    cols = {"item": x0, "hsn": x0 + 52, "desc": x0 + 100, "qty_r": 395, "unit_r": 470, "amt_r": 545}
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(x0, y + 14, 545, y + 14)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(cols["item"], y, "Item")
    c.drawString(cols["hsn"], y, "HSN")
    c.drawString(cols["desc"], y, "Description")
    c.drawRightString(cols["qty_r"], y, "Qty")
    c.drawRightString(cols["unit_r"], y, "Unit price (INR)")
    c.drawRightString(cols["amt_r"], y, "Amount (INR)")
    c.line(x0, y - 6, 545, y - 6)
    y -= 22
    for i, ln in enumerate(f["lines"]):
        c.setFont("Helvetica", 10)
        c.drawString(cols["item"], y, ln["item_code"])
        c.drawString(cols["hsn"], y, ln["hsn"])
        c.drawString(cols["desc"], y, ln["description"])
        boxes[(i, "qty")] = _right(c, cols["qty_r"], y, str(ln["qty"]))
        boxes[(i, "unit_price")] = _right(c, cols["unit_r"], y, inr(ln["unit_price_paise"]))
        boxes[(i, "amount")] = _right(c, cols["amt_r"], y, inr(ln["amount_paise"]))
        y -= 20
    c.line(x0, y + 8, 545, y + 8)

    # Totals
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(360, y, "Subtotal")
    _right(c, 545, y, inr(f["subtotal_paise"]))
    for t in f["taxes"]:
        y -= 16
        c.setFont("Helvetica", 10)
        c.drawString(360, y, f"{t['type']} {t['rate_pct']}%")
        _right(c, 545, y, inr(t["amount_paise"]))
    y -= 24
    c.setFont("Helvetica-Bold", 12)
    c.drawString(360, y, "Invoice total (INR)")
    _right(c, 545, y, inr(f["total_paise"]), "Helvetica-Bold", 12)
    y -= 30
    c.setFont("Helvetica", 9.5)
    c.drawString(x0, y, f"Amount in words: Rupees {f['amount_in_words']}")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#6b7680"))
    c.drawString(x0, 40, "Fictional assessment document - not a real invoice")
    return boxes


def layout_b(c, f) -> dict:
    """Vendor B style: centred 'TAX INVOICE', stacked label-value item blocks, dd-mm-yyyy dates, no PO."""
    boxes = {}
    cx = PAGE_W / 2
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(cx, PAGE_H - 60, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(cx, PAGE_H - 84, f["vendor_name"])
    c.setFont("Helvetica", 9.5)
    c.drawCentredString(cx, PAGE_H - 99, f"{f['supplier_city']}, {f['supplier_state']} - State code "
                                         f"{f['supplier_state_code']}")
    d = f["invoice_date"]
    x0, xv = 70, 210
    rows = [("Bill No.", f["invoice_no"]), ("Dated", f"{d[8:10]}-{d[5:7]}-{d[:4]}"),
            ("Buyer", f"{BUYER['name']}, {BUYER['city']}"),
            ("Place of supply", f"{BUYER['state_code']}-{BUYER['state']}"), ("Payment terms", "30 days")]
    if f["po_ref"]:
        rows.insert(2, ("Order ref.", f["po_ref"]))
    y = PAGE_H - 140
    for label, val in rows:
        c.setFont("Helvetica", 10)
        c.drawString(x0, y, label)
        c.drawString(xv, y, val)
        y -= 17
    y -= 10
    for i, ln in enumerate(f["lines"]):
        c.setStrokeColor(RULE)
        c.line(x0, y + 12, 525, y + 12)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x0, y - 4, f"Goods {i + 1}")
        y -= 22
        for label, key, val in (("Description of goods", None, f"{ln['item_code']} {ln['description']}"),
                                ("HSN", None, ln["hsn"]),
                                ("Rate (INR)", "unit_price", inr(ln["unit_price_paise"])),
                                ("Quantity", "qty", f"{ln['qty']} pcs"),
                                ("Extended value (INR)", "amount", inr(ln["amount_paise"]))):
            c.setFont("Helvetica", 10)
            c.drawString(x0, y, label)
            c.drawString(xv, y, val)
            if key:
                w = stringWidth(val, "Helvetica", 10)
                boxes[(i, key)] = (xv - 2, y - 3, xv + w + 2, y + 10)
            y -= 16
        y -= 6
    c.line(x0, y + 8, 525, y + 8)
    y -= 12
    pairs = [("Taxable value", f["subtotal_paise"])] + \
            [(f"{t['type']} @ {t['rate_pct']}%", t["amount_paise"]) for t in f["taxes"]]
    for label, amt in pairs:
        c.setFont("Helvetica", 10)
        c.drawString(330, y, label)
        _right(c, 525, y, inr(amt))
        y -= 16
    y -= 8
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(330, y, "Grand total (INR)")
    _right(c, 525, y, inr(f["total_paise"]), "Helvetica-Bold", 11.5)
    y -= 30
    c.setFont("Helvetica", 9.5)
    c.drawString(x0, y, f"Rupees {f['amount_in_words']}")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#6b7680"))
    c.drawCentredString(cx, 40, "Fictional assessment document - not a real invoice")
    return boxes


def layout_c(c, f) -> dict:
    """Vendor C style: boxed Tally-like invoice, Sl No / HSN/SAC / per columns, round-off line, d-Mon-yy dates."""
    boxes = {}
    L, R, T = 40, PAGE_W - 40, PAGE_H - 40
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def tally_date(iso):
        return f"{int(iso[8:10])}-{months[int(iso[5:7]) - 1]}-{iso[2:4]}"

    c.setFillColor(colors.black)
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.7)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(PAGE_W / 2, T - 4, "TAX INVOICE")
    top = T - 14
    c.rect(L, 80, R - L, top - 80)
    mid = L + (R - L) * 0.52
    # Supplier box
    c.line(L, top - 92, R, top - 92)
    c.line(mid, top, mid, top - 182)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(L + 6, top - 16, f["vendor_name"])
    c.setFont("Helvetica", 9)
    c.drawString(L + 6, top - 30, f"{f['supplier_city']}, {f['supplier_state']}")
    c.drawString(L + 6, top - 43, f"State Name : {f['supplier_state']}, Code : {f['supplier_state_code']}")
    # Right grid
    po_date = next((p["po_date"] for p in PURCHASE_ORDERS if p["po"] == f["po_ref"]), None)
    grid = [("Invoice No.", f["invoice_no"]), ("Dated", tally_date(f["invoice_date"])),
            ("Buyer's Order No.", f["po_ref"] or ""), ("Dated", tally_date(po_date) if po_date else ""),
            ("Terms of Delivery", "Ex-factory"), ("Mode of Despatch", "Road")]
    gy = top
    for i, (label, val) in enumerate(grid):
        if i:
            c.line(mid, gy, R, gy)
        c.setFont("Helvetica", 7.5)
        c.drawString(mid + 5, gy - 10, label)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(mid + 5, gy - 22, val)
        gy -= 30.33
    # Buyer box
    c.setFont("Helvetica", 7.5)
    c.drawString(L + 6, top - 104, "Buyer (Bill to)")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(L + 6, top - 118, BUYER["name"])
    c.setFont("Helvetica", 9)
    c.drawString(L + 6, top - 131, BUYER["city"])
    c.drawString(L + 6, top - 144, f"State Name : {BUYER['state']}, Code : {BUYER['state_code']}")
    c.drawString(L + 6, top - 157, f"Place of Supply : {BUYER['state']}")
    # Item table
    ty = top - 182
    c.line(L, ty, R, ty)
    xs = {"sl": L, "desc": L + 30, "hsn": L + 250, "qty": L + 310, "rate": L + 375, "per": L + 430, "amt": L + 460}
    for k in ("desc", "hsn", "qty", "rate", "per", "amt"):
        c.line(xs[k], ty, xs[k], 150)
    c.setFont("Helvetica-Bold", 8.5)
    hy = ty - 14
    c.drawString(xs["sl"] + 4, hy, "Sl No")
    c.drawString(xs["desc"] + 4, hy, "Description of Goods")
    c.drawString(xs["hsn"] + 4, hy, "HSN/SAC")
    c.drawRightString(xs["rate"] - 4, hy, "Quantity")
    c.drawRightString(xs["per"] - 4, hy, "Rate")
    c.drawString(xs["per"] + 4, hy, "per")
    c.drawRightString(R - 4, hy, "Amount")
    c.line(L, ty - 22, R, ty - 22)
    y = ty - 40
    for i, ln in enumerate(f["lines"]):
        c.setFont("Helvetica", 9.5)
        c.drawString(xs["sl"] + 8, y, str(i + 1))
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(xs["desc"] + 4, y, ln["description"])
        c.setFont("Helvetica", 8)
        c.drawString(xs["desc"] + 4, y - 11, f"Item code: {ln['item_code']}")
        c.setFont("Helvetica", 9.5)
        c.drawString(xs["hsn"] + 4, y, ln["hsn"])
        boxes[(i, "qty")] = _right(c, xs["rate"] - 4, y, f"{ln['qty']} pcs", "Helvetica-Bold", 9.5)
        boxes[(i, "unit_price")] = _right(c, xs["per"] - 4, y, inr(ln["unit_price_paise"]), "Helvetica", 9.5)
        c.setFont("Helvetica", 9.5)
        c.drawString(xs["per"] + 4, y, "pcs")
        boxes[(i, "amount")] = _right(c, R - 4, y, inr(ln["amount_paise"]), "Helvetica-Bold", 9.5)
        y -= 34
    c.line(xs["amt"], y + 14, R, y + 14)
    y -= 2
    for t in f["taxes"]:
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(xs["hsn"] - 6, y, f"{t['type']} @ {t['rate_pct']}%")
        _right(c, R - 4, y, inr(t["amount_paise"]), "Helvetica-Bold", 9.5)
        y -= 16
    if f["round_off_paise"]:
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(xs["hsn"] - 6, y, "Round Off")
        _right(c, R - 4, y, inr(f["round_off_paise"]), "Helvetica-Bold", 9.5)
    # Total row
    c.line(L, 150, R, 150)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawRightString(xs["hsn"] - 6, 136, "Total")
    c.drawRightString(xs["rate"] - 4, 136, f"{sum(l['qty'] for l in f['lines'])} pcs")
    _right(c, R - 4, 136, f"INR {inr(f['total_paise'])}", "Helvetica-Bold", 10)
    c.line(L, 128, R, 128)
    c.setFont("Helvetica", 7.5)
    c.drawString(L + 6, 116, "Amount Chargeable (in words)")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(L + 6, 103, f"INR {f['amount_in_words']}")
    c.setFont("Helvetica", 7.5)
    c.drawString(L + 6, 88, f"Taxable value INR {inr(f['subtotal_paise'])}")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawCentredString(PAGE_W / 2, 66, "This is a Computer Generated Invoice")
    c.drawCentredString(PAGE_W / 2, 54, "Fictional assessment document - not a real invoice")
    return boxes


LAYOUTS = {"A": layout_a, "B": layout_b, "C": layout_c}


def render_text_pdf(facts: dict) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)  # invariant=1: identical bytes on every run
    c.setTitle(f"{facts['vendor_name']} {facts['invoice_no']}")
    c.setAuthor("Fictional test data")
    boxes = LAYOUTS[facts["layout"]](c, facts)
    c.showPage()
    c.save()
    return buf.getvalue(), boxes


# --------------------------------------------------------------------------------------
# Scan simulation
# --------------------------------------------------------------------------------------
def phone_scan(pdf_bytes: bytes, seed: int, smudge_boxes: list | None = None) -> bytes:
    rng = np.random.default_rng(seed)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        img = pdf.pages[0].to_image(resolution=SCAN_DPI).original.convert("L")
    arr = np.asarray(img).astype(np.float32)
    h, w = arr.shape
    scale = SCAN_DPI / 72.0

    if smudge_boxes:
        for (x0, y0, x1, y1) in smudge_boxes:
            # PDF points (origin bottom-left) -> pixels (origin top-left), with a margin
            # generous margin so the blot fades out inside the region (no hard rectangle edge)
            px0, px1 = int((x0 - 16) * scale), int((x1 + 16) * scale)
            py0, py1 = int((PAGE_H - y1 - 14) * scale), int((PAGE_H - y0 + 14) * scale)
            orig = arr[py0:py1, px0:px1].copy()
            blurred = np.asarray(Image.fromarray(orig.clip(0, 255).astype(np.uint8))
                                 .filter(ImageFilter.GaussianBlur(5))).astype(np.float32)
            rh, rw = py1 - py0, px1 - px0
            # an irregular ink blot: overlapping ellipses centred on the digits, soft edges
            mask_img = Image.new("L", (rw, rh), 0)
            draw = ImageDraw.Draw(mask_img)
            for _ in range(9):
                cx, cy = rng.uniform(0.32, 0.68) * rw, rng.uniform(0.40, 0.60) * rh
                rx, ry = rng.uniform(0.16, 0.30) * rw, rng.uniform(0.18, 0.32) * rh
                draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
            mask = np.asarray(mask_img.filter(ImageFilter.GaussianBlur(6))).astype(np.float32) / 255.0
            stained = blurred * 0.45 + 95 * 0.55          # smeared and darkened
            arr[py0:py1, px0:px1] = orig * (1 - mask) + stained * mask

    # faded ink, uneven lighting, sensor noise
    yy, xx = np.mgrid[0:h, 0:w]
    light = 0.90 + 0.07 * (xx / w) - 0.05 * (yy / h)
    arr = (255 - (255 - arr) * 0.88) * light
    arr += rng.normal(0, 7, arr.shape)
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = img.rotate(1.3, resample=Image.BICUBIC, expand=True, fillcolor=205)
    jpg = io.BytesIO()
    img.save(jpg, "JPEG", quality=55)
    img = Image.open(io.BytesIO(jpg.getvalue())).convert("RGB")
    out = io.BytesIO()
    fixed = time.strptime("2026-11-10", "%Y-%m-%d")  # pinned so reruns give identical bytes
    img.save(out, "PDF", resolution=SCAN_DPI, creationDate=fixed, modDate=fixed, producer="Fictional test data")
    return out.getvalue()


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> None:
    inv_dir, gt_dir = OUT / "invoices", OUT / "ground_truth"
    for d in (inv_dir, gt_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    for i, inv in enumerate(INVOICES):
        facts = build_facts(inv)
        pdf_bytes, boxes = render_text_pdf(facts)
        if inv.get("scan") == "phone":
            smudge = [boxes[k] for k in sorted(inv.get("illegible", set()))]
            pdf_bytes = phone_scan(pdf_bytes, seed=1000 + i, smudge_boxes=smudge)
        (inv_dir / facts["file"]).write_bytes(pdf_bytes)

        gt = dict(facts)
        for (li, field) in inv.get("illegible", set()):
            gt["lines"][li][field + "_paise" if field == "unit_price" else field] = None
            gt.setdefault("illegible_fields", []).append({"line_index": li, "field": field})
        gt["notes"] = "Values as printed. null = not readable on the document; an extractor must not infer it."
        (gt_dir / (inv["file"] + ".json")).write_text(json.dumps(gt, indent=2) + "\n")

    # R3: byte-for-byte copy of R1 under a new filename
    shutil.copyfile(inv_dir / "R1.pdf", inv_dir / "R3.pdf")
    r3 = json.loads((gt_dir / "R1.json").read_text())
    r3["file"] = "R3.pdf"
    r3["notes"] = "Byte-for-byte copy of R1.pdf under a new filename."
    (gt_dir / "R3.json").write_text(json.dumps(r3, indent=2) + "\n")

    seed = {
        "note": "Fictional test data. Money in paise (integer). Load a fresh copy into every demo session.",
        "buyer": BUYER,
        "vendors": [{k: v for k, v in VENDORS[key].items() if k != "layout"} for key in ("A", "B", "C")],
        "purchase_orders": [
            {"po": p["po"], "vendor_id": VENDORS[p["vendor"]]["id"], "po_date": p["po_date"], "currency": "INR",
             "status": "open", "expected_tax_type": expected_tax_type(p["vendor"]),
             "lines": [{**ln, "description": ITEMS[ln["item"]]["description"], "hsn": ITEMS[ln["item"]]["hsn"]}
                       for ln in p["lines"]]}
            for p in PURCHASE_ORDERS],
        "ledger": [],
    }
    (OUT / "seed.json").write_text(json.dumps(seed, indent=2) + "\n")

    with open(OUT / "expected_results.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["case", "files", "setup", "expected_decision", "reason_code", "headline"])
        for case, files, setup, decision, code, headline, _ in EXPECTED:
            wr.writerow([case, files, setup, decision, code, headline])
    with open(OUT / "expected_balances.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["case", "po", "line_no", "qty_left", "pre_tax_left_inr"])
        for case, *_, bals in EXPECTED:
            for po, line_no, qty_left, amt_left in bals:
                wr.writerow([case, po, line_no, qty_left, amt_left])
    print(f"Wrote {len(list(inv_dir.glob('*.pdf')))} invoices to {inv_dir}")


if __name__ == "__main__":
    random.seed(0)
    main()
