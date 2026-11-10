"""Step 07b checks: the page check catches a model that guesses, hallucinates or fails. No API key needed:
the model call is replaced, everything else (PDF reading, OCR, value checks, engine) runs for real."""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import extract  # noqa: E402
from engine import Ledger  # noqa: E402

PACK = ROOT / "data" / "test_pack"
SEED = json.loads((PACK / "seed.json").read_text())
GT = {p.stem: json.loads(p.read_text()) for p in (PACK / "ground_truth").glob("*.json")}
PDF = {p.stem: p.read_bytes() for p in (PACK / "invoices").glob("*.pdf")}


class Scripted:
    """Stands in for the model: returns a faithful reading, optionally edited, or raises."""
    name = "scripted model"

    def __init__(self, key=None, edit=None, raw=None, error=None):
        self.key, self.edit, self.raw, self.error = key, edit, raw, error

    def extract(self, pdf):
        if self.error:
            raise extract.ProviderError(self.error)
        if self.raw is not None:
            return self.raw
        out = extract.printed_form(GT[self.key])
        if self.edit:
            self.edit(out)
        return out


def run(key, provider, ledger=None, sid=None, pdf=None):
    ledger = ledger or LEDGER
    sid = sid or ledger.start(SEED)
    res = extract.run(pdf or PDF[key], provider)
    doc = res.doc if res.doc is not None else {}
    return res, ledger.process(sid, doc, pdf or PDF[key], processing_error=res.processing_error)


