"""Invoice check: upload an invoice PDF, see each step run, and get Approved / Held / Duplicate with evidence.

Run locally:   streamlit run app.py
Settings (Streamlit secrets or environment variables):
  DATABASE_URL       postgresql://...  (without it, a local SQLite file is used and history is lost on restart)
  LLM_PROVIDER       claude | gemini
  ANTHROPIC_API_KEY  or GEMINI_API_KEY
  MODEL              optional model override
  ACCESS_CODE        optional code reviewers must type to use the app
  PS1_FIXTURE_DIR    test mode only: replay model readings from the test pack
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import extract
from engine import Ledger

HERE = Path(__file__).resolve().parent
SEED = json.loads((HERE / "data" / "seed.json").read_text())
IST = ZoneInfo("Asia/Kolkata")
KEYS = ("DATABASE_URL", "LLM_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MODEL", "ACCESS_CODE",
        "PS1_FIXTURE_DIR")

st.set_page_config(page_title="Invoice check", page_icon="🧾", layout="wide")


def setting(key):
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # no secrets file
        pass
    return os.environ.get(key)


SETTINGS = {k: setting(k) for k in KEYS}


@st.cache_resource
def get_ledger(url):
    return Ledger(url or HERE / "local.db")


@st.cache_resource
def get_provider(provider, model, has_claude, has_gemini, fixtures):
    return extract.provider_from_settings(SETTINGS)


ledger = get_ledger(SETTINGS["DATABASE_URL"])
provider = get_provider(SETTINGS["LLM_PROVIDER"], SETTINGS["MODEL"], bool(SETTINGS["ANTHROPIC_API_KEY"]),
                        bool(SETTINGS["GEMINI_API_KEY"]), SETTINGS["PS1_FIXTURE_DIR"])


def rupees(paise):
    if paise is None:
        return "—"
    return "₹" + extract._inr_text(int(paise))


def local_time(iso):
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%d %b, %H:%M")


# ---------------- access code ----------------
if SETTINGS["ACCESS_CODE"] and st.session_state.get("access") != SETTINGS["ACCESS_CODE"]:
    st.title("Invoice check")
    code = st.text_input("Access code", type="password")
    if code:
        st.session_state["access"] = code
        st.rerun()
    st.stop()

# ---------------- demo session ----------------
sid = st.query_params.get("s")
with st.sidebar:
    st.header("Demo session")
    if st.button("Start a new demo session", type="primary", use_container_width=True):
        sid = ledger.start(SEED)
        st.query_params["s"] = sid
        st.session_state.pop("shown_run", None)
        st.rerun()
    if ledger.session_exists(sid):
        st.caption(f"Session `{sid[:8]}`. The link in your address bar reopens it.")
    st.divider()
    st.caption(f"Model: {provider.name if provider else 'not configured'}")
    st.caption("Database: " + ("PostgreSQL" if (SETTINGS['DATABASE_URL'] or '').startswith('postgres')
                               else "local SQLite file (history is lost when the app restarts)"))
    if isinstance(provider, extract.FixtureProvider):
        st.warning("Test mode: the model's reading is replayed from the test pack. "
                   "Page reading, OCR and every check run for real.")

st.title("Invoice check")
st.caption("Upload a supplier invoice. It is read, every value is checked against the page, and the invoice is "
           "matched to its purchase order. Only clean invoices are approved; everything else is held with a question.")

if not ledger.session_exists(sid):
    st.info("Start a new demo session from the sidebar. Each session gets a fresh copy of the purchase orders.")
    st.stop()

# ---------------- purchase orders ----------------
left = ledger.balances(sid)
vendors = {v["id"]: v["name"] for v in SEED["vendors"]}
po_rows = []
for order in SEED["purchase_orders"]:
    for line in order["lines"]:
        ordered_paise = line["qty"] * line["unit_price_paise"]
        rest = left[order["po"]][str(line["line_no"])]
        po_rows.append({
            "PO": order["po"], "Supplier": vendors[order["vendor_id"]], "Item": line["description"],
            "Unit price": rupees(line["unit_price_paise"]),
            "Ordered": f"{line['qty']} · {rupees(ordered_paise)}",
            "Approved": f"{line['qty'] - rest['qty']} · {rupees(ordered_paise - rest['paise'])}",
            "Left": f"{rest['qty']} · {rupees(rest['paise'])}",
        })
with st.expander("Purchase orders in this session (units · amount before tax)", expanded=False):
    st.table(pd.DataFrame(po_rows).set_index("PO"))

# ---------------- upload and run ----------------
st.subheader("Check an invoice")
upload = st.file_uploader("Invoice PDF", type=["pdf"], label_visibility="collapsed")
if upload is not None and st.button("Check invoice", type="primary"):
    data = upload.getvalue()
    with st.status("Checking the invoice…", expanded=True) as status:
        res = extract.run(data, provider, on_stage=lambda name: st.write(f"✓ {name}"))
        doc = dict(res.doc or {})
        doc["source_file"] = upload.name
        result = ledger.process(sid, doc, data, processing_error=res.processing_error,
                                on_stage=lambda ev: st.write(f"✓ {ev['name']}"))
        status.update(label=f"{result['status']}: {upload.name}",
                      state="error" if result["status"] == "Failed" else "complete", expanded=False)
    st.session_state["shown_run"] = result["id"]
    st.rerun()

# ---------------- decision card ----------------
history = ledger.history(sid)
by_id = {r["id"]: r for r in history}


def card(run):
    payload = json.loads(run["payload"] or "{}")
    review = run.get("review") or {}
    style = {"Approved": st.success, "Held": st.warning, "Duplicate": st.info, "Failed": st.error,
             "Rejected": st.error}.get(run["status"], st.info)
    title = f"**{run['status']}** · {payload.get('source_file', '')}"
    if payload.get("invoice_no"):
        title += f" · {payload['invoice_no']}"
    style(f"{title}\n\n{run['detail']}")
    findings = review.get("findings") or []
    if len(findings) > 1:
        st.markdown("**Also found**\n" + "\n".join(f"- {f['detail']}" for f in findings[1:]))
    if review.get("question"):
        st.markdown(f"**Question for the reviewer:** {review['question']}")
    if run.get("rejection"):
        st.markdown(f"**Rejected** by {run.get('rejected_by')} at {local_time(run.get('rejected_at'))}: "
                    f"{run['rejection']}")
    before, after = review.get("before") or {}, review.get("after") or {}
    if before:
        st.markdown(f"**{review.get('po')} balance left (units · amount before tax)**")
        st.table(pd.DataFrame([{
            "Line": k, "Before": f"{before[k]['qty']} · {rupees(before[k]['paise'])}",
            "After": f"{after.get(k, before[k])['qty']} · {rupees(after.get(k, before[k])['paise'])}",
        } for k in before]).set_index("Line"))
    evidence = payload.get("evidence") or []
    if evidence:
        with st.expander("Evidence: where each value was found on the page"):
            st.table(pd.DataFrame([{
                "Field": e["field"], "Read as": e["printed"] or "", "Check": e["status"],
                "Found on": f"p{e['page']} · {e['source']}: {e['where']}" if e.get("where") else "",
                "Note": e.get("note") or "",
            } for e in evidence]).set_index("Field"))


if history:
    shown = st.session_state.get("shown_run")
    if shown in by_id:
        st.subheader("Decision")
        card(by_id[shown])

    # ---------------- history ----------------
    st.subheader("History")
    table = []
    for r in reversed(history):
        payload = json.loads(r["payload"] or "{}")
        table.append({"Run": r["id"], "Time (IST)": local_time(r.get("created_at")),
                      "File": payload.get("source_file", ""), "Invoice": payload.get("invoice_no") or "",
                      "PO": r.get("po") or "", "Decision": r["status"], "Reason": r["reason"]})
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
    pick = st.selectbox("Open a past run", [t["Run"] for t in table],
                        format_func=lambda i: f"Run {i} · {by_id[i]['status']} · "
                                              f"{json.loads(by_id[i]['payload'] or '{}').get('source_file', '')}")
    if pick is not None and pick != shown:
        card(by_id[pick])

    # ---------------- reject a held invoice ----------------
    held = [r for r in history if r["status"] == "Held"]
    if held:
        with st.expander("Reject a held invoice"):
            target = st.selectbox("Held invoice", [r["id"] for r in held],
                                  format_func=lambda i: f"Run {i} · {json.loads(by_id[i]['payload'] or '{}').get('invoice_no') or ''}"
                                                        f" · {by_id[i]['reason']}")
            reason = st.text_input("Reason, sent to the supplier")
            who = st.text_input("Your name")
            if st.button("Reject"):
                try:
                    ledger.reject(sid, target, reason, who)
                    st.session_state["shown_run"] = target
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
