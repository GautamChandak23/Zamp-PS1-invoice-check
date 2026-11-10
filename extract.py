"""Step 07b: turn an invoice PDF into the engine's document, with every critical value checked against the page.

Flow
  1. Read the page: the PDF text layer, or Tesseract OCR when a page is only an image (a scan).
  2. Ask the model (Claude or Gemini) to copy each field exactly as printed. It never sees our rules.
  3. Check every critical value against the page, independently of the model:
       - the value must be printed on the page, on the same row as its label or a partner value
         (unit price next to its quantity or line amount, a tax amount next to "IGST", and so on);
       - on a scan, OCR must read the same value. OCR gets a second, closer look at a number it misread;
       - totals block only: if OCR reads a tax / subtotal / total amount one digit off, the model's reading is
         accepted only when the amounts OCR did confirm add up with it and not with OCR's reading; it is
         labelled "reconciled from totals". Line values (quantity, unit price, amount) must be read directly;
       - invoice number, PO number and invoice date must sit beside or under their own label; a line's item
         code, HSN and description must sit in that line's own row or block.
       - a value derived by arithmetic is never used for a field that cannot be read (B2's smudge).
     A value that fails these checks is sent to the engine as null, which holds the invoice with a question.
  4. Hand back (document, processing_error, evidence). processing_error is ONLY for failures that a retry
     might fix: the model call failed or timed out, or its output was not valid JSON in our format.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

import pdfplumber
from PIL import Image, ImageFilter
from pydantic import BaseModel, ConfigDict, ValidationError

from rules import norm_name

MAX_PAGES = 5
MAX_BYTES = 5 * 1024 * 1024
OCR_DPI = 300

# --------------------------------------------------------------------------------------
# What the model returns: strings exactly as printed. "" or a missing key means "not legible".
# --------------------------------------------------------------------------------------
_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lines", "taxes"],
    "properties": {
        "vendor_name": {**_S, "description": "Supplier's legal name as printed"},
        "buyer_name": {**_S, "description": "Legal name of the party billed, as printed"},
        "invoice_no": {**_S, "description": "Invoice / bill number exactly as printed"},
        "invoice_date": {**_S, "description": "Invoice date exactly as printed, e.g. '02 Nov 2026' or '06-11-2026'"},
        "po_ref": {**_S, "description": "Buyer's purchase order number, if printed"},
        "currency": {**_S, "description": "Currency code, e.g. INR"},
        "supplier_state_code": {**_S, "description": "Supplier's 2-digit GST state code"},
        "place_of_supply_state_code": {**_S, "description": "Place of supply as a 2-digit GST state code"},
        "lines": {
            "type": "array",
            "description": "Every line item, including freight or other charges, in printed order",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description"],
                "properties": {
                    "item_code": _S, "hsn": _S, "description": _S,
                    "qty": {**_S, "description": "Quantity exactly as printed, digits only"},
                    "unit_price": {**_S, "description": "Unit price / rate exactly as printed"},
                    "amount": {**_S, "description": "Line amount exactly as printed"},
                },
            },
        },
        "subtotal": {**_S, "description": "Subtotal / taxable value exactly as printed"},
        "taxes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {**_S, "description": "IGST, CGST, SGST or UTGST"},
                    "rate": {**_S, "description": "Rate as printed, e.g. '5%'"},
                    "amount": {**_S, "description": "Tax amount exactly as printed"},
                },
            },
        },
        "round_off": {**_S, "description": "Round-off amount if printed, with its sign"},
        "total": {**_S, "description": "Invoice total exactly as printed"},
    },
}

PROMPT = """You are reading one supplier invoice for an accounts payable team. Fill in the schema by COPYING what is printed.

Rules:
- Copy every value exactly as printed: numbers with their commas and decimals, dates in their printed format.
- If a value is smudged, cut off or otherwise not clearly legible, leave it out (or use an empty string). Do not guess.
- Never calculate a value from other values. For example, never work out a unit price from amount divided by quantity.
- buyer_name is the legal name of the party billed, without its address.
- item_code is the item code / SKU (e.g. HD300) whenever one is printed, even if it is printed inside the
  description; description is the item text without that code.
