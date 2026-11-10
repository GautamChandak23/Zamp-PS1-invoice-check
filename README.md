# PS-1 · Invoice check (Steps 07a + 07b)

Upload a supplier invoice PDF. The app reads it, checks every value against the page, matches it to its purchase order, and returns **Approved**, **Held** (with one question for a person), **Duplicate** or **Failed** (retryable). All companies, POs and invoices in `data/` are fictional.

## Files

| File | What it does |
|---|---|
| `app.py` | Streamlit app: demo sessions, upload, live steps, decision card, evidence, PO balances, history, reject |
| `extract.py` | PDF text layer or Tesseract OCR → model reading (Claude or Gemini) → check each value against the page |
| `engine.py` | Session-scoped ledger on Postgres (or SQLite locally); one transaction per decision, per-session lock |
| `rules.py` | The decision rules, one function per rule |
| `tests/` | Acceptance, regression, extraction and end-to-end checks, plus the restart check |
| `data/seed.json`, `data/test_pack/` | Fictional POs and the 19 test invoices with ground truth and expected results |

## Run the checks (no API key needed)

```
pip install -r requirements.txt        # and Tesseract: apt install tesseract-ocr / brew install tesseract
python run_tests.py                    # SQLite
DATABASE_URL=postgresql://... python run_tests.py
```

`run_tests.py` runs 18 cases from ground truth, the review regressions, 31 extraction checks (a model that guesses, hallucinates, swaps or omits fields, or fails), and all 18 cases again **from the PDFs**: page reading, OCR, value checks and the engine run for real; only the model call is replayed from the ground truth. `tests/restart_check.py` (needs Playwright and Postgres) uploads R1.pdf through the app, restarts the app, and confirms the approval and the PO-104 balance survive.

## Run the app

```
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in DATABASE_URL and one API key
streamlit run app.py
```

Without `DATABASE_URL` the app uses a local SQLite file, which a cloud host wipes on restart.

**Test mode:** set `PS1_FIXTURE_DIR=data/test_pack` (and no API key) to replay the model's reading from the test pack. The sidebar says so. Use it to rehearse without spending API credits, never for the submission.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo. `.gitignore` keeps `secrets.toml` out.
2. Create a free Postgres database (Supabase or Neon). On Supabase, use a pooler connection string. The transaction pooler (port 6543) works because the engine turns off prepared statements.
3. In Streamlit Cloud: New app → the repo → `app.py`. Paste the secrets from `.streamlit/secrets.toml.example` under Settings → Secrets. `packages.txt` installs Tesseract.
4. Set a spending cap on the API key. Optionally set `ACCESS_CODE` and share it in the submission email.
5. Free apps sleep when idle, and free databases can pause. Open both a few minutes before the interview.

## Step 07b done-check with a real model

Use the two unseen invoices in `data/live_test/`. Test mode refuses them, so only the live model can process them. `data/live_test/README.md` lists every value to check.

1. Start a new demo session and copy the address-bar link. Upload `L1.pdf`.
2. Expect **Approved** with PO-104 left 380 · ₹76,000.00. In the evidence table, every value should be confirmed with its page row.
3. Reboot the app (Manage app → Reboot) and open the copied link. The approval and the balance should be unchanged.
4. Repeat in a fresh session with `L2.pdf`. Expect **Approved** with PO-112 left 320 · ₹62,400.00 and 160 · ₹51,200.00.
5. Then try `B1.pdf` (scan: expect Approved) and `B2.pdf` (smudged price: expect Held, unit price unreadable).

## The extraction contract

- **`processing_error` (Failed, retryable)** only when the model call fails or times out, or its output is not valid JSON in our format, or the file cannot be opened. The same file can be uploaded again.
- **Anything the page does not confirm is sent as null.** The engine then holds the invoice with "Cannot verify …". A smudged field, a value the model guessed, and a PO number the model read but the page does not show are all handled this way. A null never becomes "Failed".
- Numbers reach the engine as whole paise. The model returns strings exactly as printed. `extract.py` converts "₹ 60,000", "60000.00" or "300 pcs" and only then checks them against the page.

## How a value is checked against the page

- **Amounts:** each amount must be printed on the same row as its label or a partner value. A unit price must sit next to its quantity or line amount, or a "Rate"/"Price" label; a tax amount next to "IGST"; the total next to "Total".
- **Identity fields:** each must sit beside or under its own label.
  - The invoice number goes with "Invoice No." or "Bill No.", the PO number with "Purchase order" or "Order ref.".
  - The invoice date goes with "Date"/"Dated", and not in the PO's block. Layout C prints a date under both.
  - A PO number offered as the invoice number is rejected.
- **A missing PO number:** an empty answer from the model never means "no PO". The page decides.
  - A reference printed beside or under a PO label is read from the page and checked against procurement records.
  - A PO label carrying something unreadable means a hold.
  - Only when no PO is printed, or the PO cell is visibly blank, is the PO inferred.
  - Two or more PO numbers printed ("PO-104 / PO-107") means a hold on every run, whatever the model answered. Scope is one PO per invoice; procurement says which order authorises the charge.
- **Line items:** a line's item code, HSN and description must be printed in that line's own row or block. If neither the item code nor the description is confirmed there, the invoice is held: the engine never guesses which PO line it is.
- **Scans:** OCR runs twice on the page, plain and denoised. A number OCR read differently gets a second, closer look at just that word.
- **Reconciled from totals (totals block only):** if OCR reads a tax, subtotal or total amount one digit off, the model's reading is accepted only when the amounts OCR did confirm add up with it and not with OCR's reading, with exactly one unknown. The evidence row says "reconciled from totals" and shows both readings. On B1, Tesseract reads ₹1,000.00 as 4,000.00; the confirmed subtotal ₹20,000 and total ₹21,000 settle it.
  - Say "reconciled from totals" in the interview, not "read correctly".
- **Never inferred:** quantities, unit prices and line amounts must be read directly. A value OCR cannot read (B2's smudge) or misreads on a line is never filled in by arithmetic.

## Known limits

- Vendors are matched by name; the fictional invoices carry no GSTIN. Production would match by GSTIN.
- Two-way match only (invoice against PO); no goods receipt.
- A printed line the model leaves out is caught only through the totals: the remaining lines no longer add up to the printed subtotal. Tested with C1's freight and B11's second item; there is no independent row count.
- Label matching covers the label wordings in the three test layouts and common variants. An unusual label wording means a hold, never an approval.
- No manual approval override: a held invoice stays held until procurement amends the PO or the vendor sends a corrected invoice.
