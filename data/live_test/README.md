# Live test: two invoices the app has never seen

Neither file is in the test pack, so test mode refuses them ("not in the test pack"). Only a real model can process them. Run each in a **fresh demo session** on the hosted app, with a live API key.

| File | Layout | Printed values the model must extract | Expected decision | PO balance left after |
|---|---|---|---|---|
| `L1.pdf` | A | INV-131 · 10 Nov 2026 · PO-104 · TS180 120 × 200.00 = 24,000.00 · IGST 5% 1,200.00 · total 25,200.00 | Approved | PO-104: 380 shirts · ₹76,000.00 |
| `L2.pdf` | C (Tally style, two lines) | NTW/2026-27/0533 · 10-Nov-26 · PO-112 · TS180 80 × 195.00 = 15,600.00 · PL220 40 × 320.00 = 12,800.00 · taxable 28,400.00 · IGST 5% 1,420.00 · total 29,820.00 | Approved | PO-112: line 1 320 · ₹62,400.00; line 2 160 · ₹51,200.00 |

For each run:

1. Open the evidence table. Check every value above shows "confirmed", with the page row it came from.
2. Check the decision and the PO balance.
3. Copy the session link, reboot the app, reopen the link, and check the decision and balance are unchanged.

If a value comes back "not found", read the evidence note. A model misreading is the model's error; a value printed correctly but not found is the checker's. Both are worth reporting.