- State codes are 2-digit GST state codes. If only a state name is printed, give that state's GST code.
- List every line item, including freight or any other charge, in the order printed.
- List each tax line (IGST, or CGST and SGST) separately."""


class _Line(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item_code: Optional[str] = None
    hsn: Optional[str] = None
    description: Optional[str] = None
    qty: Optional[str] = None
    unit_price: Optional[str] = None
    amount: Optional[str] = None


class _Tax(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Optional[str] = None
    rate: Optional[str] = None
    amount: Optional[str] = None


class ModelInvoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vendor_name: Optional[str] = None
    buyer_name: Optional[str] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[str] = None
    po_ref: Optional[str] = None
    currency: Optional[str] = None
    supplier_state_code: Optional[str] = None
    place_of_supply_state_code: Optional[str] = None
    lines: list[_Line]
    subtotal: Optional[str] = None
    taxes: list[_Tax]
    round_off: Optional[str] = None
    total: Optional[str] = None


# --------------------------------------------------------------------------------------
# Model providers. Each returns the raw dict or raises ProviderError.
# --------------------------------------------------------------------------------------
class ProviderError(Exception):
    """The model call failed in a way a retry might fix."""


class ClaudeProvider:
    name = "Claude"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5", timeout: float = 90):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def extract(self, pdf: bytes) -> dict:
        import anthropic
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                    "data": base64.standard_b64encode(pdf).decode()}},
                    {"type": "text", "text": PROMPT},
                ]}],
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"Claude call failed: {type(exc).__name__}") from exc
        if msg.stop_reason in ("max_tokens", "refusal"):
            raise ProviderError(f"Claude stopped early ({msg.stop_reason})")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Claude returned output that is not JSON") from exc


class GeminiProvider:
    name = "Gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash", timeout: float = 90):
        from google import genai
        from google.genai import types
        self.model, self.types = model, types
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))

    def extract(self, pdf: bytes) -> dict:
        types = self.types
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=pdf, mime_type="application/pdf"), PROMPT],
                config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                                   response_json_schema=SCHEMA),
            )
        except Exception as exc:  # the SDK raises several unrelated types (network, API, timeout)
            code = getattr(exc, "code", None)
            status = getattr(exc, "status", None)
            raise ProviderError(f"Gemini call failed: {type(exc).__name__} | HTTP {code} | {status}") from exc
        try:
            return json.loads(resp.text or "")
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned output that is not JSON") from exc


def provider_from_settings(settings: dict):
    """settings: LLM_PROVIDER (claude|gemini), ANTHROPIC_API_KEY / GEMINI_API_KEY, optional MODEL."""
    choice = (settings.get("LLM_PROVIDER") or "").strip().lower()
    model = settings.get("MODEL")
    if choice == "claude" and settings.get("ANTHROPIC_API_KEY"):
        return ClaudeProvider(settings["ANTHROPIC_API_KEY"], **({"model": model} if model else {}))
    if choice == "gemini" and settings.get("GEMINI_API_KEY"):
        return GeminiProvider(settings["GEMINI_API_KEY"], **({"model": model} if model else {}))
    if settings.get("PS1_FIXTURE_DIR"):
        return FixtureProvider(Path(settings["PS1_FIXTURE_DIR"]))
    return None


# --------------------------------------------------------------------------------------
# Reading the page
# --------------------------------------------------------------------------------------
@dataclass
class Word:
    text: str
    box: tuple  # (x0, y0, x1, y1) in pixels for OCR, points for the text layer
    page: int


@dataclass
class Row:
    words: list
    page: int
    source: str  # "text layer" or "OCR"
    read: int = 0  # 0 text layer, 1 OCR plain, 2 OCR denoised: rows are only compared within one reading

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def y(self) -> float:
        return min(w.box[1] for w in self.words)

    @property
    def height(self) -> float:
        hs = sorted(w.box[3] - w.box[1] for w in self.words)
        return max(1.0, hs[len(hs) // 2])


@dataclass
class PageScan:
    rows: list = field(default_factory=list)
    images: dict = field(default_factory=dict)  # page -> PIL image used for OCR second looks
    scanned: bool = False


def _otsu(gray: Image.Image) -> Image.Image:
    hist = gray.histogram()[:256]
    total, sum_all = sum(hist), sum(i * h for i, h in enumerate(hist))
    w_b = s_b = 0
    best, thr = -1.0, 128
    for t, h in enumerate(hist):
        w_b += h
        if not w_b or w_b == total:
            continue
        s_b += t * h
        m_b, m_f = s_b / w_b, (sum_all - s_b) / (total - w_b)
        between = w_b * (total - w_b) * (m_b - m_f) ** 2
        if between > best:
            best, thr = between, t
    return gray.point(lambda p: 255 if p > thr else 0)


def _tesseract(img: Image.Image, psm: str = "6", tsv: bool = True) -> str:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "page.png"
        img.save(path)
        args = ["tesseract", str(path), "-", "--psm", psm] + (["tsv"] if tsv else [])
        return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout


def _ocr_rows(img: Image.Image, page: int, read: int) -> list:
    rows: dict = {}
    reader = csv.DictReader(io.StringIO(_tesseract(img)), delimiter="\t", quoting=csv.QUOTE_NONE)
    for r in reader:
        if not (r.get("text") or "").strip() or float(r.get("conf") or -1) < 0:
            continue
        l, t, w, h = (int(r[k]) for k in ("left", "top", "width", "height"))
        key = (r["block_num"], r["par_num"], r["line_num"])
        rows.setdefault(key, []).append(Word(r["text"], (l, t, l + w, t + h), page))
    return [Row(ws, page, "OCR", read) for ws in rows.values()]


def read_pages(pdf: bytes) -> PageScan:
    scan = PageScan()
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        if len(doc.pages) > MAX_PAGES:
            raise ValueError(f"The PDF has {len(doc.pages)} pages; the limit is {MAX_PAGES}.")
        for n, page in enumerate(doc.pages, 1):
            words = page.extract_words(keep_blank_chars=False)
            if words:
                words.sort(key=lambda w: (round(w["top"]), w["x0"]))
                current, last_top = [], None
                for w in words:
                    if last_top is not None and abs(w["top"] - last_top) > 3:
                        scan.rows.append(Row(current, n, "text layer"))
                        current = []
                    current.append(Word(w["text"], (w["x0"], w["top"], w["x1"], w["bottom"]), n))
                    last_top = w["top"]
                if current:
                    scan.rows.append(Row(current, n, "text layer"))
                continue
            # Image-only page: OCR twice (plain grey, and denoised + thresholded); keep both readings.
            scan.scanned = True
            gray = page.to_image(resolution=OCR_DPI).original.convert("L")
            scan.images[n] = gray
            scan.rows += _ocr_rows(gray, n, 1)
            scan.rows += _ocr_rows(_otsu(gray.filter(ImageFilter.MedianFilter(3))), n, 2)
    for row in scan.rows:
        row.words.sort(key=lambda w: w.box[0])
    return scan


def second_look(scan: PageScan, word: Word) -> str:
    """OCR one word again on its own, enlarged. Used only to confirm a number OCR misread."""
    img = scan.images.get(word.page)
    if img is None:
        return word.text
    x0, y0, x1, y1 = word.box
    pad = max(6, (y1 - y0) // 3)
    crop = img.crop((x0 - pad, y0 - pad, x1 + pad, y1 + pad))
    crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
    return _tesseract(crop, psm="7", tsv=False).strip()


# --------------------------------------------------------------------------------------
# Parsing printed values
# --------------------------------------------------------------------------------------
_MONEY_TOKEN = re.compile(r"^-?[\d,]*\d\.\d{2}$")
_STRIP = ":;|()[]{}'\"₹"


def clean(token: str) -> str:
    """Strip brackets, colons and currency marks around a word; keep a money value's own punctuation."""
    t = token.strip().strip(_STRIP)
    return t if _MONEY_TOKEN.match(t) else t.rstrip(",.:;")