def check_all(ledger):
    global LEDGER
    LEDGER = ledger
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if not cond and detail else ""))

    # 1. A model that fills in B2's smudged price by arithmetic
    res, out = run("B2", Scripted("B2", lambda o: o["lines"][0].update(unit_price="200.00")))
    ev = next(e for e in res.evidence if e.field == "line 1 unit price")
    case("B2: model guesses 200.00 for the smudged price -> not confirmed, Held FIELD_UNREADABLE",
         ev.status == "not found" and out["status"] == "Held" and out["reason"] == "FIELD_UNREADABLE", out["detail"])

    # 2. A model that 'reads' the agreed PO price instead of what R2 prints
    def agree(o):
        o["lines"][0].update(unit_price="200.00", amount="32,000.00")
        o.update(subtotal="32,000.00", total="33,600.00")
        o["taxes"][0]["amount"] = "1,600.00"
    sid = LEDGER.start(SEED)
    run("R1", Scripted("R1"), sid=sid)
    res, out = run("R2", Scripted("R2", agree), sid=sid)
    case("R2: model hallucinates the PO price -> values not on the page, never Approved",
         out["status"] == "Held" and out["reason"] == "FIELD_UNREADABLE", f"{out['status']} {out['reason']}")

    # 3. A model that quotes a PO number the page does not show
    res, out = run("R1", Scripted("R1", lambda o: o.update(po_ref="PO-107")))
    case("R1: model claims PO-107 (not printed) -> Held, PO number unverified, not inferred",
         out["status"] == "Held" and "PO number" in out["detail"], out["detail"])

    # 4. A line the page does not have
    res, out = run("R1", Scripted("R1", lambda o: o["lines"].append(
        {"item_code": "TS180", "description": "Cotton crew T-shirt 180 GSM", "qty": "50", "unit_price": "200.00",
         "amount": "10,000.00"})))
    case("R1: model invents an extra line -> Held", out["status"] == "Held", f"{out['status']} {out['reason']}")

    # 5. Different but valid ways of writing the same printed numbers
    def formats(o):
        o["lines"][0].update(qty="300 pcs", unit_price="Rs. 200", amount="₹ 60000")
        o.update(subtotal="60000", total="INR 63,000.00")
    res, out = run("R1", Scripted("R1", formats))
    case("R1: '300 pcs', 'Rs. 200', '₹ 60000' -> parsed and confirmed, Approved", out["status"] == "Approved",
         f"{out['status']} {out['detail']}")

    # 6. Buyer read with its address / punctuation (fix 2), on the unseen layout B
    res, out = run("B4", Scripted("B4", lambda o: o.update(buyer_name="Meridian Threads Pvt. Ltd., Indore")))
    case("B4: buyer 'Meridian Threads Pvt. Ltd., Indore' -> Approved, PO-110 inferred",
         out["reason"] == "APPROVED_PO_INFERRED", f"{out['reason']} {out['detail']}")
    res, out = run("R1", Scripted("R1", lambda o: o.update(buyer_name="Meridian Threads Pvt Ltd Holdings")))
    case("R1: a different buyer entity -> Held", out["status"] == "Held", f"{out['status']} {out['reason']}")

    # 7. Retryable failures (fix 1): timeout and malformed output are Failed, and the same file can be retried
    sid = LEDGER.start(SEED)
    res, out = run("R1", Scripted(error="Claude call failed: APITimeoutError"), sid=sid)
    case("R1: model timeout -> Failed (retryable)", out["status"] == "Failed", out["status"])
    res, out = run("R1", Scripted(raw={"lines": "not a list", "taxes": []}), sid=sid)
    case("R1: malformed model output -> Failed (retryable)", out["status"] == "Failed", out["status"])
    res, out = run("R1", Scripted("R1"), sid=sid)
    case("R1: retry of the same file after two failures -> Approved", out["status"] == "Approved", out["status"])

    # 8. Not a PDF
    res = extract.run(b"%PDF-1.4 truncated junk", Scripted("R1"))
    case("Corrupt file -> processing error, model never called", res.processing_error and res.doc is None,
         res.processing_error)

    # 9. B1 scan: OCR misreads the tax digit; settled by arithmetic and recorded
    res, out = run("B1", Scripted("B1"))
    tax = next(e for e in res.evidence if e.field == "IGST amount")
    case("B1 scan: OCR reads 4,000.00 for 1,000.00 -> reconciled from totals, Approved",
         tax.status == "reconciled from totals" and out["status"] == "Approved", f"{tax.status} {out['status']}")

    # 10. ... but the same misread with a WRONG model value is not settled
    res, out = run("B1", Scripted("B1", lambda o: o["taxes"][0].update(amount="7,000.00")))
    tax = next(e for e in res.evidence if e.field == "IGST amount")
    case("B1 scan: model says 7,000.00 where OCR reads 4,000.00 -> not confirmed, Held",
         tax.status == "not found" and out["status"] == "Held", f"{tax.status} {out['status']}")

    # ---- review round 2 (GPT): fields must sit beside their own label, items on their own line ----
    # 11. Page prints UNLISTED ACCESSORIES with no SKU; the model claims the shirt (GPT's probe)
    sys.path.insert(0, str(PACK))
    import generate_pack as gp
    facts = gp.build_facts(next(i for i in gp.INVOICES if i["file"] == "R1"))
    facts["lines"][0].update(item_code="", hsn="6217", description="UNLISTED ACCESSORIES")
    acc_pdf, _ = gp.render_text_pdf(facts)
    res, out = run("R1", Scripted("R1"), pdf=acc_pdf)
    desc = next(e for e in res.evidence if e.field == "line 1 description")
    case("Page says UNLISTED ACCESSORIES, model claims TS180 shirt -> description not on the line, Held",
         desc.status == "not found" and out["status"] == "Held" and "item" in out["detail"], out["detail"])

    # 12. Invoice number swapped for the PO number (GPT's probe)
    res, out = run("R1", Scripted("R1", lambda o: o.update(invoice_no="PO-104")))
    ev = next(e for e in res.evidence if e.field == "invoice number")
    case("R1: model returns PO-104 as the invoice number -> printed under the PO label, Held",
         ev.status == "not found" and out["status"] == "Held", f"{ev.note} | {out['detail']}")

    # 13. PO number swapped for the invoice number
    res, out = run("R1", Scripted("R1", lambda o: o.update(po_ref="INV-101")))
    case("R1: model returns INV-101 as the PO number -> Held, PO number unverified",
         out["status"] == "Held" and "PO number" in out["detail"], out["detail"])

    # 14. Layout C prints two 'Dated' fields; the PO's date is not the invoice date
    res, out = run("B11", Scripted("B11", lambda o: o.update(invoice_date="25-Oct-26")))
    case("B11: model returns the PO date as the invoice date -> Held",
         out["status"] == "Held" and "invoice_date" in out["detail"], out["detail"])

    # 15. Line 2 claims line 1's item code and description (both printed, but on the other line)
    def borrow(o):
        o["lines"][1].update(item_code="TS180", description="Cotton crew T-shirt 180 GSM", hsn="6109")
    res, out = run("B11", Scripted("B11", borrow))
    code2 = next(e for e in res.evidence if e.field == "line 2 item code")
    case("B11: line 2 borrows line 1's item code and description -> not on this line, Held",
         code2.status == "not found" and out["status"] == "Held", f"{code2.note} | {out['detail']}")

    # 16. A unit price is never settled from totals, even when OCR is one digit off and arithmetic agrees
    W = extract.Word
    def row(texts, y):
        x, ws = 100, []
        for t in texts:
            ws.append(W(t, (x, y, x + 12 * len(t), y + 30), 1)); x += 12 * len(t) + 20
        return extract.Row(ws, 1, "OCR", 1)
    scan = extract.PageScan(rows=[row(["TS180", "100", "500.00", "20,000.00"], 400),   # OCR misread 200.00
                                  row(["Subtotal", "20,000.00"], 500), row(["IGST", "5%", "1,000.00"], 560),
                                  row(["Invoice", "total", "21,000.00"], 620)], scanned=True)
    raw = extract.printed_form(GT["B1"])
    doc, evidence = extract.verify(raw, scan)
    price = next(e for e in evidence if e.field == "line 1 unit price")
    case("OCR reads unit price 500.00 where the model reads 200.00 -> not settled from totals",
         price.status == "not found" and doc["lines"][0]["unit_price_paise"] is None, price.status)

    # ---- review round 3 (GPT): an empty PO from the model never licenses inference ----
    def make(file, **change):
        inv = dict(next(i for i in gp.INVOICES if i["file"] == file))
        inv.update(change)
        return gp.render_text_pdf(gp.build_facts(inv))[0]

    def deducted(sid):
        with LEDGER.transaction() as db:
            return any(LEDGER.balance(db, sid, o)[str(l["line_no"])]["qty"] != l["qty"]
                       for o in SEED["purchase_orders"] for l in o["lines"])

    # 17. GPT's paired case: layout C prints vendor A's PO-104; the model returns it, then omits it
    other_po = make("B11", po="PO-104")
    reading = extract.printed_form({**GT["B11"], "po_ref": "PO-104"})
    sid = LEDGER.start(SEED)
    res, out_a = run("B11", Scripted(raw=reading), sid=sid, pdf=other_po)
    sid2 = LEDGER.start(SEED)
    res, out_b = run("B11", Scripted(raw={**reading, "po_ref": ""}), sid=sid2, pdf=other_po)
    case("Printed PO belongs to another vendor: model returns it -> Held, nothing deducted",
         out_a["reason"] == "PO_UNMATCHED" and not deducted(sid), out_a["reason"])
    case("... same PDF, model omits it -> still Held (read from the page), nothing deducted",
         out_b["reason"] == "PO_UNMATCHED" and not deducted(sid2), f"{out_b['reason']} {out_b['detail']}")

    # 18. Omitted but printed and valid: the printed PO is used, not an inferred one
    res, out = run("R1", Scripted("R1", lambda o: o.update(po_ref="")))
    case("R1: model omits the printed PO-104 -> read from the page, Approved against PO-104 (not inferred)",
         out["reason"] == "APPROVED" and out["po"] == "PO-104", f"{out['reason']} {out['po']}")
    res, out = run("B1", Scripted("B1", lambda o: o.update(po_ref="")))
    case("B1 scan: model omits the printed PO-104 -> read by OCR, Approved against PO-104",
         out["reason"] == "APPROVED" and out["po"] == "PO-104", f"{out['reason']} {out['po']}")

    # 19. Controls: genuinely no PO -> inference still allowed
    res, out = run("B11", Scripted(raw=extract.printed_form({**GT["B11"], "po_ref": None})), pdf=make("B11", po=None))
    case("Layout C with the PO cell printed blank -> PO-112 inferred, Approved",
         out["reason"] == "APPROVED_PO_INFERRED", out["reason"])
    res, out = run("B4", Scripted("B4"))
    case("B4: no PO printed at all -> PO-110 inferred, Approved", out["reason"] == "APPROVED_PO_INFERRED", out["reason"])

    # 20. A PO label whose value cannot be read -> Held, never inferred
    scan = extract.PageScan(rows=[row(["Purchase", "order:", "~~%%"], 300)], scanned=True)
    doc, evidence = extract.verify({**extract.printed_form(GT["R1"]), "po_ref": ""}, scan)
    case("PO label with an unreadable value, model omits the PO -> marked unverified (Held), not inferred",
         doc["po_ref"] is None and doc.get("po_ref_unverified"), str(doc.get("po_ref_unverified")))

    # ---- review round 4 (GPT): two PO numbers printed -> the model's answer must not pick the order ----
    def full(sid, po):
        with LEDGER.transaction() as db:
            o = next(o for o in SEED["purchase_orders"] if o["po"] == po)
            return LEDGER.balance(db, sid, o)["1"]["qty"] == o["lines"][0]["qty"]

    for printed in ("PO-104 / PO-107", "PO-104, PO-107", "PO-104 & PO-107"):
        two_po = make("R1", po=printed)
        for answer in ("", "PO-104", "PO-107"):
            if printed != "PO-104 / PO-107" and answer:
                continue  # other separators: the empty answer is enough
            sid = LEDGER.start(SEED)
            reading = {**extract.printed_form({**GT["R1"], "po_ref": None}), "po_ref": answer}
            res, out = run("R1", Scripted(raw=reading), sid=sid, pdf=two_po)
            case(f"Page prints '{printed}', model returns {answer or 'no PO'} -> Held PO_AMBIGUOUS, PO-104 and PO-107 intact",
                 out["reason"] == "PO_AMBIGUOUS" and full(sid, "PO-104") and full(sid, "PO-107"),
                 f"{out['reason']} {out['detail']}")

    failed = [n for n, ok in results if not ok]
    print(f"{len(results) - len(failed)} of {len(results)} extraction checks passed.")
    assert not failed, failed


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        check_all(Ledger(Path(tmp) / "x.db"))
