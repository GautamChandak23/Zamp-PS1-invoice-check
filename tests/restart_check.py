"""Step 07b milestone, locally: upload R1.pdf through the app, restart the app, confirm the approval and the
PO-104 balance survive. Uses Postgres from DATABASE_URL and the test-pack model replay."""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
PORT = 8599
ENV = {**os.environ, "PS1_FIXTURE_DIR": str(ROOT / "data" / "test_pack")}
assert ENV.get("DATABASE_URL", "").startswith("postgres"), "set DATABASE_URL to a Postgres database"


def start_app():
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true",
                             "--server.port", str(PORT), "--browser.gatherUsageStats", "false"],
                            cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            start_new_session=True)
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://localhost:{PORT}/_stcore/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("app did not start")


def stop_app(proc):
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=20)


def open_po_table(page):
    page.get_by_text("Purchase orders in this session").first.click()
    page.get_by_text("Tapivastra Knits LLP").first.wait_for(timeout=30000)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)

    app = start_app()
    page.goto(f"http://localhost:{PORT}")
    page.get_by_role("button", name="Start a new demo session").click()
    page.wait_for_url(re.compile(r"\?s="), timeout=30000)
    session_url = page.url
    page.locator("input[type=file]").set_input_files(str(ROOT / "data" / "test_pack" / "invoices" / "R1.pdf"))
    page.get_by_role("button", name="Check invoice").click()
    page.get_by_text("all matched lines approved").first.wait_for(timeout=60000)
    page.wait_for_timeout(1500)
    open_po_table(page)
    page.screenshot(path=str(OUT / "app_after_R1.png"), full_page=True)
    before_restart = page.locator("body").inner_text()
    stop_app(app)
    print("App stopped. Session URL:", session_url)

    app = start_app()
    page2 = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
    page2.goto(session_url)
    page2.get_by_text("History").first.wait_for(timeout=30000)
    page2.wait_for_timeout(2000)
    open_po_table(page2)
    page2.screenshot(path=str(OUT / "app_after_restart.png"), full_page=True)
    after_restart = page2.locator("body").inner_text()
    stop_app(app)
    browser.close()

for label, text in (("before restart", before_restart), ("after restart", after_restart)):
    print(f"--- {label}")
    print("  Approved decision shown:", "Approved" in text)
    print("  R1.pdf in history:", "R1.pdf" in text)
    print("  PO-104 shows 300 approved, 200 left:", "300 · ₹60,000.00" in text and "200 · ₹40,000.00" in text)
assert "R1.pdf" in after_restart and "200 · ₹40,000.00" in after_restart and "Approved" in after_restart
print("Restart check passed.")