def money(text) -> Optional[Decimal]:
    """'₹ 1,00,000.00' / 'INR 60000' / 'Rs. 500' / '-0.50' -> Decimal; None if not a plain amount."""
    if text is None:
        return None
    t = re.sub(r"(?i)inr|rs\.?|rupees|₹|,|\s", "", str(text))
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", t):
        return None
    return Decimal(t).quantize(Decimal("0.01"))


def page_money(token: str) -> Optional[Decimal]:
    t = clean(token)
    return Decimal(t.replace(",", "")) if _MONEY_TOKEN.match(t) else None


def integer(text) -> Optional[int]:
    if text is None:
        return None
    m = re.fullmatch(r"\s*(\d[\d,]*)(\.0+)?\s*(pcs|nos|units?|pieces)?\s*", str(text), re.I)
    return int(m.group(1).replace(",", "")) if m else None


def rate(text) -> Optional[Decimal]:
    if text is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?", str(text))
    return Decimal(m.group(1)).normalize() if m else None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}
_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,]+(\d{2,4})\b"),  # 02 Nov 2026, 6-Nov-26
    re.compile(r"\b(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{2,4})\b"),               # 06-11-2026 (day first)
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),                              # 2026-11-06
]


def dates_in(text: str) -> set:
    found = set()
    for i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(text or ""):
            try:
                if i == 0:
                    month = _MONTHS.get(m.group(2)[:3].lower())
                    if not month:
                        continue
                    d, y = int(m.group(1)), int(m.group(3))
                elif i == 1:
                    d, month, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    y, month, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                y = y + 2000 if y < 100 else y
                found.add(date(y, month, d).isoformat())
            except ValueError:
                continue
    return found


GST_STATES = {
    "01": "jammu and kashmir", "02": "himachal pradesh", "03": "punjab", "04": "chandigarh", "05": "uttarakhand",
    "06": "haryana", "07": "delhi", "08": "rajasthan", "09": "uttar pradesh", "10": "bihar", "11": "sikkim",
    "12": "arunachal pradesh", "13": "nagaland", "14": "manipur", "15": "mizoram", "16": "tripura",
    "17": "meghalaya", "18": "assam", "19": "west bengal", "20": "jharkhand", "21": "odisha",
    "22": "chhattisgarh", "23": "madhya pradesh", "24": "gujarat", "26": "dadra and nagar haveli and daman and diu",
    "27": "maharashtra", "29": "karnataka", "30": "goa", "31": "lakshadweep", "32": "kerala", "33": "tamil nadu",
    "34": "puducherry", "35": "andaman and nicobar islands", "36": "telangana", "37": "andhra pradesh",
    "38": "ladakh",
}


def words_of(row: Row) -> list:
    return [clean(w.text).lower() for w in row.words]


def has_label(row: Row, labels) -> bool:
    ws = set(words_of(row))
    return any(l in ws for l in labels)


def has_money(row: Row) -> bool:
    return any(page_money(w.text) is not None for w in row.words)


# ---- labels: which printed label a value sits under or beside ----
INVOICE_NO_LABEL = re.compile(r"\b(invoice|inv|bill)\b\.?\s*(no\b|number|num\b|#)", re.I)
INVOICE_WORD = re.compile(r"\b(invoice|inv|bill)\b", re.I)
PO_LABEL = re.compile(r"\b(purchase\s*order|order\s*(no\b|ref|number|#)|buyer'?s\s*order|p\.?\s*o\.?\s*(no\b|number|#|ref))", re.I)
DATE_LABEL = re.compile(r"\b(date|dated)\b", re.I)


def _rows_of(scan: PageScan, row: Row) -> list:
    return sorted((r for r in scan.rows if r.page == row.page and r.read == row.read), key=lambda r: r.y)


