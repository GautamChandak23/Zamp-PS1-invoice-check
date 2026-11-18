"""Browser check of the reviewer screens: R1-R4 in one session, pending amounts, reject, sample download.
Usage: DATABASE_URL=postgresql://... python tests/ui_check.py <output folder>"""
import os, re, sys, signal, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright
OUT = sys.argv[1]
env = {**os.environ, "PS1_FIXTURE_DIR": "data/test_pack"}
assert env.get("DATABASE_URL", "").startswith("postgres"), "set DATABASE_URL to a Postgres database"
proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", "8597",
                         "--browser.gatherUsageStats", "false"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
for _ in range(60):
    try: urllib.request.urlopen("http://localhost:8597/_stcore/health", timeout=1); break
    except Exception: time.sleep(0.5)
def po_table(pg):
    pg.get_by_text("Purchase orders in this session").first.click(); pg.wait_for_timeout(700)
    row = pg.locator("table").first.locator("tr", has_text="PO-104").first
    cells = [c.strip() for c in row.locator("th, td").all_inner_texts()]
    pg.get_by_text("Purchase orders in this session").first.click(); pg.wait_for_timeout(300)
    return " | ".join(cells[-3:])
try:
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1400, "height": 2600})
        pg.goto("http://localhost:8597"); pg.get_by_role("button", name="Start a new demo session").click(); pg.wait_for_url(re.compile(r"\?s="))
        waits = {"R1": "all matched lines approved", "R2": "Question for the reviewer", "R3": "Duplicate", "R4": "Question for the reviewer"}
        for k, w in waits.items():
            pg.locator("input[type=file]").set_input_files(f"data/test_pack/invoices/{k}.pdf")
            pg.get_by_role("button", name="Check invoice").click()
            pg.get_by_text(f"{k}.pdf").first.wait_for(timeout=60000); pg.wait_for_timeout(2500)
            card = pg.locator("[data-testid='stMain'] [data-testid='stAlert']").first.inner_text().replace("\n", " ")
            print(f"after {k}: {card[:150]}")
            print(f"    PO-104 row: {po_table(pg)}")
            if k in ("R2", "R4"): pg.screenshot(path=f"{OUT}/ui_{k}.png")
        # reject R2
        pg.get_by_text("Reject a held invoice").first.click(); pg.wait_for_timeout(500)
        pg.get_by_label("Reason for rejecting (saved with the decision)").fill("Unit price must be Rs 200 as per PO-104")
        pg.get_by_label("Your name").fill("Gautam"); pg.get_by_role("button", name="Reject").click(); pg.wait_for_timeout(2500)
        print("after rejecting the first held run:", pg.locator("[data-testid='stMain'] [data-testid='stAlert']").first.inner_text().replace("\n", " ")[:80])
        print(f"    PO-104 row: {po_table(pg)}")
        # assumptions page + a download
        pg.get_by_text("Assumptions and samples").first.click(); pg.wait_for_timeout(1500)
        for case in ("R2", "C2"):  # every sample the demo needs downloads byte for byte
            with pg.expect_download() as dl:
                pg.get_by_role("button", name=re.compile(rf"^{case} ·")).click()
            d = dl.value; path = f"{OUT}/dl_{d.suggested_filename}"; d.save_as(path)
            same = open(path, "rb").read() == open(f"data/test_pack/invoices/{case}.pdf", "rb").read()
            assert same, case
            print("download:", d.suggested_filename, os.path.getsize(path), "bytes, same as pack:", same)
        print("download buttons:", pg.get_by_role("button").all_inner_texts())
        pg.screenshot(path=f"{OUT}/ui_assumptions.png")
        b.close()
finally:
    os.killpg(proc.pid, signal.SIGTERM)
