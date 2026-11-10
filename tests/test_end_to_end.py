"""All 18 test-pack cases from the PDF itself: page reading, OCR, value checks and the engine all run for real.
Only the model call is replayed (a faithful reading from the ground truth). Expected results come from the CSVs."""
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import extract  # noqa: E402
from engine import Ledger  # noqa: E402

PACK = ROOT / "data" / "test_pack"
SEED = json.loads((PACK / "seed.json").read_text())


def rows(name):
    with (PACK / name).open() as fh:
        return list(csv.DictReader(fh))


def check_all(ledger: Ledger):
    provider = extract.FixtureProvider(PACK)
    extracted = {}
    for pdf in sorted((PACK / "invoices").glob("*.pdf")):  # extract once per file, as the app would per upload
        res = extract.run(pdf.read_bytes(), provider)
        assert res.processing_error is None, (pdf.stem, res.processing_error)
        extracted[pdf.stem] = (res.doc, pdf.read_bytes())

    def run(sid, key):
        doc, data = extracted[key]
        return ledger.process(sid, json.loads(json.dumps(doc)), data)

    balances = rows("expected_balances.csv")
    sid = None
    for row in rows("expected_results.csv"):
        case = row["case"]
        if case not in ("R2", "R3", "R4"):
            sid = ledger.start(SEED)
        if row["setup"].startswith("After R1") and case != "R2":
            run(sid, "R1")
        if case == "C2":
            ledger.reject(sid, run(sid, "R2")["id"], "Request corrected unit price", "test reviewer")
        if case in ("B8", "B12"):
            gate = Barrier(2)
            def submit(key):
                gate.wait()
                return run(sid, key)
            with ThreadPoolExecutor(2) as pool:
                result = list(pool.map(submit, ["B8", "B8"] if case == "B8" else ["B12a", "B12b"]))
        else:
            result = [run(sid, case)]
        codes = sorted(r["reason"] for r in result)
        assert codes == sorted(row["reason_code"].split("+")), (case, codes, [r["detail"] for r in result])
        assert sorted(r["status"] for r in result) == sorted(row["expected_decision"].split(" + ")), case
        with ledger.transaction() as db:
            for bal in (b for b in balances if b["case"] == case):
                order = next(o for o in SEED["purchase_orders"] if o["po"] == bal["po"])
                line = next(l for l in order["lines"] if l["line_no"] == int(bal["line_no"]))
                got = ledger.remaining(db, sid, order["po"], line)
                assert got == (int(bal["qty_left"]), 100 * int(bal["pre_tax_left_inr"])), (case, got)
        print(case, "PASS", ",".join(codes))
    print(f"End to end from PDFs: {len(rows('expected_results.csv'))} cases, {len(balances)} balance checks passed.")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        check_all(Ledger(Path(tmp) / "e2e.db"))