def _phrase(row: Row, x0: float, x1: float, gap: float) -> str:
    """The run of words in `row` that overlaps [x0, x1], extended left and right across small gaps."""
    ws = row.words
    hits = [i for i, w in enumerate(ws) if w.box[2] >= x0 and w.box[0] <= x1]
    if not hits:
        return ""
    lo, hi = hits[0], hits[-1]
    while lo > 0 and ws[lo].box[0] - ws[lo - 1].box[2] <= gap:
        lo -= 1
    while hi < len(ws) - 1 and ws[hi + 1].box[0] - ws[hi].box[2] <= gap:
        hi += 1
    return " ".join(w.text for w in ws[lo:hi + 1])


def label_chain(scan: PageScan, row: Row, word: Word, depth: int = 3) -> list:
    """Label text for a value, nearest first: words to its left on the same row (when they contain letters),
    then the phrases directly above it in the same column (a label printed over its value, as in a grid)."""
    h = max(1.0, word.box[3] - word.box[1])
    chain = []
    left = " ".join(w.text for w in row.words if w.box[2] <= word.box[0] + 0.5)
    if re.search(r"[A-Za-z]{2}", left):
        chain.append(left)
    above = [r for r in _rows_of(scan, row) if r.y < row.y - 0.4 * h]
    for r in reversed(above):
        if row.y - r.y > (len(chain) + 1) * 3.5 * h:
            break
        text = _phrase(r, word.box[0] - h, word.box[2] + h, 3 * h)
        if text:
            chain.append(text)
        if len(chain) >= depth + 1:
            break
    return chain


def line_region(scan: PageScan, anchors: list) -> list:
    """Rows that belong to one invoice line, from the rows where its quantity / price / amount were found.
    Table line (values on one row): that row, plus the row just below if it carries no amounts (e.g. 'Item code').
    Stacked line (values on separate rows): those rows plus up to 3 rows above, stopping at another line's amounts."""
    region = []
    groups: dict = {}
    for r in anchors:
        groups.setdefault((r.page, r.read), []).append(r)
    for rs in groups.values():
        ordered = _rows_of(scan, rs[0])
        index = {id(r): i for i, r in enumerate(ordered)}
        ids = sorted({index[id(r)] for r in rs if id(r) in index})
        if not ids:
            continue
        lo, hi = ids[0], ids[-1]
        region += ordered[lo:hi + 1]
        if lo == hi:
            nxt = ordered[hi + 1] if hi + 1 < len(ordered) else None
            if nxt and not has_money(nxt) and nxt.y - ordered[hi].y < 2.5 * ordered[hi].height:
                region.append(nxt)
        else:
            for j in range(lo - 1, max(-1, lo - 4), -1):
                if has_money(ordered[j]):
                    break
                region.append(ordered[j])
    return region


_REFERENCE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9/\-_.]{2,}$")
_FILLER = {"no", "number", "num", "#", "ref", "-", ""}
_ANY_LABEL = re.compile(r"\b(date|dated|invoice|bill|terms|mode|despatch|dispatch|delivery|state|code|gstin|pan)\b", re.I)


def printed_po_references(scan: PageScan) -> tuple[list, list]:
    """Independently of the model: what is printed beside or under each PO label on the page.
    Returns (references found as (value, row), rows where a PO label carries something unreadable).
    A PO label with nothing after it or under it (a blank cell) counts as no PO."""
    found, unreadable = [], []
    for row in scan.rows:
        texts = [w.text for w in row.words]
        end = next((i for i in range(len(texts)) if PO_LABEL.search(" ".join(texts[:i + 1]))), None)
        if end is None:
            continue
        h = row.height
        value = [w for w in row.words[end + 1:] if clean(w.text).lower().rstrip(".") not in _FILLER]
        source = row
        if not value:  # grid layouts print the value under the label
            start = end
            while start > 0 and row.words[start].box[0] - row.words[start - 1].box[2] <= 3 * h:
                start -= 1
            x0, x1 = row.words[start].box[0], row.words[end].box[2]
            below = [r for r in _rows_of(scan, row) if 0 < r.y - row.y <= 2.2 * h]
            if below:
                text = _phrase(below[0], x0, x1, 3 * h)
                if text and not (_ANY_LABEL.search(text) or PO_LABEL.search(text)):
                    value = [w for w in below[0].words if w.box[2] >= x0 - h and w.box[0] <= x1 + h]
                    source = below[0]
        refs = [clean(w.text) for w in value if _REFERENCE.match(clean(w.text))]
        if refs:
            found += [(ref, source) for ref in refs]  # every reference, e.g. "PO-104 / PO-107" gives two
        elif value:
            unreadable.append(source)
    return found, unreadable


def tokens(text: str) -> list:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


# --------------------------------------------------------------------------------------
# Checking values against the page
# --------------------------------------------------------------------------------------
@dataclass
class Evidence:
    field: str
    printed: Optional[str]          # what the model said is printed
    status: str                     # confirmed | second look | arithmetic | not found | not printed
    where: str = ""                 # the row it was found on
    page: Optional[int] = None
    source: str = ""                # text layer | OCR
    note: str = ""
    row: Optional[Row] = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status in ("confirmed", "second look", "reconciled from totals")

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "row"}


