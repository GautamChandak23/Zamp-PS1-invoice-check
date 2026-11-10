#!/usr/bin/env python3
"""Check the PS-1 test pack is internally consistent. Run after generate_pack.py.

Checks:
  1. R3 is byte-identical to R1 and every other file is unique.
  2. Every text PDF prints the values in its ground truth; scans have no text layer.
  3. OCR (if tesseract is installed): B1's unit price is readable, B2's is not.
  4. Arithmetic in every ground truth adds up, except the deliberate error in B7.
  5. Tax type matches supplier state vs place of supply, except the deliberate error in B6.
  6. Expected balances follow from the approvals each case implies (independent recomputation).
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent
INV, GT = ROOT / "invoices", ROOT / "ground_truth"
seed = json.loads((ROOT / "seed.json").read_text())
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def inr(paise: int) -> str:
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
    return f"{s}.{p:02d}"


gts = {p.stem: json.loads(p.read_text()) for p in sorted(GT.glob("*.json"))}

# 1. hashes
hashes = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(INV.glob("*.pdf"))}
check(hashes["R1"] == hashes["R3"], "R3 is not byte-identical to R1")
others = [h for k, h in hashes.items() if k != "R3"]
check(len(set(others)) == len(others), "two different cases share identical bytes")
check(set(hashes) == set(gts), "invoices and ground truth files do not match one to one")

# 2. text layers
for name, gt in gts.items():
    with pdfplumber.open(INV / f"{name}.pdf") as pdf:
        text = "".join((pg.extract_text() or "") for pg in pdf.pages)
    if gt["scan"]:
        check(text.strip() == "", f"{name}: scan should have no text layer")
        continue
    must = [gt["invoice_no"], inr(gt["subtotal_paise"]), inr(gt["total_paise"]), gt["vendor_name"]]
    must += [inr(t["amount_paise"]) for t in gt["taxes"]]
    for ln in gt["lines"]:
        must += [ln["item_code"], ln["hsn"], str(ln["qty"]), inr(ln["unit_price_paise"]), inr(ln["amount_paise"])]
    if gt["po_ref"]:
        must.append(gt["po_ref"])
    else:
        check("PO-" not in text, f"{name}: should print no PO reference")
    for m in must:
        check(m in text, f"{name}: text layer is missing '{m}'")

# 3. OCR on the scans
if shutil.which("tesseract"):
    def ocr(name: str) -> str:
        with pdfplumber.open(INV / f"{name}.pdf") as pdf, tempfile.TemporaryDirectory() as td:
            img = Path(td) / "p.png"
            pdf.pages[0].to_image(resolution=200).original.save(img)
            return subprocess.run(["tesseract", str(img), "-", "--psm", "6"], capture_output=True, text=True).stdout
    b1, b2 = ocr("B1"), ocr("B2")
    check("200.00" in b1.split(), "B1: OCR cannot read the unit price 200.00 (scan too degraded)")
    check("21,000.00" in b1, "B1: OCR cannot read the total")
    check("200.00" not in b2.split(), "B2: OCR can still read the smudged unit price")
    check("20,000.00" in b2, "B2: OCR cannot read the line amount (smudge too wide)")
else:
    print("tesseract not installed: OCR checks skipped")

# 4 and 5. arithmetic and tax type
vendor_by_name = {v["name"]: v for v in seed["vendors"]}
for name, gt in gts.items():
    lines = gt["lines"]
    for ln in lines:
        if ln["unit_price_paise"] is not None:
            check(ln["qty"] * ln["unit_price_paise"] == ln["amount_paise"], f"{name}: qty x price != amount")
    check(sum(l["amount_paise"] for l in lines) == gt["subtotal_paise"], f"{name}: lines do not sum to subtotal")
    for t in gt["taxes"]:
        want = Decimal(gt["subtotal_paise"]) * Decimal(t["rate_pct"]) / 100
        check(abs(Decimal(t["amount_paise"]) - want) <= 1, f"{name}: {t['type']} amount wrong for its rate")
    computed = gt["subtotal_paise"] + sum(t["amount_paise"] for t in gt["taxes"]) + gt["round_off_paise"]
    check(abs(gt["round_off_paise"]) < 100, f"{name}: round-off of a rupee or more")
    if name == "B7":
        check(computed != gt["total_paise"], "B7: should carry a deliberate total mismatch")
    else:
        check(computed == gt["total_paise"], f"{name}: subtotal + tax + round-off != printed total")
    same_state = gt["supplier_state_code"] == gt["place_of_supply_state_code"]
    printed = "+".join(t["type"] for t in gt["taxes"])
    correct = "CGST+SGST" if same_state else "IGST"
    if name == "B6":
        check(printed != correct, "B6: should carry a deliberate tax-type error")
    else:
        check(printed == correct, f"{name}: tax type {printed} but {correct} applies")

# 6. balances, recomputed from the approvals each case implies
approved_after = {
    "R1": ["R1"], "R2": ["R1"], "R3": ["R1"], "R4": ["R1"], "B1": ["B1"], "B2": [], "B3": [], "B4": ["B4"],
    "B5": ["R1"], "B6": [], "B7": [], "B8": ["B8"], "B9": [], "B10": ["R1"], "B11": ["B11"],
    "B12": ["R1", "B12a"], "C1": [], "C2": ["R1", "C2"],
}
inferred_po = {"B4": "PO-110"}  # B4 prints no PO; the expected result infers PO-110
po_lines = {(p["po"], l["line_no"]): l for p in seed["purchase_orders"] for l in p["lines"]}
with open(ROOT / "expected_balances.csv") as fh:
    rows = list(csv.DictReader(fh))
for r in rows:
    key = (r["po"], int(r["line_no"]))
    line = po_lines[key]
    qty_used = amt_used = 0
    for inv_name in approved_after[r["case"]]:
        gt = gts[inv_name]
        po = gt["po_ref"] or inferred_po.get(inv_name)
        for ln in gt["lines"]:
            if po == key[0] and ln["item_code"] == line["item"]:
                qty_used += ln["qty"]
                amt_used += ln["amount_paise"]
    want_qty = line["qty"] - qty_used
    want_amt = (line["qty"] * line["unit_price_paise"] - amt_used) // 100
    check(int(r["qty_left"]) == want_qty, f"{r['case']} {key}: qty_left {r['qty_left']} != {want_qty}")
    check(int(r["pre_tax_left_inr"]) == want_amt, f"{r['case']} {key}: amount left {r['pre_tax_left_inr']} != {want_amt}")
with open(ROOT / "expected_results.csv") as fh:
    cases = {r["case"] for r in csv.DictReader(fh)}
check(cases == set(approved_after), "expected_results.csv and the balance map cover different cases")

if fails:
    print(f"{len(fails)} problem(s):")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print(f"All checks passed: {len(gts)} invoices, {len(rows)} balance rows, {len(cases)} cases.")
