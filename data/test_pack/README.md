# PS-1 test pack v2

All companies, purchase orders and invoices are fictional. The design spec ("PS-1 Invoice Processing — Design Spec") holds the decision rules; this file covers what is in the pack and how to run each case.

## Files

| File | What it is |
|---|---|
| `seed.json` | Buyer, 3 approved vendors, 4 POs (PO-112 has two lines). Money in paise. Load a fresh copy into every demo session. |
| `invoices/` | 19 PDFs in three layouts. B1 and B2 are image-only phone-style scans. |
| `ground_truth/` | What is printed on each invoice: what a perfect extractor returns. `null` = not readable; the extractor must not infer it. |
| `expected_results.csv` | Expected decision, reason code and headline per case. |
| `expected_balances.csv` | Expected PO-line balances (units and pre-tax rupees) after each case. |
| `generate_pack.py` | Rebuilds everything; identical bytes on every run. `pip install reportlab pdfplumber pillow numpy` |
| `verify_pack.py` | Checks hashes, text layers, OCR on the scans (if Tesseract is installed), arithmetic, tax types and balances. |

Use `ground_truth/` two ways: build and test the rules engine before any LLM is connected (feed it the ground truth as if extracted), then score the extractor field by field against it.

## Running the cases

- **R1–R4:** one session, in order. This is the demo sequence.
- **Other cases:** follow the `setup` column in `expected_results.csv`.
- **B4** is the only invoice in layout B. Keep it aside during the build; it is the unseen-layout test.
- **B8 and B12:** two submissions at the same moment (two threads, or two browser tabs). In B12 whichever commits first is approved; the other must be held.
- **C2:** a reviewer must reject INV-102 (R2) first.

## Layouts

| Layout | Vendor | Date format | Invoice number | Notes |
|---|---|---|---|---|
| A | Tapivastra Knits LLP, Surat, Gujarat (24) | 02 Nov 2026 | INV-101 | Ruled item table with HSN column. Also used by Riverband Textiles LLP (B9), which is not an approved vendor. |
| B | Malwa Fabricraft Pvt Ltd, Indore, Madhya Pradesh (23) | 06-11-2026 | MFC/457 | Stacked label-value fields; no PO number; CGST + SGST. |
| C | Noyyal Threadworks Pvt Ltd, Tiruppur, Tamil Nadu (33) | 6-Nov-26 | NTW/2026-27/0418 | Boxed Tally-style; two lines; Round Off line; total prefixed "INR". |

The buyer is Meridian Threads Pvt Ltd, Indore, Madhya Pradesh (23). Every invoice prints the place of supply, so the tax type follows from supplier state versus place of supply: different states mean IGST, the same state means CGST + SGST.

## Reason codes

`APPROVED`, `APPROVED_PO_INFERRED`, `APPROVED_REPLACEMENT`, `PRICE_VARIANCE`, `DUPLICATE_SAME_FILE`, `CHANGED_VERSION`, `FIELD_UNREADABLE`, `PO_AMBIGUOUS`, `QTY_EXCEEDS_REMAINING`, `TAX_TYPE_MISMATCH`, `TOTAL_MISMATCH`, `VENDOR_NOT_APPROVED`, `POSSIBLE_REISSUE`, `CHARGE_NOT_ON_PO`. Two-submission cases join two codes with `+`.

## Rules settled in review

The design spec has these in full.

- **Exact unit price.** A lower price is held too.
- **Repeat file in the session.** Look up the earlier upload's outcome first. Approved or held: Duplicate, linked to it. Failed to process: read again. Rejected: run the full checks again.
- **Evidence.** Each critical value is tied to its page, row and field. On a scan it is confirmed by independent OCR or a crop. A price worked out from amount ÷ quantity never counts.
- **Saving.** Lock the affected PO lines in a fixed order, re-read their balances, and commit the decision and deductions together. B12 tests two different invoices racing for the same units.
- **No manual overrides.** A held invoice stays held until procurement sends an amended PO or the vendor sends a corrected invoice; either is checked again.
- **No GSTINs.** The fixtures print none, to avoid colliding with real registrations, so vendors are matched by exact name.

## Changes from v1

- **Indian tax-invoice fields:** added HSN codes, supplier state codes and place of supply.
- **Varied dates:** in v1 every invoice had the same date, so the re-issue rule (same vendor, PO, total and date) could fire by accident. Now only B10 shares INV-101's date, on purpose.
- **Layout C (B11):** a Tally-style invoice with a two-line PO (PO-112) and a round-off line. In v1 only one layout was available during the build, and B4 doubled as both the unseen-layout test and the inferred-PO test.
- **B2:** the unit price is printed and then physically smudged. v1 printed the word "[illegible]", which any reader reads cleanly.
- **B1 and B2:** tilted, faded, noisy phone-style scans instead of clean rasters.
- **B12a/B12b:** two different invoices racing for the last 200 shirts. v1's README asked for this test but had no fixture.
- **Invoice numbers with slashes** (MFC/457, NTW/2026-27/0418), to exercise number clean-up.
- **Other:** amounts use Indian digit grouping; `ground_truth/` and reason codes were added; balances moved to their own file, per PO line.