class Checker:
    def __init__(self, scan: PageScan):
        self.scan = scan
        self.evidence: list[Evidence] = []
        self.slips: dict = {}  # field -> (model value, OCR value, row) for one-digit OCR conflicts

    def _record(self, ev: Evidence) -> Evidence:
        self.evidence.append(ev)
        return ev

    def _found(self, fld, printed, row, status="confirmed", note=""):
        return self._record(Evidence(fld, printed, status, row.text, row.page, row.source, note, row))

    def _missing(self, fld, printed, note=""):
        return self._record(Evidence(fld, printed, "not found", note=note))

    # ---- numbers in a row ----
    def number(self, fld, printed, kind, rows_ok: Callable[[Row], bool], reconcile=False) -> Evidence:
        """kind: 'money' or 'qty'. rows_ok picks the rows where this value may appear.
        reconcile=True (totals block only): a one-digit OCR conflict may later be settled from confirmed totals."""
        want = money(printed) if kind == "money" else integer(printed)
        if want is None:
            return self._missing(fld, printed, "not a readable number" if printed else "not given by the model")
        rows = [r for r in self.scan.rows if rows_ok(r)]
        read = page_money if kind == "money" else (lambda t: int(clean(t)) if clean(t).isdigit() else None)
        for row in rows:
            if any(read(w.text) == want for w in row.words):
                return self._found(fld, printed, row)
        # Scans: a closer look at number-shaped words OCR read differently in those rows.
        if self.scan.scanned:
            shaped = (lambda t: bool(re.search(r"\d[.,]\d", t))) if kind == "money" else \
                     (lambda t: bool(re.fullmatch(r"\d+", clean(t))))
            for row in rows:
                for w in row.words:
                    if row.source == "OCR" and shaped(w.text) and read(w.text) != want:
                        again = second_look(self.scan, w)
                        if read(again) == want:
                            return self._found(fld, printed, row, "second look", f"OCR first read '{w.text}'")
            if kind == "money" and reconcile:
                digits = re.sub(r"\D", "", f"{want:.2f}")
                for row in rows:
                    for w in row.words:
                        other = page_money(w.text)
                        od = re.sub(r"\D", "", f"{other:.2f}") if other is not None else ""
                        if len(od) == len(digits) and sum(a != b for a, b in zip(od, digits)) == 1:
                            self.slips[fld] = (want, other, row)
                            return self._missing(fld, printed, f"OCR read {w.text}")
        return self._missing(fld, printed)

    def text_token(self, fld, printed, rows_ok=lambda r: True) -> Evidence:
        if not printed:
            return self._missing(fld, printed, "not given by the model")
        want = clean(printed).lower()
        for row in self.scan.rows:
            if rows_ok(row) and want in words_of(row):
                return self._found(fld, printed, row)
        return self._missing(fld, printed)

    def labelled(self, fld, printed, accept: Callable[[list], bool]) -> Evidence:
        """A token that must sit beside or under the right label (invoice number, PO number)."""
        if not printed:
            return self._missing(fld, printed, "not given by the model")
        want = clean(printed).lower()
        seen_elsewhere = None
        for row in self.scan.rows:
            for w in row.words:
                if clean(w.text).lower() != want:
                    continue
                chain = label_chain(self.scan, row, w)
                if chain and accept(chain):
                    return self._found(fld, printed, row)
                seen_elsewhere = seen_elsewhere or (chain[0] if chain else row.text)
        note = f"printed, but under or beside '{seen_elsewhere}'" if seen_elsewhere else ""
        return self._missing(fld, printed, note)

    def in_region(self, fld, printed, region: list, as_phrase=False) -> Evidence:
        """Item code / HSN / description: must be printed inside this line's own row or block."""
        if not printed:
            return self._missing(fld, printed, "not given by the model")
        if as_phrase:
            want, have = tokens(printed), set(tokens(" ".join(r.text for r in region)))
            hit = bool(want) and all(t in have for t in want)
        else:
            want_t = clean(printed).lower()
            hit = any(want_t in words_of(r) for r in region)
        if hit:
            row = next((r for r in region if (as_phrase and set(tokens(printed)) & set(tokens(r.text))) or
                        (not as_phrase and clean(printed).lower() in words_of(r))), region[0])
            return self._found(fld, printed, row)
        return self._missing(fld, printed, "not on this line" if region else "line values not confirmed")

    def name(self, fld, printed) -> Evidence:
        if not printed:
            return self._missing(fld, printed, "not given by the model")
        want = norm_name(printed).split()
        for row in self.scan.rows:
            have = norm_name(" ".join(w.text for w in row.words).replace(",", " ")).split()
            if want and all(w in have for w in want):
                return self._found(fld, printed, row)
        return self._missing(fld, printed)

    def invoice_date(self, printed) -> tuple:
        if not printed:
            return None, self._missing("invoice date", printed, "not given by the model")
        parsed = dates_in(printed)
        if len(parsed) != 1:
            return None, self._missing("invoice date", printed, "not a readable date")
        iso = parsed.pop()
        seen = None
        for row in self.scan.rows:
            if iso not in dates_in(row.text):
                continue
            texts = [w.text for w in row.words]
            start = next(i for i in range(len(texts)) if iso in dates_in(" ".join(texts[i:]))
                         and (i + 1 == len(texts) or iso not in dates_in(" ".join(texts[i + 1:]))))
            chain = label_chain(self.scan, row, row.words[start])
            if chain and DATE_LABEL.search(chain[0]) and not PO_LABEL.search(chain[0]):
                owner = next((t for t in chain if INVOICE_WORD.search(t) or PO_LABEL.search(t)), "")
                if not PO_LABEL.search(owner):
                    return iso, self._found("invoice date", printed, row)
            seen = seen or (chain[0] if chain else row.text)
        return None, self._missing("invoice date", printed, f"printed, but under or beside '{seen}'" if seen else "")

    def state_code(self, fld, printed) -> Optional[str]:
        code = re.sub(r"\D", "", printed or "")
        code = code.zfill(2) if code else ""
        if code not in GST_STATES:
            self._missing(fld, printed, "not a GST state code" if printed else "not given by the model")
            return None
        # Evidence row: for the place of supply prefer the "supply" row; for the supplier avoid buyer rows.
        # (The engine checks both codes against the vendor record and the buyer, so this only picks the row shown.)
        is_pos = "supply" in fld
        def preferred(r):
            t = " ".join(words_of(r))
            return 0 if (("supply" in t) == is_pos and (is_pos or not re.search(r"\b(buyer|bill)\b", t))) else 1
        for row in sorted(self.scan.rows, key=preferred):
            text = " ".join(words_of(row))
            if not re.search(r"\b(state|code|supply)\b", text):
                continue
            tokens = set(re.split(r"[\s\-:,()]+", text))
            if code in tokens or code.lstrip("0") in tokens or GST_STATES[code] in text:
                self._found(fld, printed, row)
                return code
        self._missing(fld, printed)
        return None


def verify(raw: dict, scan: PageScan) -> tuple[dict, list]:
    """raw: validated model output. Returns (engine document, evidence list)."""
    m = ModelInvoice.model_validate(raw)
    blank = lambda v: v if (v is not None and str(v).strip()) else None
    ck = Checker(scan)
    doc: dict = {}

    doc["vendor_name"] = blank(m.vendor_name) if ck.name("supplier name", blank(m.vendor_name)).ok else None
    doc["buyer_name"] = blank(m.buyer_name) if ck.name("billed to", blank(m.buyer_name)).ok else None
    is_invoice_no = lambda chain: bool(INVOICE_NO_LABEL.search(chain[0])) and not PO_LABEL.search(chain[0])
    doc["invoice_no"] = blank(m.invoice_no) if ck.labelled("invoice number", blank(m.invoice_no), is_invoice_no).ok else None
    doc["invoice_date"], _ = ck.invoice_date(blank(m.invoice_date))
    # Every run: all PO numbers printed under a PO label, read from the page independently of the model.
    refs, unreadable = printed_po_references(scan)
    distinct = sorted({value for value, _ in refs})
    doc["po_refs_printed"] = distinct  # more than one -> the engine holds: one PO per invoice
    if blank(m.po_ref):
        doc["po_ref"] = m.po_ref.strip() if ck.labelled("PO number", m.po_ref, lambda c: bool(PO_LABEL.search(c[0]))).ok else None
        if doc["po_ref"] is None:
            doc["po_ref_unverified"] = m.po_ref  # never silently treated as "no PO"
    else:
        # The model gave no PO. Only the page can say there is none: an empty answer never licenses inference.
        if len(distinct) == 1 and not unreadable:
            doc["po_ref"] = distinct[0]
            ck._found("PO number", distinct[0], next(r for v, r in refs if v == distinct[0]),
                      note="the model left it out; read from the page and checked against procurement records")
        elif len(distinct) > 1:
            doc["po_ref"] = None
            ck._missing("PO number", None, f"several PO numbers printed: {', '.join(distinct)}")
        elif unreadable:
            doc["po_ref"] = None
            doc["po_ref_unverified"] = "printed but not readable"
            ck._missing("PO number", None, "a PO label carries a value that could not be read reliably")
        else:
            doc["po_ref"] = None
            ck._record(Evidence("PO number", None, "not printed", note="no PO number on the page; a PO is inferred only if exactly one open order covers every line"))
    cur = (blank(m.currency) or "").upper()
    cur_row = next((r for r in scan.rows if any(t in ("inr", "₹", "rs", "rs.") for t in words_of(r))), None)
    if cur == "INR" and cur_row:
        ck._found("currency", cur, cur_row)
        doc["currency"] = "INR"
    else:
        ck._missing("currency", cur or None)
        doc["currency"] = None
    doc["supplier_state_code"] = ck.state_code("supplier state code", blank(m.supplier_state_code))
    doc["place_of_supply_state_code"] = ck.state_code("place of supply", blank(m.place_of_supply_state_code))

    # ---- lines ----
    lines, line_raw = [], []
    for i, ln in enumerate(m.lines, 1):
        qty_t, price_t, amt_t = blank(ln.qty), blank(ln.unit_price), blank(ln.amount)
        qty_v, price_v, amt_v = integer(qty_t), money(price_t), money(amt_t)

        def rows_with(labels, money_mates, qty_mate=None):
            """A row qualifies if it carries the field's label or one of the line's other values."""
            def ok(row):
                if has_label(row, labels):
                    return True
                vals = {page_money(w.text) for w in row.words}
                if any(m is not None and m in vals for m in money_mates):
                    return True
                ints = {int(c) for c in (clean(w.text) for w in row.words) if c.isdigit()}
                return qty_mate is not None and qty_mate in ints
            return ok

        q = ck.number(f"line {i} quantity", qty_t, "qty", rows_with(("qty", "quantity"), [price_v, amt_v]))
        p = ck.number(f"line {i} unit price", price_t, "money", rows_with(("rate", "price", "unit"), [amt_v], qty_v))
        a = ck.number(f"line {i} amount", amt_t, "money", rows_with(("amount", "value", "extended"), [price_v], qty_v))
        region = line_region(scan, [e.row for e in (q, p, a) if e.ok and e.row is not None])
        code_ok = ck.in_region(f"line {i} item code", blank(ln.item_code), region).ok if blank(ln.item_code) else False
        hsn_ok = ck.in_region(f"line {i} HSN", blank(ln.hsn), region).ok if blank(ln.hsn) else False
        desc_ok = ck.in_region(f"line {i} description", blank(ln.description), region, as_phrase=True).ok
        item_code = ln.item_code.strip() if code_ok else None
        description = blank(ln.description) if desc_ok else None
        if not item_code and description:
            # Some layouts print the code inside the description ("HD300 Fleece hoodie 300 GSM"). Use a leading
            # code-like word (letters and digits) as the item code, only if it is printed on this line by itself.
            lead = re.match(r"^\s*((?=[A-Za-z0-9\-/]*[A-Za-z])(?=[A-Za-z0-9\-/]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{1,14})[\s:\-]+(\S.*)$",
                            description)
            if lead:
                code_row = next((r for r in region if lead.group(1).lower() in words_of(r)), None)
                if code_row is not None:
                    item_code, description = lead.group(1), lead.group(2).strip()
                    ck._found(f"line {i} item code", item_code, code_row, note="printed at the start of the description")
        lines.append({
            "item_code": item_code,
            "hsn": ln.hsn.strip() if hsn_ok else None,
            "description": description,
            "qty": integer(qty_t) if q.ok else None,
            "unit_price_paise": int(money(price_t) * 100) if p.ok else None,
            "amount_paise": int(money(amt_t) * 100) if a.ok else None,
        })
        line_raw.append((qty_t, price_t, amt_t))
    doc["lines"] = lines

    # ---- totals and taxes ----
    s = ck.number("subtotal", blank(m.subtotal), "money",
                  lambda r: has_label(r, ("subtotal", "sub-total", "taxable")), reconcile=True)
    doc["subtotal_paise"] = int(money(m.subtotal) * 100) if s.ok else None
    taxes = []
    for j, t in enumerate(m.taxes, 1):
        ttype = (blank(t.type) or "").upper().replace(" ", "")
        type_ok = ttype in ("IGST", "CGST", "SGST", "UTGST")
        tax_row = lambda r, tt=ttype.lower(): tt in words_of(r)
        ev = ck.number(f"{ttype or 'tax'} amount", blank(t.amount), "money", tax_row, reconcile=True)
        want_rate = rate(t.rate)
        rate_row = next((r for r in scan.rows if want_rate is not None and tax_row(r) and
                         any(rate(w.text) == want_rate and "%" in w.text for w in r.words)), None)
        rate_ok = rate_row is not None
        if rate_ok:
            ck._found(f"{ttype or 'tax'} rate", t.rate, rate_row)
        else:
            ck._missing(f"{ttype or 'tax'} rate", t.rate)
        taxes.append({
            "type": ttype if type_ok else None,
            "rate_pct": str(want_rate) if rate_ok else None,
            "amount_paise": int(money(t.amount) * 100) if ev.ok else None,
        })
    doc["taxes"] = taxes
    if blank(m.round_off) and money(m.round_off) not in (None, Decimal("0.00")):
        r = ck.number("round off", m.round_off, "money", lambda row: has_label(row, ("round",)))
        doc["round_off_paise"] = int(money(m.round_off) * 100) if r.ok else None
    else:
        doc["round_off_paise"] = 0
    tot = ck.number("total", blank(m.total), "money", lambda r: has_label(r, ("total",)), reconcile=True)
    doc["total_paise"] = int(money(m.total) * 100) if tot.ok else None

    _settle_slips(ck, doc, line_raw)
    doc["evidence"] = [ev.public() for ev in ck.evidence]
    return doc, ck.evidence


def _settle_slips(ck: Checker, doc: dict, line_raw: list) -> None:
    """Totals block only (tax amounts, subtotal, total). OCR read the amount one digit off: accept the model's
    reading only if amounts OCR DID confirm add up with it and not with OCR's reading, with exactly one unknown.
    Line values (quantity, unit price, line amount) are never settled this way: they must be read directly.
    Decisions use a snapshot of page-confirmed values, so a settled value never helps settle another."""
    if not ck.slips:
        return
    p = lambda v: int(v * 100)
    snap = json.loads(json.dumps(doc))
    confirmed = {e.field for e in ck.evidence if e.status in ("confirmed", "second look")}
    tax_types = ("IGST", "CGST", "SGST", "UTGST")

    def fits(fld: str, value: int):
        """True/False if a confirmed equation decides this value, None if nothing can."""
        lines, taxes = snap["lines"], snap["taxes"]
        if fld.split()[0] in tax_types:
            others = [t["amount_paise"] for t in taxes if t["amount_paise"] is not None]
            if len(others) == len(taxes) - 1 and {"subtotal", "total"} <= confirmed and snap["round_off_paise"] is not None:
                return snap["subtotal_paise"] + sum(others) + snap["round_off_paise"] + value == snap["total_paise"]
        elif fld == "subtotal":
            amounts = [l["amount_paise"] for l in lines]
            if None not in amounts:
                return sum(amounts) == value
        elif fld == "total":
            amounts = [t["amount_paise"] for t in taxes]
            if snap["subtotal_paise"] is not None and None not in amounts and snap["round_off_paise"] is not None:
                return snap["subtotal_paise"] + sum(amounts) + snap["round_off_paise"] == value
        return None

    for fld, (model_v, ocr_v, row) in ck.slips.items():
        if not (fits(fld, p(model_v)) is True and fits(fld, p(ocr_v)) is False):
            continue
        ev = next(e for e in ck.evidence if e.field == fld)
        ev.status, ev.where, ev.page, ev.source, ev.row = "reconciled from totals", row.text, row.page, row.source, row
        ev.note = (f"OCR read {ocr_v:,.2f} here; the confirmed amounts only add up with {model_v:,.2f}. "
                   "Reconciled from totals, not read directly.")
        if fld in ("subtotal", "total"):
            doc[f"{fld}_paise"] = p(model_v)
        else:
            tax = next(t for t in doc["taxes"] if t["type"] == fld.split()[0] and t["amount_paise"] is None)
            tax["amount_paise"] = p(model_v)


# --------------------------------------------------------------------------------------
# The whole step
# --------------------------------------------------------------------------------------
@dataclass
class Extraction:
    doc: Optional[dict]
    processing_error: Optional[str]
    evidence: list
    scanned: bool = False


def run(pdf: bytes, provider, on_stage: Callable[[str], None] = lambda s: None) -> Extraction:
    if len(pdf) > MAX_BYTES:
        return Extraction(None, f"File is larger than {MAX_BYTES // (1024 * 1024)} MB.", [])
    on_stage("Read the PDF")
    try:
        scan = read_pages(pdf)
    except ValueError as exc:
        return Extraction(None, str(exc), [])
    except Exception:
        return Extraction(None, "Could not open this file as a PDF.", [])
    on_stage("Read the scan with OCR" if scan.scanned else "Read the text layer")
    if provider is None:
        return Extraction(None, "No model is configured.", [], scan.scanned)
    on_stage(f"Extract fields ({provider.name})")
    try:
        raw = provider.extract(pdf)
        ModelInvoice.model_validate(raw)
    except ProviderError as exc:
        return Extraction(None, str(exc), [], scan.scanned)
    except ValidationError:
        return Extraction(None, f"{provider.name} returned fields in an unexpected format.", [], scan.scanned)
    on_stage("Check each value against the page")
    doc, evidence = verify(raw, scan)
    return Extraction(doc, None, evidence, scan.scanned)


# --------------------------------------------------------------------------------------
# Test-only provider: replays what a faithful model would return, from the test pack's ground truth.
# The page reading and every check above still run for real.
# --------------------------------------------------------------------------------------
def _inr_text(paise: int) -> str:
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail, groups = s[:-3], s[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{'-' if paise < 0 else ''}{s}.{p:02d}"


def printed_form(gt: dict) -> dict:
    """What a faithful model copies off the page for a ground-truth invoice (dates, buyer line as printed)."""
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    d = gt["invoice_date"]
    date_text = {"A": f"{d[8:10]} {months[int(d[5:7]) - 1]} {d[:4]}", "B": f"{d[8:10]}-{d[5:7]}-{d[:4]}",
                 "C": f"{int(d[8:10])}-{months[int(d[5:7]) - 1]}-{d[2:4]}"}[gt["layout"]]
    buyer = {"A": f"{gt['buyer_name']}, Indore, Madhya Pradesh", "B": f"{gt['buyer_name']}, Indore",
             "C": gt["buyer_name"]}[gt["layout"]]
    return {
        "vendor_name": gt["vendor_name"], "buyer_name": buyer, "invoice_no": gt["invoice_no"],
        "invoice_date": date_text, "po_ref": gt["po_ref"] or "", "currency": "INR",
        "supplier_state_code": gt["supplier_state_code"], "place_of_supply_state_code": gt["place_of_supply_state_code"],
        "lines": [{"item_code": l["item_code"], "hsn": l["hsn"], "description": l["description"],
                   "qty": str(l["qty"]),
                   "unit_price": _inr_text(l["unit_price_paise"]) if l["unit_price_paise"] is not None else "",
                   "amount": _inr_text(l["amount_paise"])} for l in gt["lines"]],
        "subtotal": _inr_text(gt["subtotal_paise"]),
        "taxes": [{"type": t["type"], "rate": f"{t['rate_pct']}%", "amount": _inr_text(t["amount_paise"])}
                  for t in gt["taxes"]],
        "round_off": _inr_text(gt["round_off_paise"]) if gt["round_off_paise"] else "",
        "total": _inr_text(gt["total_paise"]),
    }


class FixtureProvider:
    """Test mode only. Finds the test-pack invoice with the same bytes and replays a faithful model reading."""
    name = "test fixtures"

    def __init__(self, pack: Path):
        self.by_hash = {}
        for pdf in (pack / "invoices").glob("*.pdf"):
            gt = json.loads((pack / "ground_truth" / f"{pdf.stem}.json").read_text())
            self.by_hash[hashlib.sha256(pdf.read_bytes()).hexdigest()] = gt

    def extract(self, pdf: bytes) -> dict:
        gt = self.by_hash.get(hashlib.sha256(pdf).hexdigest())
        if gt is None:
            raise ProviderError("Test mode: this file is not in the test pack")
        return printed_form(gt)
