#!/usr/bin/env python3
"""
Legislative Alpha daily data fetcher.

Pulls two live data sources and writes a single data.json for the site:
  1. Bills + amendments  -- Congress.gov API (requires CONGRESS_API_KEY).
     Each matched bill is tagged with its sector, flagged if it is an
     appropriations/funding bill (with any dollar figures extracted), and
     linked to the constituent stocks positioned to benefit.
  2. Congressional stock trades -- scraped from BOTH chambers' primary
     sources, since no free, currently-maintained API exists for this data:
       - Senate: the electronic financial disclosure search
         (efdsearch.senate.gov), structured HTML tables.
       - House: the Clerk's disclosure site (disclosures-clerk.house.gov),
         a yearly filing index plus per-filing PDFs. E-filed PDFs carry a
         text layer and are parsed; paper filings are scanned images and
         are skipped (counted in the run log).

Everything is matched to the thematic sectors defined in sectors.json;
trades in companies outside every sector's tracked list land in OTHER.
"""

import csv
import io
import json
import math
import os
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

import pdfplumber
import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECTORS_PATH = os.path.join(SCRIPT_DIR, "sectors.json")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "..", "data.json")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "..", "history.json")
HISTORY_MAX_DAYS = 90
PRICES_PATH = os.path.join(SCRIPT_DIR, "..", "prices.json")
SECTOR_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "sector_cache.json")  # ticker -> GICS sector
SECTOR_CACHE_TTL_DAYS = 45  # a company's economic sector changes rarely
BACKTEST_TICKERS = 120   # top tickers by dollar exposure to price (dominate the weighting)
BACKTEST_WINDOW = 365    # calendar days of performance history
PRICE_HISTORY_DAYS = 400 # keep this many daily closes cached per ticker

CONGRESS = 119  # 119th Congress: 2025-2027
BILLS_LOOKBACK_DAYS = 45          # only scan bills updated in this window
MAX_MATCHED_BILLS = 60            # cap how many matched bills we keep
SENATE_TRADES_LOOKBACK_DAYS = 45  # (legacy) short window; superseded by TRADES_LOOKBACK_DAYS
TRADES_LOOKBACK_DAYS = 400         # scrape ~13 months of filings so the 1-year backtest is dense
TRADES_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "trades_cache.json")  # per-filing raw txns
INSIDER_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "insider_cache.json")  # SEC Form 4 filings
INSIDER_FILINGS_PER_RUN = 600     # recent Form 4 filings to scan per run (1 req each)
INSIDER_MAX_DAYS = 120            # keep insider transactions from this window
DISCLOSURE_MAX_LAG = 400          # filings later than this are stale amendments, not signal
MIN_SCORED_BUYS_BACKEND = 3       # below this a member's record is noise, not a record
MIN_VALIDATION_N = 40             # below this a score validation is provisional, not a verdict
REQUEST_TIMEOUT = 20
USER_AGENT = "legislative-alpha-tracker/1.0 (personal project; contact via github repo)"
SEC_HEADERS = {"User-Agent": "Legislative Alpha research tracker admin@legislative-alpha.example",
               "Accept-Encoding": "gzip, deflate"}

CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
IMPACT_MODEL = "claude-opus-4-8"

# Exchange/security-type boilerplate that clutters raw security names from the
# NASDAQ/SEC feeds ("Meta Platforms, Inc. - Class A Common Stock"). We strip the
# trailing share-class / security-type tail for display, but keep the corporate
# form (Inc./Corp.) so names stay recognizable.
# The security-type boilerplate always BEGINS with one of these markers; the real
# company name is everything before the first one. Cutting at the marker is far
# more robust than trying to match every trailing variant ("Class A Ordinary
# Shares (Ireland)", "Common Stock New", "Class B Sub. Vot. Common Stock", ...).
_NAME_MARKER_RE = re.compile(
    r"\s+(?:-\s+)?(?:"
    r"Class\s+[A-Z]\b|Common\s+Stock\b|Common\s+Shares?\b|Capital\s+Stock\b|"
    r"Ordinary\s+Shares?\b|American\s+Depositary\b|Depositary\s+(?:Shares?|Units?|Receipts?)\b|"
    r"Subordinate\s+Voting\b|Sub\.\s*Vot\.|Beneficial\s+Interest\b|Shares\s+Representing\b|"
    r"Preferred\b|Warrants?\b|Units?\b|Rights?\b"
    r")", re.IGNORECASE)


def _clean_company_name(name):
    """Reduce a raw security name to just the company: cut parse garbage that
    leaks in from option disclosures / scanned-PDF filings, then drop the
    security-type boilerplate at the first marker. Idempotent."""
    if not name:
        return name
    s = " ".join(str(name).split())  # collapse runaway whitespace from PDFs
    # cut obvious parse/option-contract garbage that trails the real name
    for cut in ("Option Type:", " ID Owner ", " Transaction Date ", " Notification "):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    m = _NAME_MARKER_RE.search(s)
    if m and m.start() > 0:
        s = s[:m.start()]
    return s.strip().rstrip(" -,") or name


def load_sectors():
    with open(SECTORS_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_ticker_index(sectors):
    """ticker -> list of {sector, company, lda_search}.

    A handful of stocks are genuinely multi-thematic (NVIDIA shows up under
    both Quantum Computing and Semiconductors & AI; Lockheed Martin under
    both Defense and Space). Each ticker maps to a LIST so a real trade or
    lobbying filing in that ticker is attributed to every sector it belongs
    to, rather than only the last one a plain dict overwrite happened to
    keep."""
    index = {}
    for code, sector in sectors.items():
        for ticker, info in sector["constituents"].items():
            index.setdefault(ticker, []).append({"sector": code, "company": info["name"], "lda_search": info["lda_search"]})
    return index


# ---------------------------------------------------------------------------
# 1. Congress.gov -- bills + amendments
# ---------------------------------------------------------------------------

def congress_get(path, params=None, max_retries=3):
    if not CONGRESS_API_KEY:
        raise RuntimeError("CONGRESS_API_KEY environment variable is not set")
    params = dict(params or {})
    params["api_key"] = CONGRESS_API_KEY
    params["format"] = "json"
    url = f"https://api.congress.gov/v3/{path}"
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
    raise last_error


_SECTOR_KEYWORD_PATTERNS = None


def _build_keyword_patterns(sectors):
    """Compile one word-boundary regex per sector so e.g. the keyword
    "transit" doesn't false-positive-match inside "transition"."""
    global _SECTOR_KEYWORD_PATTERNS
    if _SECTOR_KEYWORD_PATTERNS is None:
        _SECTOR_KEYWORD_PATTERNS = {}
        for code, sector in sectors.items():
            # Trade-only economic sectors carry no keywords -- skip them so bills
            # are never matched by an empty alternation (which matches everything).
            if not sector.get("keywords"):
                continue
            alternation = "|".join(re.escape(kw) for kw in sector["keywords"])
            _SECTOR_KEYWORD_PATTERNS[code] = re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)
    return _SECTOR_KEYWORD_PATTERNS


def match_sector(title, sectors):
    """Whole-word, case-insensitive keyword match against each sector's
    keyword list. Returns the first matching sector code, or None."""
    patterns = _build_keyword_patterns(sectors)
    for code, pattern in patterns.items():
        if pattern.search(title):
            return code
    return None


def derive_status(latest_action_text):
    text = (latest_action_text or "").lower()
    if "became public law" in text or "signed by president" in text:
        return "Signed"
    if "vetoed" in text:
        return "Vetoed"
    if "passed senate" in text or "passed/agreed to in senate" in text:
        return "Passed Senate"
    if "passed house" in text or "passed/agreed to in house" in text:
        return "Passed House"
    if "referred to" in text or "received in" in text or "committee" in text:
        return "Committee"
    return "Introduced"


STAGE_SCORE = {
    "Introduced": 15,
    "Committee": 35,
    "Passed House": 65,
    "Passed Senate": 65,
    "Signed": 92,
    "Vetoed": 8,
}


def compute_momentum(bill, cosponsor_count):
    stage = STAGE_SCORE.get(bill["status"], 20)
    cosponsor_bonus = min(cosponsor_count * 0.4, 15)
    recency_bonus = 0
    try:
        action_date = datetime.strptime(bill["latest_action"]["date"], "%Y-%m-%d")
        if (datetime.now() - action_date).days <= 14:
            recency_bonus = 8
    except (KeyError, ValueError, TypeError):
        pass
    return max(0, min(100, round(stage + cosponsor_bonus + recency_bonus)))


# Titles that mark a spending/authorization measure -- these bills are ALWAYS
# kept (the money bills are the point), even when no thematic keyword matches.
APPROP_TITLE = re.compile(
    r"\b(appropriations?|authorization act|reauthoriz\w+|"
    r"omnibus|continuing resolution|supplemental appropriations|"
    r"consolidated appropriations|making appropriations)\b",
    re.IGNORECASE,
)


def fetch_bills(sectors):
    print("Fetching recently updated bills from Congress.gov...", file=sys.stderr)
    from_dt = (datetime.now(timezone.utc) - timedelta(days=BILLS_LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    matched, approps = [], []   # thematic bills, and appropriations bills (always kept)
    seen = set()
    offset = 0
    page_size = 250
    max_pages = 4  # scan up to 1000 recently-updated bills (don't early-stop -- find approps too)
    for _ in range(max_pages):
        data = congress_get(
            f"bill/{CONGRESS}",
            {"sort": "updateDate+desc", "limit": page_size, "offset": offset, "fromDateTime": from_dt},
        )
        bills = data.get("bills", [])
        if not bills:
            break
        for b in bills:
            title = b.get("title") or ""
            is_appr = bool(APPROP_TITLE.search(title))
            sector = match_sector(title, sectors)
            # Appropriations bills that match no theme still get kept, in the
            # cross-cutting "Appropriations & Budget" bucket.
            if not sector and is_appr:
                sector = "APPRO"
            if not sector:
                continue
            bill_type = b.get("type", "").upper()
            number = b.get("number")
            bid = f"{CONGRESS}-{bill_type}-{number}"
            if bid in seen:
                continue
            seen.add(bid)
            latest_action = b.get("latestAction") or {}
            record = {
                "id": bid,
                "number": f"{bill_type} {number}",
                "title": title,
                "sector": sector,
                "introduced_date": None,  # filled in by detail call below
                "latest_action": {"date": latest_action.get("actionDate"), "text": latest_action.get("text")},
                "congress_gov_url": f"https://www.congress.gov/bill/{CONGRESS}th-congress/{b.get('originChamber', '').lower()}-bill/{number}",
                "amendments_count": (b.get("amendments") or {}).get("count", 0),
                "amendments_url": (b.get("amendments") or {}).get("url"),
            }
            record["status"] = derive_status(record["latest_action"]["text"])
            (approps if is_appr else matched).append(record)
        offset += page_size
        time.sleep(0.2)

    # Always keep every appropriations bill; fill the remaining slots with the
    # most-recent thematic bills so the total stays bounded for detail fetching.
    result = approps + matched[: max(0, MAX_MATCHED_BILLS - len(approps))]
    print(f"  matched {len(result)} bills ({len(approps)} appropriations/authorization, {len(result) - len(approps)} thematic)", file=sys.stderr)
    return result


def fetch_bill_details(bill):
    """Fetch sponsor, committee, introduced date, cosponsor count, and amendments."""
    bill_type_map = {"HR": "hr", "S": "s", "HJRES": "hjres", "SJRES": "sjres",
                      "HCONRES": "hconres", "SCONRES": "sconres", "HRES": "hres", "SRES": "sres"}
    parts = bill["number"].split(" ", 1)
    bill_type = bill_type_map.get(parts[0].replace(".", "").upper(), parts[0].lower())
    number = parts[1] if len(parts) > 1 else ""
    try:
        detail = congress_get(f"bill/{CONGRESS}/{bill_type}/{number}")["bill"]
    except requests.RequestException as e:
        print(f"  WARN: could not fetch details for {bill['number']}: {e}", file=sys.stderr)
        return bill, 0

    bill["introduced_date"] = detail.get("introducedDate")
    sponsors = detail.get("sponsors") or []
    if sponsors:
        s = sponsors[0]
        bill["sponsor"] = {
            "name": s.get("fullName"),
            "party": s.get("party"),
            "state": s.get("state"),
        }
    else:
        bill["sponsor"] = {"name": "Unknown", "party": "", "state": ""}
    committees = (detail.get("committees") or {})
    bill["committee_count"] = committees.get("count", 0)
    cosponsor_count = (detail.get("cosponsors") or {}).get("count", 0)
    bill["cosponsor_count"] = cosponsor_count
    return bill, cosponsor_count


def fetch_amendments(bill):
    if not bill.get("amendments_url") or bill.get("amendments_count", 0) == 0:
        return []
    amendments = None
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(
                bill["amendments_url"],
                params={"api_key": CONGRESS_API_KEY, "format": "json", "limit": 10},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            amendments = resp.json().get("amendments", [])
            break
        except requests.RequestException as e:
            last_error = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if amendments is None:
        print(f"  WARN: could not fetch amendments for {bill['number']}: {last_error}", file=sys.stderr)
        return []

    out = []
    for a in amendments[:10]:
        latest = a.get("latestAction") or {}
        out.append({
            "number": f"{a.get('type', '')} {a.get('number', '')}".strip(),
            "purpose": a.get("purpose") or a.get("description") or "",
            "submitted_date": a.get("submittedDate") or a.get("updateDate"),
            "latest_action": latest.get("text"),
        })
    return out


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html):
    """Congress.gov summaries are HTML. Flatten to readable plain text."""
    text = re.sub(r"</p\s*>", "\n\n", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                .replace("&quot;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_bill_summary(bill):
    """CRS-written summary of what the bill actually does. Returns plain text
    of the most recent summary version, or '' if none has been published yet
    (common for freshly-introduced bills)."""
    bill_type_map = {"HR": "hr", "S": "s", "HJRES": "hjres", "SJRES": "sjres",
                     "HCONRES": "hconres", "SCONRES": "sconres", "HRES": "hres", "SRES": "sres"}
    parts = bill["number"].split(" ", 1)
    bill_type = bill_type_map.get(parts[0].replace(".", "").upper(), parts[0].lower())
    number = parts[1] if len(parts) > 1 else ""
    try:
        data = congress_get(f"bill/{CONGRESS}/{bill_type}/{number}/summaries")
    except requests.RequestException as e:
        print(f"  WARN: could not fetch summary for {bill['number']}: {e}", file=sys.stderr)
        return ""
    summaries = data.get("summaries") or []
    if not summaries:
        return ""
    # summaries are chronological; the last one is the most current version
    latest = summaries[-1]
    return _html_to_text(latest.get("text", ""))


def enrich_bills(bills):
    print("Fetching summary / sponsor / cosponsor / amendment detail for matched bills...", file=sys.stderr)
    for bill in bills:
        bill, cosponsor_count = fetch_bill_details(bill)
        bill["amendments"] = fetch_amendments(bill)
        bill["summary"] = fetch_bill_summary(bill)
        bill["momentum"] = compute_momentum(bill, cosponsor_count)
        time.sleep(0.15)
    return bills


# ---------------------------------------------------------------------------
# 2. Senate stock trades -- scraped from efdsearch.senate.gov
# ---------------------------------------------------------------------------

EFD_BASE = "https://efdsearch.senate.gov"


def efd_open_session():
    """Perform the required disclaimer handshake and return an authenticated
    requests.Session plus the CSRF token to send on subsequent POSTs."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    resp = session.get(f"{EFD_BASE}/search/", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not token_input:
        raise RuntimeError("Could not find csrfmiddlewaretoken on eFD home page -- site layout may have changed")
    form_token = token_input["value"]

    agree_resp = session.post(
        f"{EFD_BASE}/search/home/",
        data={"csrfmiddlewaretoken": form_token, "prohibition_agreement": "1"},
        headers={"Referer": f"{EFD_BASE}/search/home/", "Origin": EFD_BASE},
        timeout=REQUEST_TIMEOUT,
    )
    agree_resp.raise_for_status()

    csrf_cookie = session.cookies.get("csrftoken")
    if not csrf_cookie:
        raise RuntimeError("eFD session did not return a csrftoken cookie after agreement POST")
    return session, csrf_cookie


LINK_RE = re.compile(r'href="(?P<path>/search/view/ptr/[^"]+)"[^>]*>(?P<label>[^<]+)<')


def search_ptr_reports(session, csrf_token, start_date, end_date):
    """Paginate through the PTR (Periodic Transaction Report) search results
    for the given date range. Returns a list of dicts with report metadata."""
    reports = []
    start = 0
    length = 100
    while True:
        resp = session.post(
            f"{EFD_BASE}/search/report/data/",
            data={
                "report_types": "[11]",
                "filer_types": "[]",
                "submitted_start_date": start_date.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": end_date.strftime("%m/%d/%Y 23:59:59"),
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "draw": "1",
                "start": str(start),
                "length": str(length),
            },
            headers={
                "Referer": f"{EFD_BASE}/search/",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrf_token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", [])
        for row in rows:
            first_name, last_name, display, link_html, filed_date = row[:5]
            m = LINK_RE.search(link_html)
            if not m:
                continue
            # The display column labels the filer, e.g.
            # "Armstrong, Alan (Senator)" or "Smith, Jane (Candidate)".
            # Keep only sitting senators -- candidates and other filers file
            # PTRs too, but the "follow a member of Congress" framing is about
            # people currently holding office.
            if "candidate" in (display or "").lower():
                continue
            name = re.sub(r"\s+", " ", f"{first_name} {last_name}".strip()).strip(" ,")
            reports.append({
                "senator": name,
                "report_path": m.group("path"),
                "filed_date": filed_date,
            })
        start += length
        if start >= payload.get("recordsFiltered", 0):
            break
        time.sleep(0.3)
    return reports


def fetch_ptr_transactions(session, report_path):
    """GET one PTR report and parse its transactions table. Older paper
    filings render as an embedded PDF instead of a table -- skip those,
    since PDF text extraction is out of scope for this script."""
    resp = session.get(f"{EFD_BASE}{report_path}", timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"class": "table-striped"})
    if not table or not table.find("tbody"):
        return []  # paper/PDF-only filing

    out = []
    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        _, tx_date, owner, ticker, asset_name, asset_type, tx_type, amount = cells[:8]
        ticker = ticker.strip()
        out.append({
            # "--" means a non-ticker asset (bond, fund, etc.) -- still a trade
            "ticker": ticker.upper() if ticker and ticker != "--" else None,
            "asset_name": asset_name,
            "asset_type": asset_type,
            "owner": owner,
            "type": tx_type,
            "transaction_date": tx_date,
            "amount_range": amount,
        })
    return out


TX_FIELDS = ("ticker", "asset_name", "transaction_date", "type", "amount_range")


def load_trades_cache():
    if os.path.exists(TRADES_CACHE_PATH):
        try:
            c = json.load(open(TRADES_CACHE_PATH, encoding="utf-8"))
            c.setdefault("senate", {}); c.setdefault("house", {})
            return c
        except (json.JSONDecodeError, OSError):
            pass
    return {"senate": {}, "house": {}}


def save_trades_cache(cache):
    json.dump(cache, open(TRADES_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))


def scrape_senate_filings(cache, start_date, end_date):
    """Enumerate Senate PTR filings in the window and fetch/parse only the ones
    not already cached (each filing's raw transactions are stored once)."""
    print("Scraping Senate PTRs (efdsearch.senate.gov)...", file=sys.stderr)
    try:
        session, csrf_token = efd_open_session()
    except (requests.RequestException, RuntimeError) as e:
        print(f"  WARN: could not open eFD session: {e}", file=sys.stderr)
        return
    try:
        reports = search_ptr_reports(session, csrf_token, start_date, end_date)
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN: PTR search failed: {e}", file=sys.stderr)
        return

    sc = cache["senate"]
    todo = [r for r in reports if r["report_path"] not in sc]
    print(f"  {len(reports)} filings in window, {len(todo)} new to fetch...", file=sys.stderr)
    fetched = 0
    for i, r in enumerate(todo):
        try:
            txns = fetch_ptr_transactions(session, r["report_path"])
        except requests.RequestException:
            continue
        sc[r["report_path"]] = {
            "member": f"Sen. {r['senator']}",
            "chamber": "Senate",
            "filed_date": r["filed_date"],
            "report_url": f"{EFD_BASE}{r['report_path']}",
            "txns": [{k: tx[k] for k in TX_FIELDS} for tx in txns],
        }
        fetched += 1
        time.sleep(0.2)
        if fetched % 100 == 0:
            print(f"  ...senate {fetched}/{len(todo)}", file=sys.stderr)
            save_trades_cache(cache)  # checkpoint the backfill
    print(f"  senate: {fetched} newly cached, {len(sc)} total filings", file=sys.stderr)


# ---------------------------------------------------------------------------
# 2b. House stock trades -- Clerk of the House disclosure PDFs
# ---------------------------------------------------------------------------

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"

# A transaction row inside an e-filed House PTR, e.g.
#   "SP Intel Corporation - Common Stock P 05/29/2026 05/29/2026 $1,000,001 -"
#   "(INTC) [OP] $5,000,000"
HOUSE_TX_ANCHOR = re.compile(
    r"(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<tx_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif_date>\d{2}/\d{2}/\d{4})\s+"
    r"\$(?P<lo>[\d,]+)(?:\s*-\s*\$(?P<hi>[\d,]+))?"
)
HOUSE_TICKER = re.compile(r"\(([A-Z0-9.]{1,7})\)")
HOUSE_DOLLAR = re.compile(r"\$([\d,]+)")
HOUSE_OWNER = re.compile(r"^(SP|JT|DC)\s+")
HOUSE_TYPE_MAP = {"P": "Purchase", "S": "Sale", "S (partial)": "Sale (Partial)", "E": "Exchange"}


def _clean_house_asset(text):
    text = HOUSE_TICKER.sub("", text)
    text = re.sub(r"\[?[A-Z]{2}\]", "", text)   # asset-type codes like [ST]
    text = re.sub(r"\s+", " ", text).strip(" -[]")
    return text[:90]


def fetch_house_ptr_index(year):
    """Download the Clerk's yearly filing index and return PTR entries."""
    resp = requests.get(
        f"{HOUSE_BASE}/financial-pdfs/{year}FD.zip",
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
    entries = []
    for m in root.findall("Member"):
        if m.findtext("FilingType") != "P":
            continue
        entries.append({
            "name": f"{m.findtext('First', '')} {m.findtext('Last', '')}".strip(),
            "state_district": m.findtext("StateDst", ""),
            "filed_date": m.findtext("FilingDate", ""),
            "doc_id": m.findtext("DocID", ""),
            "year": year,
        })
    return entries


def parse_house_ptr_pdf(pdf_bytes):
    """Parse an e-filed House PTR's transaction table. Returns None for
    paper filings (scanned images with no text layer)."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return None
    if not text.strip():
        return None

    lines = [l.strip() for l in text.replace("\x00", " ").splitlines()]
    txs = []
    for idx, line in enumerate(lines):
        a = HOUSE_TX_ANCHOR.search(line)
        if not a:
            continue
        pre = line[: a.start()].strip()
        owner_m = HOUSE_OWNER.match(pre)
        owner = owner_m.group(1) if owner_m else "Self"
        asset = HOUSE_OWNER.sub("", pre).strip()
        pre_tickers = HOUSE_TICKER.findall(pre)
        ticker = pre_tickers[-1] if pre_tickers else None
        hi = a.group("hi")
        # Asset name / ticker / amount-upper-bound can wrap onto the next
        # line or two. Status/description lines contain ":" -- stop there,
        # or at the next transaction row.
        for nxt in lines[idx + 1: idx + 4]:
            if HOUSE_TX_ANCHOR.search(nxt) or ":" in nxt or nxt.startswith("* For the complete"):
                break
            if ticker is None:
                tk = HOUSE_TICKER.search(nxt)
                if tk:
                    ticker = tk.group(1)
            if hi is None:
                d = HOUSE_DOLLAR.search(nxt)
                if d:
                    hi = d.group(1)
            remainder = _clean_house_asset(HOUSE_DOLLAR.sub("", nxt))
            if remainder:
                asset = f"{asset} {remainder}"
        txs.append({
            "ticker": ticker,
            "asset_name": _clean_house_asset(asset),
            "owner": owner,
            "type": HOUSE_TYPE_MAP[a.group("type")],
            "transaction_date": a.group("tx_date"),
            "amount_range": f"${a.group('lo')} - ${hi}" if hi else f"${a.group('lo')}",
        })
    return txs


def scrape_house_filings(cache, start_date, end_date):
    """Enumerate House PTR filings in the window and download/parse only the
    ones not already cached. Paper (scanned) filings are cached as empty so we
    don't re-download them each run."""
    print("Scraping House PTRs (disclosures-clerk.house.gov)...", file=sys.stderr)
    years = sorted({start_date.year, end_date.year, end_date.year - 1})
    reports = []
    for year in years:
        try:
            reports.extend(fetch_house_ptr_index(year))
        except (requests.RequestException, zipfile.BadZipFile, ET.ParseError, StopIteration) as e:
            print(f"  WARN: could not fetch House index for {year}: {e}", file=sys.stderr)

    in_window = []
    for r in reports:
        try:
            filed = datetime.strptime(r["filed_date"], "%m/%d/%Y")
        except ValueError:
            continue
        if start_date <= filed <= end_date:
            in_window.append(r)

    hc = cache["house"]
    todo = [r for r in in_window if r["doc_id"] not in hc]
    print(f"  {len(in_window)} filings in window, {len(todo)} new to fetch...", file=sys.stderr)
    fetched = paper = 0
    for r in todo:
        pdf_url = f"{HOUSE_BASE}/ptr-pdfs/{r['year']}/{r['doc_id']}.pdf"
        try:
            resp = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        transactions = parse_house_ptr_pdf(resp.content)
        if transactions is None:
            hc[r["doc_id"]] = {"paper": True, "txns": []}  # cache the skip
            paper += 1
        else:
            hc[r["doc_id"]] = {
                "member": f"Rep. {r['name']} ({r['state_district']})",
                "chamber": "House",
                "filed_date": r["filed_date"],
                "report_url": pdf_url,
                "txns": [{k: tx[k] for k in TX_FIELDS} for tx in transactions],
            }
            fetched += 1
        time.sleep(0.25)
        if (fetched + paper) % 100 == 0:
            print(f"  ...house {fetched + paper}/{len(todo)}", file=sys.stderr)
            save_trades_cache(cache)
    print(f"  house: {fetched} newly cached, {paper} paper skipped, {len(hc)} total filings", file=sys.stderr)


def build_trades_from_cache(cache, ticker_index, start_date):
    """Rebuild the flat trades list from every cached filing, applying the
    current sector matching. Cheap, so sector logic can evolve without rescraping."""
    trades = []
    seen = set()
    future = datetime.now() + timedelta(days=1)  # a trade can't have happened tomorrow
    dropped_future = 0
    for source in ("senate", "house"):
        for f in cache.get(source, {}).values():
            if not f.get("txns"):
                continue
            try:
                if datetime.strptime(f["filed_date"], "%m/%d/%Y") < start_date:
                    continue
            except (ValueError, KeyError):
                pass
            for tx in f["txns"]:
                # Drop data-entry errors with an impossible future transaction date.
                txd = _parse_mdy(tx["transaction_date"])
                if txd != datetime.min and txd > future:
                    dropped_future += 1
                    continue
                matches = (ticker_index.get(tx["ticker"]) if tx["ticker"] else None) or [
                    {"sector": "OTHER", "company": tx["asset_name"]}
                ]
                for info in matches:
                    key = (f["member"], tx["ticker"], tx["asset_name"], tx["transaction_date"],
                           tx["type"], tx["amount_range"], info["sector"])
                    if key in seen:
                        continue
                    seen.add(key)
                    trades.append({
                        "sector": info["sector"],
                        "ticker": tx["ticker"],
                        "company": info["company"],
                        "member": f["member"],
                        "chamber": f["chamber"],
                        "transaction_date": tx["transaction_date"],
                        "filed_date": f["filed_date"],
                        "type": tx["type"],
                        "amount_range": tx["amount_range"],
                        "report_url": f["report_url"],
                    })
    matched = sum(1 for t in trades if t["sector"] != "OTHER")
    print(f"  built {len(trades)} trades from cache ({matched} matched a tracked sector; "
          f"dropped {dropped_future} with future dates)", file=sys.stderr)
    return trades


# ---------------------------------------------------------------------------
# 2c. Corporate insider trades -- SEC Form 4 filings (EDGAR).
# Officers, directors and 10% owners buying/selling their OWN company's stock.
# Open-market purchases (code P) are the classic bullish "insiders are buying"
# signal; sales (code S) are noisier. Free from SEC EDGAR (needs a real UA).
# ---------------------------------------------------------------------------

EDGAR_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
SEC_TXN_KEEP = {"P", "S"}  # open-market purchase / sale (skip grants, option exercises, gifts)


def load_insider_cache():
    if os.path.exists(INSIDER_CACHE_PATH):
        try:
            return json.load(open(INSIDER_CACHE_PATH, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_insider_cache(cache):
    json.dump(cache, open(INSIDER_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))


def fetch_recent_form4_links(count):
    """(accession, index_url) for the most recent Form 4 filings across EDGAR."""
    out = []
    for start in range(0, count, 100):
        try:
            r = requests.get("https://www.sec.gov/cgi-bin/browse-edgar",
                             params={"action": "getcurrent", "type": "4", "output": "atom",
                                     "count": 100, "start": start},
                             headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                break
            root = ET.fromstring(r.text)
        except (requests.RequestException, ET.ParseError):
            break
        entries = root.findall("a:entry", EDGAR_ATOM_NS)
        if not entries:
            break
        for e in entries:
            link = e.find("a:link", EDGAR_ATOM_NS)
            href = link.get("href") if link is not None else None
            if not href:
                continue
            m = re.search(r"/(\d{10}-\d{2}-\d{6})-index", href)
            out.append((m.group(1) if m else href, href))
        time.sleep(0.2)
    return out


def _f4_text(parent, path):
    if parent is None:
        return None
    el = parent.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(index_url):
    """Fetch a Form 4's XML and return {ticker, company, insider, roles, txns[]}.
    Tries the standard primary_doc.xml directly (1 request) to stay well under
    SEC's 10 req/sec limit; falls back to scraping the index page if needed."""
    folder = index_url.rsplit("/", 1)[0]
    try:
        r = requests.get(folder + "/primary_doc.xml", headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200 or "<issuer" not in r.text[:4000]:
            idx = requests.get(index_url, headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT)
            m = re.search(r'href="([^"]*\.xml)"', idx.text)
            if not m:
                return None
            path = m.group(1)
            xml_url = ("https://www.sec.gov" + path) if path.startswith("/") else path
            r = requests.get(xml_url, headers=SEC_HEADERS, timeout=REQUEST_TIMEOUT)
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError):
        return None

    issuer = root.find("issuer")
    ticker = _f4_text(issuer, "issuerTradingSymbol")
    if ticker:
        ticker = ticker.upper().strip()
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if _f4_text(rel, "isDirector") in ("1", "true"):
        roles.append("Director")
    if _f4_text(rel, "isOfficer") in ("1", "true"):
        roles.append(_f4_text(rel, "officerTitle") or "Officer")
    if _f4_text(rel, "isTenPercentOwner") in ("1", "true"):
        roles.append("10% owner")

    txns = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = _f4_text(t, "transactionCoding/transactionCode")
        if code not in SEC_TXN_KEEP:
            continue
        try:
            shares = float(_f4_text(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(_f4_text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except (TypeError, ValueError):
            shares = price = 0
        txns.append({
            "code": code,  # P=purchase, S=sale
            "date": _f4_text(t, "transactionDate/value"),
            "shares": round(shares),
            "price": round(price, 2),
            "value": round(shares * price),
        })
    return {"ticker": ticker, "company": _f4_text(issuer, "issuerName"),
            "insider": _f4_text(owner, "reportingOwnerId/rptOwnerName"), "roles": roles, "txns": txns}


def scrape_insider_filings(cache):
    """Fetch recent Form 4 filings and parse the ones we haven't cached yet.
    Cache is keyed by accession number; filings with no open-market P/S trades are
    cached as empty so they aren't re-fetched."""
    print("Scraping SEC Form 4 insider filings (edgar)...", file=sys.stderr)
    try:
        links = fetch_recent_form4_links(INSIDER_FILINGS_PER_RUN)
    except requests.RequestException as e:
        print(f"  WARN: could not list Form 4 filings: {e}", file=sys.stderr)
        return
    todo = [(acc, url) for acc, url in links if acc not in cache]
    print(f"  {len(links)} recent filings, {len(todo)} new to parse...", file=sys.stderr)
    fetched = kept = 0
    for i, (acc, url) in enumerate(todo):
        parsed = parse_form4(url)
        if parsed is None:
            cache[acc] = {"txns": []}  # unparseable -> don't retry
        else:
            parsed["url"] = url
            cache[acc] = parsed
            if parsed["txns"]:
                kept += 1
        fetched += 1
        time.sleep(0.15)
        if fetched % 100 == 0:
            print(f"  ...insiders {fetched}/{len(todo)}", file=sys.stderr)
            save_insider_cache(cache)
    print(f"  insiders: {fetched} parsed ({kept} with open-market trades), {len(cache)} cached total", file=sys.stderr)


_EXEC_TITLES = re.compile(r"chief|ceo|cfo|coo|\bc[a-z]?o\b|president|chairman|chair\b|founder|treasurer|principal officer", re.I)


# Base conviction by role. The academic insider-alpha literature (Lakonishok &
# Lee; Cohen, Malloy & Pomorski) finds executive/officer purchases carry the
# strongest predictive signal, directors weaker, and passive 10% holders (funds)
# the weakest -- so weight them accordingly.
ROLE_W = {"exec": 45, "director": 30, "owner": 18, "insider": 12}


def _role_tier(roles):
    """exec (C-suite) > director > owner (10%) > insider -- for whale ranking."""
    blob = " ".join(roles or [])
    if _EXEC_TITLES.search(blob):
        return "exec"
    if re.search(r"director", blob, re.I):
        return "director"
    if "10%" in blob:
        return "owner"
    return "insider"


def build_insider_data(cache, prices=None, cong_buyers=None):
    """Aggregate cached Form 4s into whale buys, a feed, per-ticker signals and
    cluster buys. 'Whales' = the biggest open-market bets insiders make on their
    own stock, with C-suite (CEO/CFO) buys flagged as the strongest conviction.

    When `prices` is supplied, each buy is turned into an alpha signal: the stock's
    return SINCE the insider bought (and the excess over the S&P), whether they
    bought into a dip (opportunistic), the trend, and a 0-100 conviction score."""
    cutoff = datetime.now() - timedelta(days=INSIDER_MAX_DAYS)
    feed = []
    for acc, f in cache.items():
        if not f.get("txns") or not f.get("ticker"):
            continue
        roles = f.get("roles", [])
        tier = _role_tier(roles)
        for tx in f["txns"]:
            try:
                d = datetime.strptime(tx["date"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if d < cutoff:
                continue
            feed.append({
                "ticker": f["ticker"], "company": f.get("company"),
                "insider": f.get("insider"), "roles": roles, "tier": tier,
                "code": tx["code"], "buy": tx["code"] == "P",
                "date": tx["date"], "shares": tx["shares"], "price": tx["price"], "value": tx["value"],
                "url": f.get("url"),
            })
    feed.sort(key=lambda x: x["date"], reverse=True)
    buys = [x for x in feed if x["buy"]]
    sells = [x for x in feed if not x["buy"]]

    # per-ticker signals
    sig = {}
    for x in feed:
        s = sig.setdefault(x["ticker"], {"ticker": x["ticker"], "company": x["company"],
                                         "buyers": set(), "sellers": set(), "buy_value": 0, "sell_value": 0,
                                         "buy_count": 0, "sell_count": 0})
        if x["buy"]:
            s["buyers"].add(x["insider"]); s["buy_value"] += x["value"]; s["buy_count"] += 1
        else:
            s["sellers"].add(x["insider"]); s["sell_value"] += x["value"]; s["sell_count"] += 1
    signals = [{"ticker": s["ticker"], "company": s["company"],
                "n_buyers": len(s["buyers"]), "n_sellers": len(s["sellers"]),
                "buy_value": s["buy_value"], "sell_value": s["sell_value"],
                "buy_count": s["buy_count"], "sell_count": s["sell_count"],
                "net_value": s["buy_value"] - s["sell_value"]} for s in sig.values()]

    clusters = sorted([s for s in signals if s["n_buyers"] >= 2],
                      key=lambda s: (s["n_buyers"], s["buy_value"]), reverse=True)[:24]

    total_buy_value = sum(x["value"] for x in buys)
    exec_buys = [x for x in buys if x["tier"] == "exec"]
    biggest = max(buys, key=lambda x: x["value"]) if buys else None

    # Whales: aggregate an insider's repeat buys of the same stock into one bet
    # (so a 10% owner accumulating over a week shows as a single big card).
    whale_agg = {}
    for x in buys:
        k = (x["ticker"], x["insider"])
        w = whale_agg.get(k)
        if w is None:
            whale_agg[k] = {**x, "n": 1}
        else:
            w["value"] += x["value"]; w["shares"] += x["shares"]; w["n"] += 1
            if x["date"] < w["date"]:
                w["date"] = x["date"]  # earliest accumulation date -> longest track record
    for w in whale_agg.values():
        if w.get("shares"):
            w["price"] = round(w["value"] / w["shares"], 2)  # value-weighted cost basis
    whale_buys = sorted(whale_agg.values(), key=lambda w: w["value"], reverse=True)

    # ---- Alpha layer -----------------------------------------------------------
    tk_buyers = {tk: len(s["buyers"]) for tk, s in sig.items()}  # cluster size per ticker
    spy = (prices or {}).get("SPY")
    spy_last = spy[max(spy)] if spy else None
    cong = cong_buyers or {}

    def enrich(x):
        """Attach return-since-bought, opportunistic dip, trend + conviction score."""
        series = (prices or {}).get(x["ticker"])
        x["since_pct"] = x["since_excess"] = x["pre30"] = x["above200"] = None
        entry = x.get("price") or (_nearest_close(series, x["date"]) if series else None)
        last = series[max(series)] if series else None
        if series and entry and last:
            x["since_pct"] = round((last / entry - 1) * 100, 1)
            # micro-cap Form 4 data is split/currency-glitch prone: a >300% or
            # <-95% "return since bought" is almost always a bad cost basis, so
            # drop it rather than let it poison the aggregate or the leaderboard.
            if not (-95 <= x["since_pct"] <= 300):
                x["since_pct"] = None
            elif spy and spy_last:
                se = _nearest_close(spy, x["date"])
                if se:
                    x["since_excess"] = round(x["since_pct"] - (spy_last / se - 1) * 100, 1)
            bd = _nearest_close(series, x["date"])
            try:
                prior = (datetime.strptime(x["date"], "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
                pre = _nearest_close(series, prior)
            except (ValueError, TypeError):
                pre = None
            if bd and pre:
                x["pre30"] = round((bd / pre - 1) * 100, 1)
            sma200 = _sma([c for _, c in sorted(series.items())], 200)
            if sma200:
                x["above200"] = last > sma200
        nb = tk_buyers.get(x["ticker"], 1)
        cb = cong.get(x["ticker"], 0)
        x["n_insiders"] = nb
        x["congress"] = cb
        x["opportunistic"] = bool(x.get("pre30") is not None and x["pre30"] < -10)
        sc = ROLE_W.get(x["tier"], 12)
        sc += max(0, min(24, (math.log10(max(x["value"], 1)) - 3) * 8))   # size, $10k->8 .. $1M->24
        sc += 18 if nb >= 3 else 10 if nb >= 2 else 0                     # insider cluster
        if x["opportunistic"]:
            sc += 12                                                       # bought the dip
        if x.get("above200"):
            sc += 8                                                        # trend-confirmed
        if cb:
            sc += 10                                                       # Congress also buying
        x["conviction"] = min(100, round(sc))
        # quality gate: keep real bets out of penny/nano noise
        x["quality"] = bool((x.get("price") or 0) >= 1 and x["value"] >= 25000)
        return x

    for w in whale_buys:
        enrich(w)
    for b in buys:
        enrich(b)

    quality = [w for w in whale_buys if w["quality"]]
    # one row per ticker on the conviction board (a cluster shows once, with its
    # n_insiders count doing the talking) -- keep the highest-conviction bet.
    top_conviction, seen = [], set()
    for w in sorted(quality, key=lambda w: w["conviction"], reverse=True):
        if w["ticker"] in seen:
            continue
        seen.add(w["ticker"])
        top_conviction.append(w)
        if len(top_conviction) >= 12:
            break
    perf = [w for w in quality if w.get("since_excess") is not None]
    best_performers = sorted(perf, key=lambda w: w["since_excess"], reverse=True)[:8]
    med_excess = round(statistics.median([w["since_excess"] for w in perf]), 1) if perf else None
    pct_beating = round(sum(1 for w in perf if w["since_excess"] > 0) / len(perf) * 100) if perf else None

    tier_counts = {}
    for x in buys:
        tier_counts[x["tier"]] = tier_counts.get(x["tier"], 0) + 1
    total_sell_value = sum(x["value"] for x in sells)

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "window_days": INSIDER_MAX_DAYS,
        "stats": {"total_buy_value": total_buy_value, "buy_count": len(buys),
                  "total_sell_value": total_sell_value, "sell_count": len(sells),
                  "exec_buy_count": len(exec_buys), "tiers": tier_counts,
                  "biggest": {"ticker": biggest["ticker"], "value": biggest["value"],
                              "insider": biggest["insider"]} if biggest else None},
        # realized signal: how the tracked quality buys have done vs the S&P
        "signal": {"n_quality": len(quality), "n_tracked": len(perf),
                   "median_excess": med_excess, "pct_beating": pct_beating},
        "top_conviction": top_conviction,   # highest-conviction bets (scored)
        "best_performers": best_performers,  # best return-since-bought
        "whale_buys": whale_buys[:24],       # biggest bets, one per insider-stock
        "recent_buys": buys[:60],            # newest first
        "recent_sells": sells[:24],
        "clusters": clusters,
        "signals": {s["ticker"]: s for s in signals},
    }


# ---------------------------------------------------------------------------
# Market-impact analysis -- how each bill would help or hurt its stocks.
# Generated at build time with Claude, cached across runs so repeated
# refreshes (for fresher disclosure data) don't re-pay for unchanged bills.
# ---------------------------------------------------------------------------

import hashlib

IMPACT_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]},
        "analysis": {"type": "string"},
        "winners": {"type": "array", "items": {"type": "string"}},
        "losers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "analysis", "winners", "losers"],
    "additionalProperties": False,
}


def _impact_fingerprint(bill):
    """Cache key: re-analyze only when the substance we feed the model changes."""
    basis = "|".join([
        bill.get("number", ""),
        bill.get("title", ""),
        bill.get("summary", "") or "",
        bill.get("status", ""),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def load_impact_cache():
    """Read impact analyses from the previously committed data.json so a run
    only calls Claude for new or changed bills."""
    cache = {}
    if not os.path.exists(OUTPUT_PATH):
        return cache
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError):
        return cache
    for b in prev.get("bills", []):
        if b.get("impact_analysis") and b.get("impact_fingerprint"):
            cache[b["impact_fingerprint"]] = {
                "impact_direction": b.get("impact_direction"),
                "impact_analysis": b.get("impact_analysis"),
                "impact_winners": b.get("impact_winners", []),
                "impact_losers": b.get("impact_losers", []),
            }
    return cache


def generate_bill_impact(client, bill):
    tickers = [f"{s['ticker']} ({s['name']})" for s in bill.get("beneficiary_stocks", [])]
    summary = bill.get("summary") or "(No official summary published yet.)"
    prompt = (
        "You are a policy-to-markets analyst. Given a piece of U.S. federal legislation and a list of "
        "publicly traded companies in its sector, assess how the bill -- IF ENACTED -- would most "
        "plausibly help or hurt those specific companies.\n\n"
        f"BILL: {bill.get('number')} — {bill.get('title')}\n"
        f"SECTOR: {bill.get('sector')}\n"
        f"STATUS: {bill.get('status')}\n"
        f"SUMMARY: {summary}\n\n"
        f"SECTOR COMPANIES: {', '.join(tickers) if tickers else '(none tracked)'}\n\n"
        "Write 2–4 plain-English sentences explaining the concrete mechanism of help or harm "
        "(funding, mandates, demand creation, compliance cost, competitive shifts) and name specific "
        "companies where the effect is clearest. Set `direction` to the net effect on the sector basket. "
        "Put tickers most likely to benefit in `winners` and any likely to be hurt in `losers` -- only use "
        "tickers from the provided list, and leave an array empty if none clearly apply. Do not give "
        "investment advice or price targets; describe policy exposure only."
    )
    resp = client.messages.create(
        model=IMPACT_MODEL,
        max_tokens=1500,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": IMPACT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text)


def attach_impact_analysis(bills):
    """Populate impact_* fields on each bill. Skips gracefully (leaving the
    fields empty) when no ANTHROPIC_API_KEY is configured, so the pipeline
    still produces a valid site without it."""
    for b in bills:
        b["impact_fingerprint"] = _impact_fingerprint(b)
        b.setdefault("impact_analysis", "")
        b.setdefault("impact_direction", "")
        b.setdefault("impact_winners", [])
        b.setdefault("impact_losers", [])

    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY not set -- skipping bill impact analysis", file=sys.stderr)
        return bills

    try:
        import anthropic
    except ImportError:
        print("  WARN: anthropic package not installed -- skipping impact analysis", file=sys.stderr)
        return bills

    cache = load_impact_cache()
    client = anthropic.Anthropic()
    generated = 0
    reused = 0
    print(f"Generating market-impact analysis for {len(bills)} bills (cached where unchanged)...", file=sys.stderr)
    for b in bills:
        fp = b["impact_fingerprint"]
        if fp in cache:
            b.update(cache[fp])
            reused += 1
            continue
        try:
            result = generate_bill_impact(client, b)
            b["impact_direction"] = result.get("direction", "")
            b["impact_analysis"] = result.get("analysis", "")
            b["impact_winners"] = result.get("winners", [])
            b["impact_losers"] = result.get("losers", [])
            generated += 1
        except Exception as e:  # never let one bad analysis kill the run
            print(f"  WARN: impact analysis failed for {b.get('number')}: {e}", file=sys.stderr)
        time.sleep(0.1)
    print(f"  impact analysis: {generated} generated, {reused} reused from cache", file=sys.stderr)
    return bills


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def flag_pre_filing_trades(bills, trades):
    """Mark a bill if a tracked trade in the SAME sector was disclosed in the
    45 days before the bill's introduction. This is a sector-level
    correlation signal, not a claim that a specific trade concerned a
    specific bill."""
    for bill in bills:
        bill["sector_trade_flag"] = False
        intro = bill.get("introduced_date")
        if not intro:
            continue
        try:
            intro_dt = datetime.strptime(intro, "%Y-%m-%d")
        except ValueError:
            continue
        for t in trades:
            if t["sector"] != bill["sector"]:
                continue
            try:
                tx_dt = datetime.strptime(t["transaction_date"], "%m/%d/%Y")
            except ValueError:
                continue
            delta_days = (intro_dt - tx_dt).days
            if 0 <= delta_days <= 45:
                bill["sector_trade_flag"] = True
                break
    return bills


# Appropriations / funding-bill detection and dollar-figure extraction.
APPROPRIATION_TERMS = re.compile(
    r"\b(appropriat|making appropriations|authoriz\w* to be appropriated|"
    r"funding|to fund|supplemental|reauthoriz|budget|grant program|"
    r"amounts made available|there (is|are) authorized)\b",
    re.IGNORECASE,
)
# Dollar figures like "$1,500,000,000", "$5 billion", "$250 million".
DOLLAR_FIGURE = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?\s?(?:billion|million|trillion|thousand)?",
    re.IGNORECASE,
)


def analyze_appropriation(bill):
    """Flag whether a bill is an appropriations/funding measure and pull any
    dollar figures visible in its title or latest-action text. This works
    off the fields we already fetch -- no extra API calls."""
    blob = " ".join(filter(None, [
        bill.get("title"),
        (bill.get("latest_action") or {}).get("text"),
        bill.get("summary"),
    ]))
    bill["is_appropriation"] = bool(APPROPRIATION_TERMS.search(blob) or APPROP_TITLE.search(bill.get("title") or ""))
    figures = []
    seen = set()
    for m in DOLLAR_FIGURE.finditer(blob):
        val = re.sub(r"\s+", " ", m.group(0)).strip()
        # ignore bare "$" or trivially short catches
        if len(re.sub(r"[^\d]", "", val)) == 0:
            continue
        if val.lower() not in seen:
            seen.add(val.lower())
            figures.append(val)
    bill["dollar_figures"] = figures[:6]
    return bill


def attach_beneficiary_stocks(bills, sectors):
    """For each bill, list the constituent stocks of its sector -- the names
    positioned to benefit if the bill advances. This is the sector mapping
    the user already approved, surfaced per-bill."""
    for bill in bills:
        sector = sectors.get(bill["sector"], {})
        constituents = sector.get("constituents", {})
        bill["beneficiary_stocks"] = [
            {"ticker": t, "name": info["name"]} for t, info in constituents.items()
        ]
    return bills


def attach_bill_trades(bills, trades):
    """Link each bill to disclosed congressional trades in the stocks that
    would benefit from it -- i.e. trades whose ticker is one of the bill's
    sector constituents. This is the bill -> beneficiary-stock -> disclosure
    nexus, built entirely from disclosed records."""
    trades_by_sector = {}
    for t in trades:
        trades_by_sector.setdefault(t["sector"], []).append(t)

    for bill in bills:
        related = trades_by_sector.get(bill["sector"], [])
        # newest disclosures first
        related = sorted(related, key=lambda t: _parse_mdy(t["filed_date"]), reverse=True)
        bill["related_trades"] = [
            {
                "member": t["member"],
                "chamber": t["chamber"],
                "ticker": t["ticker"],
                "company": t["company"],
                "type": t["type"],
                "amount_range": t["amount_range"],
                "est_amount": t.get("est_amount", 0),
                "transaction_date": t["transaction_date"],
                "filed_date": t["filed_date"],
                "report_url": t["report_url"],
                "return_pct": t.get("return_pct"),
                "gain_value": t.get("gain_value"),
                "entry_price": t.get("entry_price"),
                "last_price": t.get("last_price"),
            }
            for t in related[:12]
        ]
        bill["related_trade_count"] = len(related)
    return bills


def _parse_mdy(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except (ValueError, TypeError):
        return datetime.min


def _is_buy(trade_type):
    return trade_type.lower().startswith("purchase")


def estimate_amount(amount_range):
    """STOCK Act disclosures give a dollar RANGE, not an exact figure. Estimate
    a point value as the midpoint of the range (or the single value if only one
    is given) so trades can be dollar-weighted rather than merely counted --
    the way Quiver/Capitol-Trades size congressional activity."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$?([\d,]+)", amount_range or "") if n.replace(",", "").isdigit()]
    nums = [n for n in nums if n >= 1]
    if not nums:
        return 0
    if len(nums) == 1:
        return nums[0]
    return round((min(nums) + max(nums)) / 2)


def annotate_trade_values(trades):
    for t in trades:
        t["est_amount"] = estimate_amount(t["amount_range"])
    return trades


def _nearest_close(series, date_str, back=10):
    """Close on or immediately before date_str (searching back up to `back`
    days to skip weekends/holidays). None if nothing lands in the window."""
    if not series:
        return None
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(back + 1):
        c = series.get(d.strftime("%Y-%m-%d"))
        if c:
            return c
        d -= timedelta(days=1)
    return None


def _next_close(series, date_str, fwd=10):
    """Close on or immediately AFTER date_str -- the first price a follower who
    saw the filing that morning could actually have paid."""
    if not series:
        return None
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(fwd + 1):
        c = series.get(d.strftime("%Y-%m-%d"))
        if c:
            return c
        d += timedelta(days=1)
    return None


def annotate_trade_pnl(trades, prices):
    """Attach paper gain/loss to each trade, measured TWO ways.

    `return_pct` / `excess_pct` run from the TRANSACTION date. That is the
    member's own experience, but nobody else could trade on it: the STOCK Act
    filing arrives a median ~28 days later (mean ~67; a fifth land past 90 days).
    Measured from there it is a look-ahead number.

    `return_filed_pct` / `excess_filed_pct` run from the FILING date -- the first
    price a follower could actually have paid. That is the implementable figure,
    and the gap between the two is the part of the move that happens inside the
    disclosure blackout, where only the member can act.

    For a PURCHASE these are the position's unrealized P&L; for a SALE they show
    how the stock moved after the member exited."""
    last_close = {tk: s[max(s)] for tk, s in prices.items() if s}
    spy = prices.get("SPY")
    spy_last = last_close.get("SPY")
    priced = filed_priced = 0
    for t in trades:
        t["entry_price"] = t["last_price"] = t["return_pct"] = t["gain_value"] = t["excess_pct"] = None
        t["return_filed_pct"] = t["excess_filed_pct"] = t["lag_days"] = None
        tk = t.get("ticker")
        series = prices.get(tk) if tk else None
        if not series or not t.get("transaction_date"):
            continue
        try:
            txd = datetime.strptime(t["transaction_date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        entry = _nearest_close(series, txd)
        last = last_close.get(tk)
        if not entry or not last:
            continue
        ret = last / entry - 1
        t["entry_price"] = round(entry, 2)
        t["last_price"] = round(last, 2)
        # raw price move since the trade date (from the stock's perspective)
        t["return_pct"] = round(ret * 100, 1)
        # excess return vs the S&P 500 over the SAME holding period (alpha) --
        # the honest measure of whether the pick actually beat the market.
        if spy and spy_last:
            spy_entry = _nearest_close(spy, txd)
            if spy_entry:
                t["excess_pct"] = round((ret - (spy_last / spy_entry - 1)) * 100, 1)
        # dollar P&L only for PURCHASES -- an open position whose paper value we
        # can size. A sale closes the position, so we show only the price move
        # since (as context on the exit), never an implied realized profit.
        if _is_buy(t["type"]):
            t["gain_value"] = round(t.get("est_amount", 0) * ret)
        priced += 1

        # ---- the implementable leg: entry at the first close after the filing
        # NB _parse_mdy returns datetime.min (not None) on a bad date
        fdt = _parse_mdy(t.get("filed_date"))
        txdt = _parse_mdy(t.get("transaction_date"))
        if fdt == datetime.min or txdt == datetime.min:
            continue
        lag = (fdt - txdt).days
        # negative lag = a filing dated before the trade (data error); absurd
        # lags are stale amendments rather than a tradeable signal
        if lag < 0 or lag > DISCLOSURE_MAX_LAG:
            continue
        t["lag_days"] = lag
        fentry = _next_close(series, fdt.strftime("%Y-%m-%d"))
        if not fentry:
            continue
        fret = last / fentry - 1
        t["return_filed_pct"] = round(fret * 100, 1)
        if spy and spy_last:
            spy_fentry = _next_close(spy, fdt.strftime("%Y-%m-%d"))
            if spy_fentry:
                t["excess_filed_pct"] = round((fret - (spy_last / spy_fentry - 1)) * 100, 1)
                filed_priced += 1
    print(f"  trade P&L: priced {priced}/{len(trades)} trades "
          f"({filed_priced} also priced from the filing date)", file=sys.stderr)
    return trades


# ---------------------------------------------------------------------------
# Economic-sector classification -- give EVERY traded stock a home. The 12
# curated sectors are policy THEMES (niche, bill-linked); most blue-chip trades
# fall outside them and used to pile into "Other". We classify every traded
# ticker by its real GICS sector (via Yahoo) and route the leftovers into 11
# broad economic sectors so nothing but genuine non-equities stays unclassified.
# ---------------------------------------------------------------------------

# Yahoo's sector strings -> our broad economic-sector codes (see sectors.json).
GICS_TO_CODE = {
    "Technology": "TECH",
    "Financial Services": "FIN",
    "Industrials": "INDU",
    "Healthcare": "HLTH",
    "Consumer Cyclical": "CONSD",
    "Consumer Defensive": "CONSS",
    "Communication Services": "COMM",
    "Real Estate": "REAL",
    "Basic Materials": "MATR",
    "Energy": "ENRG",
    "Utilities": "UTIL",
}


def _yahoo_session():
    """A requests session primed with Yahoo's cookie + crumb, required now for
    the quoteSummary endpoint. Returns (session, crumb) or (None, None)."""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        s.get("https://fc.yahoo.com", timeout=REQUEST_TIMEOUT)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=REQUEST_TIMEOUT).text.strip()
        if not crumb or "<" in crumb:
            return None, None
        return s, crumb
    except requests.RequestException:
        return None, None


def fetch_ticker_sectors(tickers):
    """ticker -> GICS sector string, cached in sector_cache.json. Only symbols
    missing or older than the TTL are fetched, so daily runs stay cheap and the
    site degrades gracefully (unknown tickers simply stay in Other)."""
    cache = {}
    if os.path.exists(SECTOR_CACHE_PATH):
        try:
            cache = json.load(open(SECTOR_CACHE_PATH, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def fresh(entry):
        try:
            age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(entry.get("asof", "2000-01-01"), "%Y-%m-%d")).days
            return age < SECTOR_CACHE_TTL_DAYS
        except ValueError:
            return False

    todo = [t for t in dict.fromkeys(tickers) if t and not (t in cache and fresh(cache[t]))]
    if todo:
        session, crumb = _yahoo_session()
        if session:
            fetched = 0
            for i, tk in enumerate(todo):
                ysym = tk.replace(".", "-")  # Yahoo uses BRK-B, not BRK.B
                try:
                    r = session.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ysym}",
                                    params={"modules": "assetProfile", "crumb": crumb}, timeout=REQUEST_TIMEOUT)
                    if r.status_code == 200:
                        p = r.json()["quoteSummary"]["result"][0]["assetProfile"]
                        cache[tk] = {"gics": p.get("sector"), "industry": p.get("industry"), "asof": today}
                        fetched += 1
                    elif r.status_code == 404:
                        cache[tk] = {"gics": None, "industry": None, "asof": today}  # no such profile: don't retry
                    else:
                        # rate-limited / transient: leave uncached so it retries next run
                        if r.status_code == 429:
                            print(f"  sector classify: rate-limited at {i}/{len(todo)}, stopping (fills in next run)", file=sys.stderr)
                            break
                except (requests.RequestException, KeyError, ValueError, IndexError):
                    pass  # transient: leave uncached so it retries next run
                time.sleep(0.03)
                if (i + 1) % 60 == 0:
                    print(f"  ...classified {i + 1}/{len(todo)} tickers", file=sys.stderr)
            json.dump(cache, open(SECTOR_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
            print(f"  sector classify: {fetched} fetched, {len(cache) - fetched} cached", file=sys.stderr)
        else:
            print("  WARN: Yahoo crumb handshake failed -- economic-sector routing skipped", file=sys.stderr)
    return {tk: v.get("gics") for tk, v in cache.items()}


def reclassify_trades(trades, ticker_gics):
    """Route every 'Other' trade that has a ticker into its broad economic
    sector using the GICS lookup. Trades already matched to a policy theme keep
    that theme; genuine non-equities (no ticker) and unknown symbols stay Other."""
    moved = 0
    for t in trades:
        if t.get("sector") != "OTHER" or not t.get("ticker"):
            continue
        code = GICS_TO_CODE.get(ticker_gics.get(t["ticker"]))
        if code:
            t["sector"] = code
            moved += 1
    still = sum(1 for t in trades if t.get("sector") == "OTHER")
    print(f"  economic routing: moved {moved} trades into broad sectors; {still} remain Other (mostly bonds/funds/options)", file=sys.stderr)
    return trades


def mark_key_bills(bills, per_sector=3):
    """Flag the most important bills in each sector -- the "specific bills
    coming through" surface. Importance = a blend of appropriation status,
    how far the bill has advanced, and its momentum score."""
    def key_score(b):
        score = b["momentum"]
        if b.get("is_appropriation"):
            score += 20
        status = b.get("status", "")
        if status in ("Passed House", "Passed Senate"):
            score += 15
        elif status == "Signed":
            score += 40
        return score

    for b in bills:
        b["key_bill"] = False
    by_sector = {}
    for b in bills:
        by_sector.setdefault(b["sector"], []).append(b)
    for sector_bills in by_sector.values():
        for b in sorted(sector_bills, key=key_score, reverse=True)[:per_sector]:
            b["key_bill"] = True
    return bills


# ---------------------------------------------------------------------------
# Member roster -- party affiliation + official photos, matched to filers.
# ---------------------------------------------------------------------------

_US_STATES = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
    'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
    'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD', 'Massachusetts': 'MA',
    'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO', 'Montana': 'MT',
    'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM',
    'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT',
    'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY',
}
_PARTY = {'Democratic': 'D', 'Republican': 'R', 'Independent': 'I'}
_SUFFIX_RE = re.compile(r',?\s+(jr|sr|ii|iii|iv)\.?$', re.IGNORECASE)


def _norm_name(s):
    return re.sub(r'[^a-z]', '', (s or '').lower())


def fetch_member_roster():
    """Current 119th-Congress members with party, state, chamber, and photo."""
    print("Fetching member roster (party + photos) from Congress.gov...", file=sys.stderr)
    members = []
    offset = 0
    while True:
        try:
            data = congress_get("member", {"currentMember": "true", "limit": 250, "offset": offset})
        except requests.RequestException as e:
            print(f"  WARN: roster fetch failed at offset {offset}: {e}", file=sys.stderr)
            break
        page = data.get("members", [])
        if not page:
            break
        members.extend(page)
        offset += 250
        if len(page) < 250:
            break
    print(f"  roster: {len(members)} members", file=sys.stderr)
    return members


def build_member_index(roster):
    index = {"House": [], "Senate": []}
    for m in roster:
        name = m.get("name", "")
        last = name.split(",")[0] if "," in name else name
        first = name.split(",", 1)[1].strip() if "," in name else ""
        chamber_raw = (m.get("terms", {}).get("item", [{}])[-1].get("chamber", "") or "")
        chamber = "Senate" if "Senate" in chamber_raw else "House"
        index[chamber].append({
            "last": _norm_name(last),
            "first": _norm_name(first),
            "state": _US_STATES.get(m.get("state", ""), ""),
            "party": _PARTY.get(m.get("partyName", ""), "?"),
            "image_url": (m.get("depiction") or {}).get("imageUrl"),
            "bioguide": m.get("bioguideId"),
        })
    return index


def match_member(member_str, index):
    """Match a disclosure filer string (e.g. 'Sen. Gary C Peters',
    'Rep. Nancy Pelosi (CA11)') to a roster member. Handles multi-word last
    names and Jr./Sr. suffixes. Returns the roster dict or None."""
    chamber = "Senate" if member_str.startswith("Sen.") else "House"
    body = re.sub(r"^(Sen\.|Rep\.)\s*", "", member_str)
    state_m = re.search(r"\(([A-Z]{2})\d*\)\s*$", body)
    state = state_m.group(1) if state_m else None
    body = re.sub(r"\s*\([^)]*\)\s*$", "", body)
    body = _SUFFIX_RE.sub("", body).strip().rstrip(",")
    full = _norm_name(body)
    if not full:
        return None
    cands = [m for m in index.get(chamber, []) if len(m["last"]) >= 3 and full.endswith(m["last"])]
    if state:
        state_cands = [m for m in cands if not m["state"] or m["state"] == state]
        if state_cands:
            cands = state_cands
    cands.sort(key=lambda m: len(m["last"]), reverse=True)  # most specific last name wins
    if len(cands) > 1:
        first_cands = [m for m in cands if m["first"] and full.startswith(m["first"][:4])]
        if first_cands:
            cands = first_cands
    return cands[0] if cands else None


def annotate_trade_parties(trades, index):
    """Add party / image_url / bioguide to each trade by matching its filer."""
    cache = {}
    unmatched = set()
    for t in trades:
        member = t["member"]
        if member not in cache:
            cache[member] = match_member(member, index)
        info = cache[member]
        t["party"] = info["party"] if info else "?"
        t["image_url"] = info["image_url"] if info else None
        t["bioguide"] = info["bioguide"] if info else None
        if not info:
            unmatched.add(member)
    if unmatched:
        print(f"  WARN: {len(unmatched)} filers unmatched to roster: {sorted(unmatched)[:5]}", file=sys.stderr)
    print(f"  party matched: {len(cache) - len(unmatched)}/{len(cache)} distinct filers", file=sys.stderr)
    return trades


def _jackknife(by_ticker_dw):
    """Drop the member's single most helpful ticker and recompute.

    A dollar-weighted alpha built on thirty names is a record. The same number
    built on one position that happened to 10x is a story about that position.
    This reports the alpha with the best contributor removed, so a reader can see
    which it is at a glance -- the standard leave-one-out robustness check, with
    the unit being the TICKER rather than the trade."""
    if len(by_ticker_dw) < 3:
        return {"alpha_ex_best": None, "jk_dropped": None, "one_name": False}

    def dw(dct):
        num = sum(v * e / 100.0 for rows in dct.values() for v, e in rows)
        den = sum(v for rows in dct.values() for v, _ in rows)
        return (num / den * 100) if den else None

    full = dw(by_ticker_dw)
    if full is None:
        return {"alpha_ex_best": None, "jk_dropped": None, "one_name": False}
    worst, dropped = None, None
    for tk in by_ticker_dw:
        alt = dw({k: v for k, v in by_ticker_dw.items() if k != tk})
        if alt is None:
            continue
        if worst is None or alt < worst:
            worst, dropped = alt, tk
    if worst is None:
        return {"alpha_ex_best": None, "jk_dropped": None, "one_name": False}
    return {
        "alpha_ex_best": round(worst, 1),
        "jk_dropped": dropped,
        # Fragile = a positive record that does not survive losing one name,
        # OR one where two thirds of the alpha walks out with that single name.
        # The second clause matters: a record that falls 30pp to +1% is still
        # "positive" but is plainly a story about one position.
        "one_name": bool(full > 0 and (worst <= 0 or worst < full / 3.0)),
    }


def _cluster_tstat(by_ticker):
    """A one-sample t-stat on a member's implementable excess returns, with one
    observation per DISTINCT TICKER rather than per trade.

    Buying NVDA ten times on the way up is one idea expressed ten times, not ten
    independent bets; pooling raw trades inflates t by roughly sqrt(trades per
    idea). Averaging within a ticker first is the cheap, defensible fix. Even
    then the residuals are not truly independent -- names share sectors and
    overlapping holding periods, all measured to one common end date -- so read
    |t| > 2 as 'worth a look', not as a p-value. With ~40 scoreable members you
    should expect about two to clear that bar on luck alone."""
    vals = [sum(v) / len(v) for v in by_ticker.values() if v]
    n = len(vals)
    if n < 4:
        return None
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    if var <= 0:
        return None
    return round(mean / ((var / n) ** 0.5), 2)


def build_member_profiles(trades):
    """Autopilot-style 'follow a politician': aggregate every disclosed trade
    by the member who filed it, so each politician becomes a trackable
    portfolio. Per-member trade lists are not duplicated here -- the site
    filters the full trades feed by member name for the detail view."""
    profiles = {}
    for t in trades:
        member = t["member"]
        p = profiles.setdefault(member, {
            "member": member,
            "chamber": t["chamber"],
            "party": t.get("party", "?"),
            "image_url": t.get("image_url"),
            "bioguide": t.get("bioguide"),
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "buy_value": 0,
            "sell_value": 0,
            "tickers": {},
            "sectors": set(),
            "last_filed": None,
            "buy_basis": 0, "buy_gain": 0, "priced": 0, "wins": 0,
            "buy_excess": 0, "excess_basis": 0, "beat_market": 0,
            "best": None, "worst": None,
            # implementable leg: everything measured from the FILING date
            "filed_excess": 0, "filed_basis": 0, "filed_priced": 0, "filed_wins": 0,
            "lags": [], "by_ticker": {}, "by_ticker_dw": {},
            "sell_excess": [],
        })
        p["trade_count"] += 1
        val = t.get("est_amount", 0)
        if _is_buy(t["type"]):
            p["buy_count"] += 1
            p["buy_value"] += val
            # Track record: dollar-weighted paper return of the member's BUYS
            # (a sale closes a position, so P&L is only meaningful on purchases).
            if t.get("return_pct") is not None and t.get("gain_value") is not None:
                p["buy_basis"] += val
                p["buy_gain"] += t["gain_value"]
                p["priced"] += 1
                if t["return_pct"] > 0:
                    p["wins"] += 1
                if p["best"] is None or t["return_pct"] > p["best"]["return_pct"]:
                    p["best"] = {"ticker": t["ticker"], "return_pct": t["return_pct"]}
                if p["worst"] is None or t["return_pct"] < p["worst"]["return_pct"]:
                    p["worst"] = {"ticker": t["ticker"], "return_pct": t["return_pct"]}
                if t.get("excess_pct") is not None:
                    p["buy_excess"] += val * t["excess_pct"] / 100.0
                    p["excess_basis"] += val
                    if t["excess_pct"] > 0:
                        p["beat_market"] += 1
            # the implementable leg is tracked independently: a trade can be
            # priced from the filing date even when the trade-date leg is not
            if t.get("excess_filed_pct") is not None:
                p["filed_excess"] += val * t["excess_filed_pct"] / 100.0
                p["filed_basis"] += val
                p["filed_priced"] += 1
                if t["excess_filed_pct"] > 0:
                    p["filed_wins"] += 1
                # cluster by ticker for the t-test: ten buys of one stock are
                # one bet repeated, not ten independent observations
                p["by_ticker"].setdefault(t["ticker"], []).append(t["excess_filed_pct"])
                # dollar terms too, so the jackknife can rebuild the weighted alpha
                p["by_ticker_dw"].setdefault(t["ticker"], []).append((val, t["excess_filed_pct"]))
            if t.get("lag_days") is not None:
                p["lags"].append(t["lag_days"])
        else:
            p["sell_count"] += 1
            if t.get("excess_filed_pct") is not None:
                p["sell_excess"].append(t["excess_filed_pct"])
            p["sell_value"] += val
        if t["ticker"]:
            p["tickers"][t["ticker"]] = p["tickers"].get(t["ticker"], 0) + 1
        if t["sector"] != "OTHER":
            p["sectors"].add(t["sector"])
        filed = _parse_mdy(t["filed_date"])
        if p["last_filed"] is None or filed > _parse_mdy(p["last_filed"]):
            p["last_filed"] = t["filed_date"]

    out = []
    for p in profiles.values():
        top_tickers = sorted(p["tickers"].items(), key=lambda kv: kv[1], reverse=True)[:6]
        out.append({
            "member": p["member"],
            "chamber": p["chamber"],
            "party": p["party"],
            "image_url": p["image_url"],
            "bioguide": p["bioguide"],
            "trade_count": p["trade_count"],
            "buy_count": p["buy_count"],
            "sell_count": p["sell_count"],
            "buy_value": p["buy_value"],
            "sell_value": p["sell_value"],
            "total_value": p["buy_value"] + p["sell_value"],
            "distinct_tickers": len(p["tickers"]),
            "top_tickers": [{"ticker": t, "count": c} for t, c in top_tickers],
            "sectors": sorted(p["sectors"]),
            "last_filed": p["last_filed"],
            # Track record (dollar-weighted paper return of disclosed buys)
            "portfolio_return": round(p["buy_gain"] / p["buy_basis"] * 100, 1) if p["buy_basis"] else None,
            # Alpha: dollar-weighted EXCESS return vs the S&P over the same holding
            # periods -- the honest measure of stock-picking skill.
            "alpha": round(p["buy_excess"] / p["excess_basis"] * 100, 1) if p["excess_basis"] else None,
            "win_rate": round(p["wins"] / p["priced"] * 100) if p["priced"] else None,
            "beat_market_rate": round(p["beat_market"] / p["priced"] * 100) if p["priced"] else None,
            "priced_buys": p["priced"],
            "best_trade": p["best"],
            "worst_trade": p["worst"],
            # --- implementable: measured from the day the filing went public ---
            "alpha_filed": round(p["filed_excess"] / p["filed_basis"] * 100, 1) if p["filed_basis"] else None,
            "filed_buys": p["filed_priced"],
            "filed_win_rate": round(p["filed_wins"] / p["filed_priced"] * 100) if p["filed_priced"] else None,
            "median_lag": (sorted(p["lags"])[len(p["lags"]) // 2] if p["lags"] else None),
            # skill vs noise, clustered by ticker (see _cluster_tstat)
            "tstat": _cluster_tstat(p["by_ticker"]),
            "n_tickers_scored": len(p["by_ticker"]),
            # how much of the record is one lucky name (see _jackknife)
            **_jackknife(p["by_ticker_dw"]),
            # does this member's buying beat their own selling?
            "sell_alpha": (round(sum(p["sell_excess"]) / len(p["sell_excess"]), 1)
                           if len(p["sell_excess"]) >= 5 else None),
        })

    # Confidence-adjust the alpha so a lucky one- or two-trade streak can't top
    # the leaderboard. Empirical-Bayes style shrinkage: treat each member as if
    # they had also made ALPHA_SHRINK_K "average" trades at the group's typical
    # alpha (a robust median prior). A long, consistent record barely moves; a
    # thin sample gets pulled hard toward the mean. This is the honest way to
    # RANK skill under very different sample sizes.
    ALPHA_SHRINK_K = 5
    prior_sample = [p["alpha"] for p in out
                    if p["alpha"] is not None and (p["priced_buys"] or 0) >= 3]
    alpha_prior = round(statistics.median(prior_sample), 1) if prior_sample else 0.0
    for p in out:
        n = p["priced_buys"] or 0
        p["alpha_adj"] = (round((n * p["alpha"] + ALPHA_SHRINK_K * alpha_prior) / (n + ALPHA_SHRINK_K), 1)
                          if p["alpha"] is not None and n > 0 else None)
        p["alpha_prior"] = alpha_prior
        p["alpha_k"] = ALPHA_SHRINK_K

    # the implementable leg gets the same treatment, with its own prior -- the
    # two distributions are not the same, so they must not share a mean
    filed_sample = [p["alpha_filed"] for p in out
                    if p["alpha_filed"] is not None and (p["filed_buys"] or 0) >= 3]
    filed_prior = round(statistics.median(filed_sample), 1) if filed_sample else 0.0
    for p in out:
        n = p["filed_buys"] or 0
        p["alpha_filed_adj"] = (round((n * p["alpha_filed"] + ALPHA_SHRINK_K * filed_prior) / (n + ALPHA_SHRINK_K), 1)
                                if p["alpha_filed"] is not None and n > 0 else None)
        p["alpha_filed_prior"] = filed_prior

    out.sort(key=lambda p: p["total_value"], reverse=True)
    return out


def build_stock_signals(trades):
    """Quiver-style per-stock consensus: for each ticker, how many distinct
    members traded it and the net buy/sell direction across Congress."""
    signals = {}
    for t in trades:
        if not t["ticker"]:
            continue  # skip non-ticker assets (bonds, funds)
        s = signals.setdefault(t["ticker"], {
            "ticker": t["ticker"],
            "company": t["company"],
            "sector": t["sector"],
            "buy_count": 0,
            "sell_count": 0,
            "buy_value": 0,
            "sell_value": 0,
            "members": set(),
            "parties": set(),
            "last_filed": None,
        })
        val = t.get("est_amount", 0)
        if _is_buy(t["type"]):
            s["buy_count"] += 1
            s["buy_value"] += val
        else:
            s["sell_count"] += 1
            s["sell_value"] += val
        s["members"].add(t["member"])
        if t.get("party") in ("D", "R", "I"):
            s["parties"].add(t["party"])
        filed = _parse_mdy(t["filed_date"])
        if s["last_filed"] is None or filed > _parse_mdy(s["last_filed"]):
            s["last_filed"] = t["filed_date"]

    out = []
    for s in signals.values():
        out.append({
            "ticker": s["ticker"],
            "company": _clean_company_name(s["company"]),
            "sector": s["sector"],
            "buy_count": s["buy_count"],
            "sell_count": s["sell_count"],
            "buy_value": s["buy_value"],
            "sell_value": s["sell_value"],
            "net": s["buy_count"] - s["sell_count"],
            "net_value": s["buy_value"] - s["sell_value"],
            "total_value": s["buy_value"] + s["sell_value"],
            "member_count": len(s["members"]),
            "total_trades": s["buy_count"] + s["sell_count"],
            "parties": sorted(s["parties"]),
            "bipartisan": len(s["parties"]) >= 2,
            "last_filed": s["last_filed"],
        })
    # rank by dollar volume, then breadth of members
    out.sort(key=lambda s: (s["total_value"], s["member_count"]), reverse=True)
    return out


def _smart_money_score(s):
    """Per-stock 'Smart Money Score' (0-100). Unlike raw trade volume, it weights
    what actually matters: how many members are buying (breadth), the NET
    direction, the demonstrated SKILL of the buyers (their alpha), how the buys
    have DONE since disclosure, plus corporate-insider confirmation, the analyst
    edge and the price trend. A stock a few skilled members are quietly
    accumulating -- that has since outperformed -- outranks one dozens dumped."""
    e = 0.0
    e += min(s["member_count"], 15) / 15 * 20                         # breadth of buyers
    tot = s["buy_count"] + s["sell_count"]
    if tot:
        e += max(0.0, (s["buy_count"] - s["sell_count"]) / tot) * 12  # net-buy tilt
    if s.get("buyer_alpha") is not None:
        e += max(0.0, min(20.0, s["buyer_alpha"])) / 20 * 16          # skill of the buyers
    if s.get("buy_excess") is not None:
        e += max(0.0, min(40.0, s["buy_excess"])) / 40 * 16           # realized track record
    ib = s.get("insider_buyers", 0)
    e += 10 if ib >= 2 else 6 if ib >= 1 else 0                       # corporate-insider confirm
    if s.get("edge") is not None:
        e += s["edge"] / 100 * 12                                     # analyst/composite edge
    if s.get("above200") and (s.get("r6") or 0) > 0:
        e += 8                                                        # price trend confirms
    return round(max(0.0, min(100.0, e)))


def enrich_stock_signals(stock_signals, trades, members, insiders, street, screener):
    """Turn the raw per-stock Congress tallies into a research surface: weight the
    buyers by their skill (alpha), measure how the buys have done since disclosure,
    and join corporate-insider / analyst / technical signals -- then score it all."""
    alpha = {m["member"]: m.get("alpha_adj") for m in members if m.get("alpha_adj") is not None}
    isig = (insiders or {}).get("signals") or {}
    strt = {s["ticker"]: s for s in (street or {}).get("stocks") or []}
    tech = {s["ticker"]: s for s in (screener or {}).get("stocks") or []}
    buys_by_tk = {}
    for t in trades:
        tk = t.get("ticker")
        if tk and _is_buy(t["type"]):
            buys_by_tk.setdefault(tk, []).append(t)
    for s in stock_signals:
        tk = s["ticker"]
        buys = buys_by_tk.get(tk, [])
        skilled = [alpha[t["member"]] for t in {b["member"]: b for b in buys}.values() if t["member"] in alpha]
        s["buyer_alpha"] = round(statistics.mean(skilled), 1) if skilled else None
        s["scored_buyers"] = len(skilled)
        exc = [t["excess_pct"] for t in buys if t.get("excess_pct") is not None]
        s["buy_excess"] = round(statistics.mean(exc), 1) if exc else None
        s["priced_buys"] = len(exc)
        st, te, ii = strt.get(tk), tech.get(tk), isig.get(tk)
        s["insider_buyers"] = (ii or {}).get("n_buyers", 0)
        s["edge"] = st.get("edge") if st else None
        s["upside"] = st.get("upside") if st else None
        s["rec"] = st.get("rec") if st else None
        s["r6"] = (te or {}).get("r6")
        s["above200"] = (te or {}).get("above200")
        s["mcap"] = (te or {}).get("mcap")
        s["sm_score"] = _smart_money_score(s)
    return stock_signals


def build_unusual_activity(stock_signals):
    """Surface the 'signal' in the noise -- Quiver-style unusual activity.
    All computed from disclosed records: consensus accumulation, consensus
    distribution, and cross-party (bipartisan) interest in the same name."""
    tradable = [s for s in stock_signals if s["sector"] != "OTHER" or s["member_count"] >= 2]
    consensus_buys = sorted(
        [s for s in stock_signals if s["member_count"] >= 2 and s["net_value"] > 0],
        key=lambda s: (s["member_count"], s["net_value"]), reverse=True)[:8]
    consensus_sells = sorted(
        [s for s in stock_signals if s["member_count"] >= 2 and s["net_value"] < 0],
        key=lambda s: (s["member_count"], -s["net_value"]), reverse=True)[:8]
    bipartisan = sorted(
        [s for s in stock_signals if s.get("bipartisan")],
        key=lambda s: (s["member_count"], s["total_value"]), reverse=True)[:8]

    def slim(s):
        return {k: s[k] for k in ("ticker", "company", "sector", "member_count", "buy_count",
                                   "sell_count", "net_value", "total_value", "parties")}
    return {
        "consensus_buys": [slim(s) for s in consensus_buys],
        "consensus_sells": [slim(s) for s in consensus_sells],
        "bipartisan": [slim(s) for s in bipartisan],
    }


def build_smart_money(trades, cluster_days=75, min_buyers=3):
    """The 'smart money' surface -- the signals that actually mean something:
      * cluster buys: several DIFFERENT members buying the same stock inside a
        short window (conviction that isn't one person's idiosyncratic bet),
      * big single bets placed recently,
    each carrying the buyers, party mix, dollar size, and how the stock has done
    since. All from disclosed records."""
    now = datetime.now()
    cutoff = now - timedelta(days=cluster_days)
    by_tk = {}
    for t in trades:
        if not t.get("ticker") or not _is_buy(t["type"]):
            continue
        if _parse_mdy(t["transaction_date"]) < cutoff:
            continue
        e = by_tk.setdefault(t["ticker"], {"ticker": t["ticker"], "company": t["company"],
                                           "sector": t["sector"], "buyers": {}, "value": 0, "rets": []})
        b = e["buyers"].setdefault(t["member"], {"member": t["member"], "party": t.get("party", "?"), "value": 0})
        b["value"] += t.get("est_amount", 0)
        e["value"] += t.get("est_amount", 0)
        if t.get("return_pct") is not None:
            e["rets"].append(t["return_pct"])

    clusters = []
    for e in by_tk.values():
        if len(e["buyers"]) < min_buyers:
            continue
        parties = {b["party"] for b in e["buyers"].values() if b["party"] in ("D", "R", "I")}
        clusters.append({
            "ticker": e["ticker"], "company": e["company"], "sector": e["sector"],
            "n_buyers": len(e["buyers"]),
            "total_value": e["value"],
            "parties": sorted(parties),
            "bipartisan": len(parties & {"D", "R"}) == 2,
            "avg_return": round(sum(e["rets"]) / len(e["rets"]), 1) if e["rets"] else None,
            "buyers": sorted(([dict(b) for b in e["buyers"].values()]), key=lambda b: b["value"], reverse=True)[:10],
        })
    clusters.sort(key=lambda c: (c["n_buyers"], c["total_value"]), reverse=True)

    recent_cut = now - timedelta(days=45)
    recent = [t for t in trades if t.get("ticker") and _parse_mdy(t["transaction_date"]) >= recent_cut]
    recent.sort(key=lambda t: t.get("est_amount", 0), reverse=True)
    big_bets = [{"ticker": t["ticker"], "company": t["company"], "member": t["member"],
                 "party": t.get("party", "?"), "type": t["type"], "value": t.get("est_amount", 0),
                 "transaction_date": t["transaction_date"], "return_pct": t.get("return_pct"),
                 "sector": t["sector"]} for t in recent[:14]]

    return {"cluster_window_days": cluster_days, "clusters": clusters[:24], "big_bets": big_bets}


def _sector_trade_stats(sector_trades):
    """Dollar flows, disclosed-buy paper return, and top movers for a sector."""
    buy_value = sum(t["est_amount"] for t in sector_trades if _is_buy(t["type"]))
    sell_value = sum(t["est_amount"] for t in sector_trades if not _is_buy(t["type"]))
    # dollar-weighted paper return of the disclosed BUYS in this sector
    priced_buys = [t for t in sector_trades if _is_buy(t["type"]) and t.get("return_pct") is not None]
    buy_basis = sum(t["est_amount"] for t in priced_buys)
    buy_gain = sum(t["gain_value"] for t in priced_buys)
    trade_return = round(buy_gain / buy_basis * 100, 1) if buy_basis else None
    # top tickers by dollar volume, with net buy/sell direction
    vol, net = {}, {}
    for t in sector_trades:
        tk = t.get("ticker")
        if not tk:
            continue
        vol[tk] = vol.get(tk, 0) + t["est_amount"]
        net[tk] = net.get(tk, 0) + (t["est_amount"] if _is_buy(t["type"]) else -t["est_amount"])
    top_stocks = [{"ticker": tk, "value": v, "net": net[tk]}
                  for tk, v in sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:4]]
    members = len({t.get("member") for t in sector_trades if t.get("member")})
    return {
        "buy_value": buy_value,
        "sell_value": sell_value,
        "net_value": buy_value - sell_value,
        "trade_return": trade_return,
        "top_stocks": top_stocks,
        "member_count": members,
    }


def build_sector_summaries(sectors, bills, trades=()):
    by_sector = {}
    for t in trades:
        by_sector.setdefault(t["sector"], []).append(t)

    summaries = {}
    for code, sector in sectors.items():
        sector_bills = [b for b in bills if b["sector"] == code]
        sector_trades = by_sector.get(code, [])
        avg_momentum = round(sum(b["momentum"] for b in sector_bills) / len(sector_bills)) if sector_bills else 0
        summaries[code] = {
            "name": sector["name"],
            "short": sector.get("short", sector["name"]),
            "etf": sector["etf"],
            "color": sector["color"],
            "group": sector.get("group", "theme"),
            "bill_count": len(sector_bills),
            "appropriation_count": sum(1 for b in sector_bills if b.get("is_appropriation")),
            "trade_count": len(sector_trades),
            "stock_count": len(sector.get("constituents", {})),
            "avg_momentum": avg_momentum,
            **_sector_trade_stats(sector_trades),
        }
    other_trades = by_sector.get("OTHER", [])
    if other_trades:
        summaries["OTHER"] = {
            "name": "Bonds, Funds & Options",
            "short": "Bonds & Funds",
            "etf": None,
            "color": "#565F73",
            "group": "economy",
            "bill_count": 0,
            "appropriation_count": 0,
            "trade_count": len(other_trades),
            "stock_count": 0,
            "avg_momentum": 0,
            **_sector_trade_stats(other_trades),
        }
    return summaries


# ---------------------------------------------------------------------------
# Performance backtest -- a "Congress vs Market" index built from real prices.
# Positions come from disclosed STOCK Act trades (which lag execution by weeks),
# so this is an ILLUSTRATIVE backtest of *following the disclosures*, not a
# claim of members' actual returns. Prices via the free Yahoo chart API.
# ---------------------------------------------------------------------------

YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _yf_prices(symbol, range_="2y"):
    """Daily closes {date_str: close} from Yahoo, or {} on failure."""
    try:
        resp = requests.get(YF_BASE + symbol, params={"range": range_, "interval": "1d"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        out = {}
        for t, c in zip(ts, closes):
            if c is not None:
                out[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = round(c, 4)
        return out
    except (requests.RequestException, KeyError, ValueError, IndexError):
        return {}


def load_price_cache():
    if not os.path.exists(PRICES_PATH):
        return {}
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def fetch_prices(tickers):
    """Fetch daily closes for the given tickers + SPY, caching in prices.json so
    the 6-hourly runs only hit the network once per day per symbol."""
    cache = load_price_cache()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    symbols = list(dict.fromkeys(list(tickers) + ["SPY"]))
    fetched = 0
    for i, tk in enumerate(symbols):
        entry = cache.get(tk)
        if entry and entry.get("asof") == today:
            continue  # already fresh today
        series = _yf_prices(tk)
        if series:
            items = sorted(series.items())[-PRICE_HISTORY_DAYS:]
            cache[tk] = {"asof": today, "d": [d for d, _ in items], "c": [c for _, c in items]}
            fetched += 1
        time.sleep(0.05)
        if (i + 1) % 40 == 0:
            print(f"  ...priced {i + 1}/{len(symbols)} symbols", file=sys.stderr)
    with open(PRICES_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    print(f"  prices: {fetched} fetched, {len(symbols) - fetched} from cache", file=sys.stderr)
    return {tk: dict(zip(v["d"], v["c"])) for tk, v in cache.items() if v.get("c")}


SCREENER_PRICES_PATH = os.path.join(SCRIPT_DIR, "..", "screener_prices.json")
SCREENER_HISTORY_DAYS = 300  # ~14 months of closes -- enough for 200-DMA + 52-week


def fetch_screener_prices(tickers):
    """Daily closes for the whole screener universe (~5-6k names). Kept in its
    own cache (persisted via GitHub Actions cache, not git -- too large to
    commit). Skips symbols already fresh today, so the big fetch runs once/day."""
    cache = {}
    if os.path.exists(SCREENER_PRICES_PATH):
        try:
            cache = json.load(open(SCREENER_PRICES_PATH, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todo = [t for t in dict.fromkeys(tickers) if not (t in cache and cache[t].get("asof") == today)]
    print(f"  screener prices: {len(todo)} of {len(tickers)} to fetch...", file=sys.stderr)
    fetched = 0
    for i, tk in enumerate(todo):
        series = _yf_prices(tk)
        if series:
            items = sorted(series.items())[-SCREENER_HISTORY_DAYS:]
            cache[tk] = {"asof": today, "d": [d for d, _ in items], "c": [c for _, c in items]}
            fetched += 1
        else:
            cache[tk] = {"asof": today, "d": [], "c": []}  # attempted; retry tomorrow
        time.sleep(0.04)
        if (i + 1) % 400 == 0:
            print(f"  ...screener priced {i + 1}/{len(todo)}", file=sys.stderr)
            json.dump(cache, open(SCREENER_PRICES_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    json.dump(cache, open(SCREENER_PRICES_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"  screener prices: {fetched} fetched, {len(cache)} cached total", file=sys.stderr)
    return {tk: dict(zip(v["d"], v["c"])) for tk, v in cache.items() if v.get("c")}


MCAP_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "mcap_cache.json")
MCAP_TTL_DAYS = 7          # market cap changes slowly; a weekly refresh is plenty
MID_CAP_FLOOR = 2_000_000_000  # $2B -- screener universe is mid-cap and above only


def fetch_market_caps(tickers):
    """Market cap per ticker via Yahoo's batch quote endpoint (~200/request),
    cached weekly. Used to keep the screener at mid-cap and above."""
    cache = {}
    if os.path.exists(MCAP_CACHE_PATH):
        try:
            cache = json.load(open(MCAP_CACHE_PATH, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def fresh(e):
        try:
            return (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(e.get("asof", "2000-01-01"), "%Y-%m-%d")).days < MCAP_TTL_DAYS
        except ValueError:
            return False
    todo = [t for t in dict.fromkeys(tickers) if not (t in cache and fresh(cache[t]))]
    if todo:
        session, crumb = _yahoo_session()
        if session:
            fetched = 0
            for i in range(0, len(todo), 200):
                batch = todo[i:i + 200]
                try:
                    r = session.get("https://query1.finance.yahoo.com/v7/finance/quote",
                                    params={"symbols": ",".join(batch), "crumb": crumb}, timeout=REQUEST_TIMEOUT)
                    got = set()
                    if r.status_code == 200:
                        for q in r.json().get("quoteResponse", {}).get("result", []):
                            sym = q.get("symbol")
                            if sym:
                                cache[sym] = {"mc": q.get("marketCap"), "asof": today}
                                got.add(sym)
                    for s in batch:
                        if s not in got:
                            cache[s] = {"mc": None, "asof": today}
                    fetched += len(batch)
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(0.3)
            json.dump(cache, open(MCAP_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
            print(f"  market caps: {fetched} refreshed, {len(cache)} cached", file=sys.stderr)
        else:
            print("  WARN: Yahoo crumb failed -- market-cap floor skipped", file=sys.stderr)
    return {t: v.get("mc") for t, v in cache.items()}


# ---------------------------------------------------------------------------
# Wall Street vs the Crowd -- analyst ratings (Yahoo) + retail social sentiment
# (StockTwits) for the stocks this site cares about. Both cached; both degrade
# gracefully if a source rate-limits.
# ---------------------------------------------------------------------------

RATINGS_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "ratings_cache.json")
SOCIAL_CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "social_cache.json")
RATINGS_TTL_DAYS = 3
SOCIAL_TTL_DAYS = 1
STREET_MAX_TICKERS = 240


def _load_cache(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _fresh(entry, today, ttl):
    try:
        return (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(entry.get("asof", "2000-01-01"), "%Y-%m-%d")).days < ttl
    except ValueError:
        return False


def fetch_ratings(tickers):
    """Analyst consensus per ticker via Yahoo quoteSummary: recommendation, mean
    price target + upside, analyst count, and the strong-buy..strong-sell split."""
    cache = _load_cache(RATINGS_CACHE_PATH)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todo = [t for t in dict.fromkeys(tickers) if not (t in cache and _fresh(cache[t], today, RATINGS_TTL_DAYS))]
    if todo:
        session, crumb = _yahoo_session()
        if session:
            got = 0
            for i, tk in enumerate(todo):
                try:
                    r = session.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{tk.replace('.', '-')}",
                                    params={"modules": "financialData,recommendationTrend", "crumb": crumb}, timeout=REQUEST_TIMEOUT)
                    if r.status_code == 200:
                        res = r.json()["quoteSummary"]["result"][0]
                        f = res.get("financialData") or {}
                        rt = (res.get("recommendationTrend") or {}).get("trend") or []
                        t0 = rt[0] if rt else {}
                        cur = (f.get("currentPrice") or {}).get("raw")
                        tgt = (f.get("targetMeanPrice") or {}).get("raw")
                        cache[tk] = {"asof": today, "rec": f.get("recommendationKey"),
                                     "target": round(tgt, 2) if tgt else None, "current": round(cur, 2) if cur else None,
                                     "upside": round((tgt / cur - 1) * 100, 1) if tgt and cur else None,
                                     "n": (f.get("numberOfAnalystOpinions") or {}).get("raw"),
                                     "sb": t0.get("strongBuy", 0), "b": t0.get("buy", 0), "h": t0.get("hold", 0),
                                     "s": t0.get("sell", 0), "ss": t0.get("strongSell", 0)}
                        got += 1
                    else:
                        cache[tk] = {"asof": today, "rec": None}
                except (requests.RequestException, KeyError, ValueError, IndexError):
                    cache[tk] = {"asof": today, "rec": None}
                time.sleep(0.2)
                if (i + 1) % 80 == 0:
                    json.dump(cache, open(RATINGS_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
            json.dump(cache, open(RATINGS_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
            print(f"  analyst ratings: {got} fetched, {len(cache)} cached", file=sys.stderr)
    return cache


SOCIAL_MIN_FOLLOWERS = 25   # a "quality" voice, not a throwaway/pump account
SOCIAL_MIN_IDEAS = 15       # has a real posting history


def fetch_social(tickers):
    """Retail social sentiment per ticker from StockTwits. Buzz = raw message
    volume, but bull/bear sentiment is counted ONLY from established users
    (min followers + posting history) so pump/spam accounts don't skew it."""
    cache = _load_cache(SOCIAL_CACHE_PATH)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todo = [t for t in dict.fromkeys(tickers) if not (t in cache and _fresh(cache[t], today, SOCIAL_TTL_DAYS))]
    got = limited = 0
    for tk in todo:
        try:
            r = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{tk}.json",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                limited = 1
                break
            if r.status_code == 200:
                msgs = r.json().get("messages", [])
                bull = bear = 0
                for m in msgs:
                    s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                    if s not in ("Bullish", "Bearish"):
                        continue
                    u = m.get("user") or {}
                    if (u.get("followers") or 0) < SOCIAL_MIN_FOLLOWERS or (u.get("ideas") or 0) < SOCIAL_MIN_IDEAS:
                        continue  # low-quality / brand-new account -- ignore its vote
                    if s == "Bullish":
                        bull += 1
                    else:
                        bear += 1
                cache[tk] = {"asof": today, "msgs": len(msgs), "bull": bull, "bear": bear, "q": bull + bear}
                got += 1
            else:
                cache[tk] = {"asof": today, "msgs": 0}
        except (requests.RequestException, ValueError):
            cache[tk] = {"asof": today, "msgs": 0}
        time.sleep(0.35)
    json.dump(cache, open(SOCIAL_CACHE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"  social sentiment: {got} fetched{' (rate-limited, will continue next run)' if limited else ''}, {len(cache)} cached", file=sys.stderr)
    return cache


def build_street(tickers, ratings, social, stock_signals, insiders, screener):
    """Combine analyst ratings + retail buzz with this site's own smart-money
    signals into one 'Wall Street vs the Crowd' picture per stock."""
    csig = {s["ticker"]: s for s in stock_signals}
    isig = (insiders or {}).get("signals") or {}
    tech = {s["ticker"]: s for s in (screener.get("stocks") or [])}
    rows = []
    for tk in dict.fromkeys(tickers):
        r = ratings.get(tk) or {}
        soc = social.get(tk) or {}
        n = r.get("n") or 0
        # a rating is only trustworthy with real coverage (>=3 analysts)
        has_rating = bool(r.get("rec")) and n >= 3
        has_social = bool(soc.get("msgs"))
        if not has_rating and not has_social:
            continue
        # clamp obviously-stale price targets (a >250% or <-90% "upside" is a glitch)
        upside = r.get("upside")
        if upside is not None and (upside > 250 or upside < -90):
            upside = None
        buy = r.get("sb", 0) + r.get("b", 0)
        hold = r.get("h", 0)
        sell = r.get("s", 0) + r.get("ss", 0)
        bull, bear = soc.get("bull", 0), soc.get("bear", 0)
        stotal = bull + bear
        c = csig.get(tk) or {}
        info = c or tech.get(tk) or {}
        bull_pct = round(bull / stotal * 100) if stotal >= 3 else None
        row = {
            "ticker": tk, "company": info.get("company", tk),
            "sector": c.get("sector") or (tech.get(tk) or {}).get("sector") or "OTHER",
            "rec": r.get("rec") if has_rating else None,
            "target": r.get("target") if has_rating else None, "current": r.get("current"),
            "upside": upside if has_rating else None, "n_analysts": n if has_rating else None,
            "buy": buy, "hold": hold, "sell": sell,
            "buzz": soc.get("msgs", 0), "bull": bull, "bear": bear, "q_msgs": stotal,
            "bull_pct": bull_pct,
            "congress": c.get("member_count", 0) if c.get("net_value", 0) > 0 else 0,
            "insiders": (isig.get(tk) or {}).get("n_buyers", 0),
            "r6": (tech.get(tk) or {}).get("r6"),
            "above200": (tech.get(tk) or {}).get("above200"),
        }
        row["edge"] = _edge_score(row, has_rating)
        rows.append(row)
    rows.sort(key=lambda s: s["edge"], reverse=True)
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "stocks": rows}


def _edge_score(row, has_rating):
    """Composite 0-100 alpha 'edge' -- a synthesis of the documented signals, not a
    single noisy one: analyst implied upside + buy consensus (weak alone, useful in
    aggregate), how many members of Congress are buying, corporate-insider buying,
    price momentum, and a CONTRARIAN divergence term (pros ahead of an unexcited
    crowd scores up; a euphoric crowd running ahead of the pros is penalised)."""
    e = 0.0
    up = row.get("upside")
    tot = row["buy"] + row["hold"] + row["sell"]
    if has_rating:
        if up is not None:
            e += max(0.0, min(50.0, up)) / 50 * 25          # implied upside to target
        if tot:
            e += (row["buy"] - row["sell"]) / tot * 10        # analyst buy consensus
    e += min(row["congress"], 10) / 10 * 20                   # congressional buying breadth
    ins = row["insiders"]
    e += 12 if ins >= 2 else 7 if ins >= 1 else 0             # corporate insider buying
    r6 = row.get("r6")
    if r6 is not None:
        e += max(0.0, min(50.0, r6)) / 50 * 18               # price momentum
    if has_rating and tot and row["bull_pct"] is not None and row["q_msgs"] >= 4:
        gap = (row["buy"] - row["sell"]) / tot - (row["bull_pct"] - 50) / 50
        if gap > 0.4:
            e += min(gap, 1.5) / 1.5 * 12                     # pros ahead of the crowd (contrarian +)
        elif gap < -0.4:
            e -= 10                                           # crowd euphoric vs pros (caution)
    return round(max(0.0, min(100.0, e)))


def build_performance(trades, prices):
    """'Follow Congress' index vs the S&P 500 -- Quiver's public methodology.

    Build an EQUAL-weighted portfolio that buys a stock the day Congress
    discloses buying it and closes the position when they disclose selling it,
    then track the cumulative return in the time following the disclosures,
    benchmarked against the S&P 500 over the same window. Equal weighting (rather
    than dollar-weighting by the reported amount RANGES) is deliberate: it keeps a
    single multi-million-dollar filing from hijacking the index, so the line
    reflects the breadth of what Congress is actually buying. Returns None if
    prices are unavailable (the site degrades gracefully)."""
    priced_trades = [t for t in trades if t.get("ticker") and t.get("est_amount")]
    if not priced_trades:
        return None

    spy = prices.get("SPY")
    if not spy:
        print("  WARN: no SPY prices -- skipping performance backtest", file=sys.stderr)
        return None

    end = max(spy)
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=BACKTEST_WINDOW)).strftime("%Y-%m-%d")
    dates = sorted(d for d in spy if start <= d <= end)
    if len(dates) < 20:
        return None

    def daily_returns(pr):
        out = {}
        for i in range(1, len(dates)):
            a, b = pr.get(dates[i - 1]), pr.get(dates[i])
            if a and b:
                out[dates[i]] = b / a - 1
        return out

    traded = {t["ticker"] for t in priced_trades if prices.get(t["ticker"])}
    rets = {tk: daily_returns(prices[tk]) for tk in traded}
    spy_rets = daily_returns(spy)

    # Follow timeline: +1 when Congress buys a ticker, -1 when they sell it.
    timeline = []
    for t in priced_trades:
        try:
            txd = datetime.strptime(t["transaction_date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if t["ticker"] not in rets:
            continue
        timeline.append((txd, t["ticker"], 1 if _is_buy(t["type"]) else -1))
    timeline.sort()

    net = {}   # ticker -> cumulative (buys - sells); a ticker is HELD when net > 0
    series = [{"d": dates[0], "s": 100.0, "m": 100.0}]
    ti = 0
    s_val = m_val = 100.0
    for i in range(1, len(dates)):
        dprev, dcur = dates[i - 1], dates[i]
        while ti < len(timeline) and timeline[ti][0] <= dprev:
            _, tk, sgn = timeline[ti]
            net[tk] = net.get(tk, 0) + sgn
            ti += 1
        held = [tk for tk, n in net.items() if n > 0]
        # equal-weighted daily return across every currently-held position
        r = sum(rets[tk].get(dcur, 0) for tk in held) / len(held) if held else 0.0
        s_val *= (1 + r)
        m_val *= (1 + spy_rets.get(dcur, 0))
        series.append({"d": dcur, "s": round(s_val, 3), "m": round(m_val, 3)})

    return {
        "series": series,
        "start_date": dates[0],
        "end_date": dates[-1],
        "n_tickers": len(rets),
        "n_positions": len(timeline),
        "methodology": ("Follow-Congress index (Quiver's public methodology): an equal-weighted "
                        "portfolio that buys a stock when a member of Congress discloses buying it and "
                        "closes the position when they disclose selling it, tracking the cumulative "
                        "return in the time following each disclosure vs the S&P 500. Equal-weighted so "
                        "no single multi-million-dollar filing dominates. Built only from the trades we "
                        "scrape over this window, so it reflects this period's disclosures — not a "
                        "multi-year record. STOCK Act filings are disclosed up to ~45 days after the "
                        "trade. Not investment advice; past performance does not predict future results."),
    }


def build_lag_study(trades, members):
    """The question the whole site turns on: does following Congress work AFTER
    you are allowed to see the trade?

    The STOCK Act gives members up to 45 days to file, and in practice a fifth
    of filings land well past that. Every headline return here -- and on every
    other site like it -- is measured from the transaction date, which is a
    price the public never had. This measures the same buys twice: once from the
    trade date (the member's experience) and once from the first close after the
    filing (a follower's), and reports the gap."""
    rows = [t for t in trades
            if _is_buy(t.get("type"))
            and t.get("excess_pct") is not None
            and t.get("excess_filed_pct") is not None
            and t.get("lag_days") is not None]
    if len(rows) < 100:
        return None

    lags = sorted(t["lag_days"] for t in rows)
    n = len(lags)

    def stats(key):
        v = sorted(t[key] for t in rows)
        mean = sum(v) / len(v)
        sd = (sum((x - mean) ** 2 for x in v) / max(1, len(v) - 1)) ** 0.5
        return {
            "mean": round(mean, 2),
            "median": round(v[len(v) // 2], 2),
            "win": round(100 * sum(1 for x in v if x > 0) / len(v)),
            "t": round(mean / (sd / len(v) ** 0.5), 2) if sd else None,
        }

    # dollar-weighted, matching how the leaderboard aggregates a member
    def dw(key):
        num = sum((t.get("est_amount") or 0) * t[key] for t in rows)
        den = sum((t.get("est_amount") or 0) for t in rows)
        return round(num / den, 2) if den else None

    buckets = []
    for lo, hi, lbl in [(0, 15, "Within 15 days"), (16, 30, "16-30 days"),
                        (31, 45, "31-45 days"), (46, 90, "46-90 days"),
                        (91, DISCLOSURE_MAX_LAG, "Over 90 days")]:
        g = [t for t in rows if lo <= t["lag_days"] <= hi]
        if len(g) < 25:
            continue
        buckets.append({
            "label": lbl, "n": len(g),
            "trade": round(sum(t["excess_pct"] for t in g) / len(g), 1),
            "filed": round(sum(t["excess_filed_pct"] for t in g) / len(g), 1),
        })

    # how many members keep a positive edge once the lag is priced in
    elig = [m for m in members if (m.get("filed_buys") or 0) >= MIN_SCORED_BUYS_BACKEND]
    kept = sum(1 for m in elig if (m.get("alpha_filed") or 0) > 0)
    trade_pos = sum(1 for m in members
                    if (m.get("priced_buys") or 0) >= MIN_SCORED_BUYS_BACKEND and (m.get("alpha") or 0) > 0)
    # cross-sectional skill test: more |t|>2 than luck would produce?
    ts = [m["tstat"] for m in members if m.get("tstat") is not None]
    strong = sum(1 for t in ts if abs(t) > 2)

    # Can they tell their own buys from their own sells? A stock-picker's buys
    # should beat the things they chose to get rid of. This compares the two
    # populations directly (Welch, unequal variances) and then repeats the test
    # inside each member so it cannot be driven by whoever trades most.
    sell_rows = [t["excess_filed_pct"] for t in trades
                 if not _is_buy(t.get("type")) and t.get("excess_filed_pct") is not None]
    buy_rows = [t["excess_filed_pct"] for t in rows]
    disc = None
    if len(sell_rows) >= 100 and len(buy_rows) >= 100:
        def ms(v):
            m = sum(v) / len(v)
            return m, (sum((x - m) ** 2 for x in v) / max(1, len(v) - 1)) ** 0.5, len(v)
        bm, bs, bn = ms(buy_rows)
        sm, ss_, sn = ms(sell_rows)
        se = (bs * bs / bn + ss_ * ss_ / sn) ** 0.5
        per_member = [m["alpha_filed"] - m["sell_alpha"] for m in members
                      if m.get("alpha_filed") is not None and m.get("sell_alpha") is not None]
        pm = None
        if len(per_member) >= 8:
            mu = sum(per_member) / len(per_member)
            sd = (sum((x - mu) ** 2 for x in per_member) / max(1, len(per_member) - 1)) ** 0.5
            pm = {"n": len(per_member), "mean": round(mu, 2),
                  "t": round(mu / (sd / len(per_member) ** 0.5), 2) if sd else None,
                  "positive": sum(1 for x in per_member if x > 0)}
        disc = {
            "buy_mean": round(bm, 2), "buy_n": bn,
            "sell_mean": round(sm, 2), "sell_n": sn,
            "gap": round(bm - sm, 2),
            "t": round((bm - sm) / se, 2) if se else None,
            "per_member": pm,
        }

    return {
        "n_trades": n,
        "discrimination": disc,
        "lag_median": lags[n // 2],
        "lag_mean": round(sum(lags) / n),
        "lag_p90": lags[int(n * 0.9)],
        "pct_over_45": round(100 * sum(1 for l in lags if l > 45) / n),
        "trade_date": stats("excess_pct"),
        "filed_date": stats("excess_filed_pct"),
        "dw_trade": dw("excess_pct"),
        "dw_filed": dw("excess_filed_pct"),
        "buckets": buckets,
        "members_scored": len(elig),
        "members_positive_filed": kept,
        "members_positive_trade": trade_pos,
        "tstat_tested": len(ts),
        "tstat_strong": strong,
        "tstat_expected_by_chance": round(0.05 * len(ts), 1),
    }


def build_conviction_validation(insiders):
    """Does the 0-100 conviction score actually predict anything?

    A score nobody checks is decoration. This sorts every scored buy that now
    has a realised excess return into quartiles and reports what each earned,
    plus the rank correlation between score and outcome and its t-stat. If the
    number is noise, the site should say so rather than keep displaying it as
    though it works."""
    rows = [x for x in (insiders.get("recent_buys") or [])
            if x.get("conviction") is not None and x.get("since_excess") is not None]
    if len(rows) < 24:
        return None
    rows.sort(key=lambda x: x["conviction"])
    n = len(rows)
    q = n // 4
    quartiles = []
    for i, lbl in enumerate(["Lowest", "Low-mid", "High-mid", "Highest"]):
        g = rows[i * q:(i + 1) * q] if i < 3 else rows[3 * q:]
        if not g:
            continue
        ex = [x["since_excess"] for x in g]
        quartiles.append({
            "label": lbl, "n": len(g),
            "lo": g[0]["conviction"], "hi": g[-1]["conviction"],
            "mean": round(sum(ex) / len(ex), 1),
            "median": round(sorted(ex)[len(ex) // 2], 1),
            "win": round(100 * sum(1 for e in ex if e > 0) / len(ex)),
        })

    xs = [x["conviction"] for x in rows]
    ys = [x["since_excess"] for x in rows]

    def corr(a, b):
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        sa = sum((v - ma) ** 2 for v in a) ** 0.5
        sb = sum((v - mb) ** 2 for v in b) ** 0.5
        return (sum((p_ - ma) * (q - mb) for p_, q in zip(a, b)) / (sa * sb)) if sa and sb else 0.0

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    def tof(r_, m):
        return r_ * ((m - 2) / max(1e-9, 1 - r_ * r_)) ** 0.5

    r_p = corr(xs, ys)                      # magnitude-sensitive
    r_s = corr(rank(xs), rank(ys))          # rank only -- robust to the skew
    t_s = tof(r_s, n)
    # outlier check: does the magnitude-based correlation survive losing the
    # three largest outcomes? If not, that number was about those three.
    keep = sorted(zip(xs, ys), key=lambda pr: -abs(pr[1]))[3:]
    r_trim = corr([k[0] for k in keep], [k[1] for k in keep]) if len(keep) > 8 else None

    return {
        "n": n,
        "quartiles": quartiles,
        "r": round(r_s, 3),                 # headline = Spearman
        "t": round(t_s, 2),
        "r_pearson": round(r_p, 3),
        "r_trimmed": round(r_trim, 3) if r_trim is not None else None,
        # The verdict is re-computed every refresh on a sample that grows slowly,
        # so a bare |t| > 2 would flip on noise. Require a real sample and the
        # ROBUST statistic to clear the bar.
        "significant": bool(n >= MIN_VALIDATION_N and abs(t_s) > 2),
        "provisional": bool(n < MIN_VALIDATION_N),
        # medians, because the top-bucket mean is carried by a couple of outcomes
        "spread": round(quartiles[-1]["mean"] - quartiles[0]["mean"], 1) if len(quartiles) >= 2 else None,
        "spread_median": (round(quartiles[-1]["median"] - quartiles[0]["median"], 1)
                          if len(quartiles) >= 2 else None),
    }


def _curve_risk(series):
    """Max drawdown and annualised volatility for a cumulative-return series
    (expressed in percent, starting at 0).

    Two curves ending at the same place are not the same result: one may have
    put you through a 30% hole to get there. `ret_vol` is return divided by
    volatility -- a Sharpe-shaped number with no risk-free rate, so it is only
    meaningful compared against the S&P line computed the same way."""
    if not series or len(series) < 10:
        return {}
    peak, mdd = -1e18, 0.0
    for v in series:
        peak = max(peak, v)
        dd = (1 + v / 100.0) / (1 + peak / 100.0) - 1
        mdd = min(mdd, dd)
    rets = []
    for i in range(1, len(series)):
        prev, cur = 1 + series[i - 1] / 100.0, 1 + series[i] / 100.0
        if prev > 0:
            rets.append(cur / prev - 1)
    vol = 0.0
    if len(rets) > 2:
        mu = sum(rets) / len(rets)
        vol = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * (252 ** 0.5) * 100
    final = series[-1]
    return {
        "maxdd": round(mdd * 100, 1),
        "vol": round(vol, 1),
        "ret_vol": round(final / vol, 2) if vol > 1 else None,
    }


def _snap_at(snaps, days_back):
    """The snapshot closest to `days_back` days before the latest one."""
    if not snaps:
        return None
    end = datetime.strptime(snaps[-1]["date"], "%Y-%m-%d")
    target = end - timedelta(days=days_back)
    return min(snaps, key=lambda s: abs((datetime.strptime(s["date"], "%Y-%m-%d") - target).days))


def build_flow(trades, stock_signals, history=None, windows=(90, 180)):
    """Where congressional money is moving, not where it already sits.

    A stock eight members have held for a year and a stock eight members bought
    last month look identical on the Stocks board. They are not the same signal.

    Everything here is keyed on the FILING date, which is both the date a reader
    could have acted on and -- unlike the rolling member counts in the daily
    snapshots -- free of decay artefacts. Counting members who filed inside a
    window against the window immediately before it isolates genuinely new
    interest from trades simply ageing out of a lookback."""
    if not trades:
        return {"has_history": False}
    meta = {s["ticker"]: s for s in stock_signals}
    dated = []
    for t in trades:
        if not t.get("ticker"):
            continue
        fd = _parse_mdy(t.get("filed_date"))
        if fd == datetime.min:
            continue
        dated.append((fd, t))
    if len(dated) < 50:
        return {"has_history": False}
    asof = max(fd for fd, _ in dated)

    def agg(lo, hi):
        """Per-ticker aggregate of filings in [asof-hi, asof-lo)."""
        out = {}
        for fd, t in dated:
            age = (asof - fd).days
            if not (lo <= age < hi):
                continue
            tk = t["ticker"]
            b = out.setdefault(tk, {"buyers": set(), "sellers": set(), "net": 0, "vol": 0, "n": 0})
            val = t.get("est_amount") or 0
            if _is_buy(t.get("type")):
                b["buyers"].add(t["member"])
                b["net"] += val
            else:
                b["sellers"].add(t["member"])
                b["net"] -= val
            b["vol"] += val
            b["n"] += 1
        return out

    out = {"has_history": True, "as_of": asof.strftime("%Y-%m-%d"),
           "windows": list(windows)}

    for days in windows:
        cur, prev = agg(0, days), agg(days, days * 2)
        rows = []
        for tk, c in cur.items():
            m = meta.get(tk)
            p_ = prev.get(tk) or {"buyers": set(), "sellers": set(), "net": 0, "vol": 0}
            nb, pb = len(c["buyers"]), len(p_["buyers"])
            rows.append({
                "ticker": tk,
                "company": (m or {}).get("company") or "",
                "sector": (m or {}).get("sector") or "",
                "buyers": nb, "sellers": len(c["sellers"]),
                "prev_buyers": pb,
                "d_buyers": nb - pb,
                "net": c["net"], "vol": c["vol"], "trades": c["n"],
                "fresh": pb == 0 and nb > 0,
                "insiders": (m or {}).get("insider_buyers") or 0,
                "edge": (m or {}).get("edge"),
                "above200": (m or {}).get("above200"),
                "r6": (m or {}).get("r6"),
                "members_total": (m or {}).get("member_count") or 0,
                "spark": ((history or {}).get("stock_series") or {}).get(tk),
            })

        # Building: more distinct buyers than the window before, net positive.
        building = sorted([r for r in rows if r["d_buyers"] > 0 and r["net"] > 0],
                          key=lambda r: (r["d_buyers"], r["net"]), reverse=True)[:15]
        # Fresh: nobody filed a buy in the prior window at all.
        fresh = sorted([r for r in rows if r["fresh"] and r["net"] > 0],
                       key=lambda r: (r["buyers"], r["vol"]), reverse=True)[:15]
        # Exiting: sellers outnumber buyers and the net is negative.
        exiting = sorted([r for r in rows if r["sellers"] > r["buyers"] and r["net"] < 0],
                         key=lambda r: r["net"])[:15]
        out["w%d" % days] = {
            "days": days,
            "building": building, "fresh": fresh, "exiting": exiting,
            "n_names": len(rows),
            "n_buyers": len(set().union(*[r_["buyers"] for r_ in cur.values()]) if cur else set()),
            "net_flow": sum(r["net"] for r in rows),
            "gross_flow": sum(r["vol"] for r in rows),
        }

    # ---- sector rotation on the longer window
    long_days = max(windows)
    cur, prev = agg(0, long_days), agg(long_days, long_days * 2)
    by_sec = {}
    for tk, c in cur.items():
        code = ((meta.get(tk) or {}).get("sector")) or "OTHER"
        b = by_sec.setdefault(code, {"sector": code, "net": 0, "prev_net": 0, "buyers": 0, "n": 0})
        b["net"] += c["net"]
        b["buyers"] += len(c["buyers"])
        b["n"] += 1
    for tk, pv in prev.items():
        code = ((meta.get(tk) or {}).get("sector")) or "OTHER"
        by_sec.setdefault(code, {"sector": code, "net": 0, "prev_net": 0, "buyers": 0, "n": 0})
        by_sec[code]["prev_net"] += pv["net"]
    rotation = [b for b in by_sec.values() if abs(b["net"]) > 10000 or abs(b["prev_net"]) > 10000]
    for b in rotation:
        b["d_net"] = b["net"] - b["prev_net"]
    rotation.sort(key=lambda b: b["net"], reverse=True)
    out["rotation"] = rotation[:14]
    out["rotation_days"] = long_days

    # ---- weekly net flow curve, by filing week
    weeks = {}
    for fd, t in dated:
        if (asof - fd).days > 365:
            continue
        wk = (fd - timedelta(days=fd.weekday())).strftime("%Y-%m-%d")
        val = t.get("est_amount") or 0
        weeks[wk] = weeks.get(wk, 0) + (val if _is_buy(t.get("type")) else -val)
    out["curve"] = [{"week": k, "net": v} for k, v in sorted(weeks.items())][-52:]
    return out


def build_member_race(members, trades, prices, top_n=5, min_buys=3):
    """The Home hero chart: cumulative-return curves of the members who are
    OUTPACING the S&P 500, drawn over the index itself.

    For each member we run the same equal-weighted 'follow' backtest as
    build_performance, but using only that member's own disclosed trades: hold a
    stock while their net position in it is positive, equal-weight across current
    holdings, and track the cumulative return over the backtest window. We then
    keep the members whose windowed return beats the S&P, ranked by that return,
    so the chart shows real outperformers rather than a lucky single trade
    (members need >=min_buys scored buys to qualify). Returns None if prices are
    unavailable."""
    spy = prices.get("SPY")
    if not spy or not trades:
        return None
    end = max(spy)
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=BACKTEST_WINDOW)).strftime("%Y-%m-%d")
    dates = sorted(d for d in spy if start <= d <= end)
    if len(dates) < 20:
        return None

    def daily_returns(pr):
        out = {}
        for i in range(1, len(dates)):
            a, b = pr.get(dates[i - 1]), pr.get(dates[i])
            if a and b:
                out[dates[i]] = b / a - 1
        return out

    traded = {t["ticker"] for t in trades if t.get("ticker") and prices.get(t["ticker"])}
    rets = {tk: daily_returns(prices[tk]) for tk in traded}
    spy_rets = daily_returns(spy)

    # S&P cumulative-return line (percent), the shared baseline.
    spy_series, m_val = [], 1.0
    for i, d in enumerate(dates):
        if i:
            m_val *= (1 + spy_rets.get(d, 0))
        spy_series.append(round((m_val - 1) * 100, 2))
    spy_final = spy_series[-1]

    # eligible members: enough scored buys to be a real record
    eligible = {m["member"] for m in members if (m.get("priced_buys") or 0) >= min_buys}
    # Two event streams per member. `by_member` reacts on the day the member
    # traded -- their own experience, and unobservable to anyone else.
    # `by_member_filed` reacts on the day the filing went public, which is the
    # only one a follower could have run. Same weighting, same holding rule.
    by_member, by_member_filed = {}, {}
    for t in trades:
        if t["member"] not in eligible or t.get("ticker") not in rets:
            continue
        sgn = 1 if _is_buy(t["type"]) else -1
        try:
            txd = datetime.strptime(t["transaction_date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        by_member.setdefault(t["member"], []).append((txd, t["ticker"], sgn))
        fdt = _parse_mdy(t.get("filed_date"))
        if fdt != datetime.min:
            by_member_filed.setdefault(t["member"], []).append(
                (fdt.strftime("%Y-%m-%d"), t["ticker"], sgn))

    def follow(tl):
        """Equal-weighted across whatever is currently held, rebalanced daily."""
        tl = sorted(tl)
        net, ti, val, invested = {}, 0, 1.0, 0
        series = [0.0]
        for i in range(1, len(dates)):
            dprev, dcur = dates[i - 1], dates[i]
            while ti < len(tl) and tl[ti][0] <= dprev:
                _, tk, sg = tl[ti]
                net[tk] = net.get(tk, 0) + sg
                ti += 1
            held = [tk for tk, n in net.items() if n > 0]
            if held:
                invested += 1
            r = sum(rets[tk].get(dcur, 0) for tk in held) / len(held) if held else 0.0
            val *= (1 + r)
            series.append(round((val - 1) * 100, 2))
        return series, round(invested / max(1, len(dates) - 1), 3)

    runs = []
    for member, tl in by_member.items():
        series, coverage = follow(tl)
        f_series, f_coverage = follow(by_member_filed.get(member, []))
        runs.append({"member": member, "final": series[-1], "series": series,
                     "coverage": coverage,
                     "filed_final": f_series[-1], "filed_series": f_series,
                     "filed_coverage": f_coverage,
                     "risk": _curve_risk(series), "filed_risk": _curve_risk(f_series)})

    party = {m["member"]: m.get("party", "?") for m in members}
    # A member who only starts holding late in the window draws a flat line that
    # spikes at the end -- the curve looks broken and the ranking rewards a short
    # lucky window. Require them to have actually been invested for most of it.
    MIN_COVERAGE = 0.6
    ahead = [r for r in runs if r["final"] > spy_final]
    qualified = sorted([r for r in ahead if r["coverage"] >= MIN_COVERAGE],
                       key=lambda r: r["final"], reverse=True)
    beating = qualified[:top_n]
    if len(beating) < top_n:  # fall back to the best-covered of the rest
        rest = sorted([r for r in ahead if r["coverage"] < MIN_COVERAGE],
                      key=lambda r: (r["coverage"], r["final"]), reverse=True)
        beating += rest[:top_n - len(beating)]
    for r in beating:
        r["party"] = party.get(r["member"], "?")
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "start_date": dates[0], "end_date": dates[-1],
        "dates": dates,
        "spy": spy_series, "spy_final": spy_final,
        "spy_risk": _curve_risk(spy_series),
        "n_beating": sum(1 for r in runs if r["final"] > spy_final),
        "n_beating_filed": sum(1 for r in runs if r["filed_final"] > spy_final),
        "n_ranked": len(runs),
        "members": beating,
    }


# ---------------------------------------------------------------------------
# Screener -- Finviz-style, but organized by INVESTMENT PHILOSOPHY. Technical
# indicators (200-DMA, RSI, momentum, drawdown) computed from the daily closes
# we already cache, then bucketed so each philosophy surfaces different names.
# ---------------------------------------------------------------------------

def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _sparkline(series, n=22, window=126):
    """A compact price sparkline: the last ~6 months of closes downsampled to n
    points and normalised to 0-100 (period low..high). Tiny to ship, enough to
    draw a shape in a row or the modal. `series` is a {date: close} dict."""
    if not series:
        return None
    closes = [c for _, c in sorted(series.items()) if c]
    if len(closes) < 8:
        return None
    w = closes[-window:]
    step = max(1, len(w) // n)
    pts = w[::step][-n:]
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    return [round((p - lo) / rng * 100) for p in pts]


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    return (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return round(100 - 100 / (1 + rs), 1)


UNIVERSE_PATH = os.path.join(SCRIPT_DIR, "..", "universe.json")
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
# names that signal a non-common-stock security we don't want in the screener
_NONCOMMON = re.compile(r"warrant|\bunit(s)?\b|\bright(s)?\b|preferred|depositary|when issued|%|convertible note", re.I)


def _parse_listing(text, sym_i, name_i, etf_i, test_i, out):
    for ln in text.splitlines()[1:]:
        if ln.startswith("File Creation Time"):
            break
        p = ln.split("|")
        if len(p) <= max(sym_i, name_i, etf_i, test_i):
            continue
        sym, name = p[sym_i].strip().upper(), p[name_i].strip()
        if p[test_i].strip() == "Y" or p[etf_i].strip() == "Y":
            continue
        if not sym.isalpha() or len(sym) > 5 or _NONCOMMON.search(name):
            continue
        # trim boilerplate suffixes from the display name
        short = re.split(r"\s*[-–]\s*(Common|Class|Ordinary|American)", name)[0].strip() or name
        out.setdefault(sym, {"name": short[:48]})


def load_universe():
    """Every US-listed common stock (NASDAQ + NYSE/AMEX) from the exchanges'
    official symbol directories, cached ~7 days. This is the screener's universe
    -- the whole market, small caps included, independent of congressional data."""
    if os.path.exists(UNIVERSE_PATH):
        try:
            cached = json.load(open(UNIVERSE_PATH, encoding="utf-8"))
            asof = cached.get("_asof")
            if asof and (datetime.now() - datetime.strptime(asof, "%Y-%m-%d")).days < 7:
                return cached.get("tickers", {})
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    tickers = {}
    try:
        nas = requests.get(NASDAQ_LISTED, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT).text
        oth = requests.get(OTHER_LISTED, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT).text
        # nasdaqlisted: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF|NextShares
        _parse_listing(nas, 0, 1, 6, 3, tickers)
        # otherlisted: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
        _parse_listing(oth, 0, 1, 4, 6, tickers)
    except requests.RequestException as e:
        print(f"  WARN: could not load stock universe: {e}", file=sys.stderr)
        if os.path.exists(UNIVERSE_PATH):
            try:
                return json.load(open(UNIVERSE_PATH, encoding="utf-8")).get("tickers", {})
            except (json.JSONDecodeError, OSError):
                pass
        return {}
    json.dump({"_asof": datetime.now().strftime("%Y-%m-%d"), "tickers": tickers},
              open(UNIVERSE_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"  universe: {len(tickers)} US common stocks", file=sys.stderr)
    return tickers


_SCR_JUNK = re.compile(r"acquisition|\bspac\b|merger corp|capital corp|investment corp|"
                       r"capital investment|\btrust\b|\bfund\b|\bETF\b|\bETN\b|"
                       r"fixed|floating|\brate\b|preferred|\bpfd\b|\bnote|depositary|"
                       r"debenture|\bctf\b|\bsr\b|warrant|"
                       r"voya|virtus|nuveen|\bpimco\b|abrdn|eaton vance|cohen & steers|"
                       r"income fund|dividend|municipal|\bmuni\b", re.I)


def _stock_indicators(closes):
    """Technical indicators from a close series (assumed chronological), or None
    if it fails basic history / price / volatility quality gates. Shared by the
    live screener and the point-in-time agent backtester."""
    if len(closes) < 200 or not closes[-1]:
        return None
    last = closes[-1]
    if last < 3:  # pennies / stale data
        return None
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    hi = max(closes[-252:])
    vs_high = round((last / hi - 1) * 100, 1) if hi else None
    def ret(n):
        return round((last / closes[-n] - 1) * 100, 1) if len(closes) > n and closes[-n] else None
    r1, r3, r6 = ret(21), ret(63), ret(126)
    drets = [closes[i] / closes[i - 1] - 1 for i in range(-63, 0) if closes[i - 1]]
    vol = round(_stdev(drets) * (252 ** 0.5) * 100, 1) if len(drets) > 2 else None
    if vol is None or vol < 8 or vol > 250:            # SPAC trusts / split glitches
        return None
    if (r6 is not None and r6 > 400) or (r1 is not None and r1 > 150):  # low-float pumps
        return None
    sma50p, sma200p = _sma(closes[:-15], 50), _sma(closes[:-15], 200)
    return {
        "price": round(last, 2),
        "vs200": round((last / sma200 - 1) * 100, 1) if sma200 else None,
        "vs_high": vs_high, "rsi": _rsi(closes), "r1": r1, "r3": r3, "r6": r6, "vol": vol,
        "above200": bool(sma200 and last > sma200),
        "golden": bool(sma50 and sma200 and sma50 > sma200 and sma50p and sma200p and sma50p <= sma200p),
    }


def build_screener(prices, universe, mcaps):
    """Return every screenable stock (mid-cap and above) with its technical
    indicators + market cap. The front-end buckets these into preset philosophies
    and applies custom filters. Quality gates keep pennies, SPAC shells, low-float
    pumps and sub-$2B names out."""
    stocks = []
    for tk, info in universe.items():
        name = info.get("name", tk)
        if _SCR_JUNK.search(name):
            continue
        mc = mcaps.get(tk)
        if not mc or mc < MID_CAP_FLOOR:   # mid-cap and above only
            continue
        series = prices.get(tk)
        if not series:
            continue
        ind = _stock_indicators([c for _, c in sorted(series.items())])
        if ind is None:
            continue
        ind.pop("r1", None)  # not displayed -- trim payload
        stocks.append({"ticker": tk, "company": _clean_company_name(name), "sector": info.get("sector", ""),
                       "mcap": mc, "spark": _sparkline(series), **ind})
    stocks.sort(key=lambda s: s["mcap"] or 0, reverse=True)
    return {"universe": len(stocks), "min_mcap": MID_CAP_FLOOR, "stocks": stocks}


def build_conviction(stock_signals, insiders, screener):
    """The site's edge: where independent smart-money signals CONFLUENCE on the
    same stock -- Congress buying + corporate insiders buying + a technical
    uptrend. Two or more aligned is a far stronger tell than any one alone."""
    csig = {s["ticker"]: s for s in stock_signals}
    isig = (insiders or {}).get("signals") or {}
    tech = {s["ticker"]: s for s in (screener.get("stocks") or [])}

    tickers = set()
    for tk, s in csig.items():
        if s.get("net_value", 0) > 0 and s.get("member_count", 0) >= 1:
            tickers.add(tk)
    for tk, s in isig.items():
        if s.get("n_buyers", 0) >= 1 and s.get("buy_value", 0) > 0:
            tickers.add(tk)

    rows = []
    for tk in tickers:
        c, i, t = csig.get(tk), isig.get(tk), tech.get(tk)
        cong_buy = bool(c and c.get("net_value", 0) > 0 and c.get("member_count", 0) >= 1)
        ins_buy = bool(i and i.get("n_buyers", 0) >= 1 and i.get("buy_value", 0) > 0)
        uptrend = bool(t and t.get("above200") and (t.get("r6") or 0) > 0)
        signals, score = [], 0.0
        if cong_buy:
            n = c["member_count"]
            score += 1 + min(n - 1, 3) * 0.4
            signals.append({"src": "congress", "label": f"{n} in Congress buying"})
            if c.get("bipartisan"):
                score += 0.5
                signals.append({"src": "bipartisan", "label": "Bipartisan"})
        if ins_buy:
            n = i["n_buyers"]
            score += 1.2 + min(n - 1, 3) * 0.4   # insiders weighted a touch higher
            signals.append({"src": "insider", "label": f"{n} insider{'s' if n > 1 else ''} buying"})
        if uptrend:
            score += 0.8
            signals.append({"src": "trend", "label": "Uptrend"})
        confluence = cong_buy + ins_buy + uptrend
        rows.append({
            "ticker": tk,
            "company": (c or {}).get("company") or (i or {}).get("company") or (t or {}).get("company") or tk,
            "sector": (c or {}).get("sector") or (t or {}).get("sector") or "OTHER",
            "confluence": confluence, "score": round(score, 2), "signals": signals,
            "member_count": (c or {}).get("member_count", 0), "n_insiders": (i or {}).get("n_buyers", 0),
            "congress_value": (c or {}).get("net_value", 0), "insider_value": (i or {}).get("buy_value", 0),
            "r6": (t or {}).get("r6"), "vs200": (t or {}).get("vs200"), "mcap": (t or {}).get("mcap"),
        })
    rows.sort(key=lambda r: (r["confluence"], r["score"], r["congress_value"] + r["insider_value"]), reverse=True)
    return {"stocks": rows[:30], "multi_signal": sum(1 for r in rows if r["confluence"] >= 2)}


# ---------------------------------------------------------------------------
# Agents -- simulated, no-fee model portfolios. Each agent is a rules-based
# strategy (a "philosophy") that we BACKTEST point-in-time: at each monthly
# rebalance we re-screen using only data available then (no look-ahead), hold
# the picks equal-weighted for the month, and compound. Educational only.
# ---------------------------------------------------------------------------

# (select filter, rank key desc, hold count, name, tag, thesis). VTI holds all.
AGENT_DEFS = [
    {"key": "vti", "name": "Total Market", "tag": "VTI mimic", "hold": None,
     "thesis": "Own the whole market, equal-weighted — a fee-free take on a total-market index fund. Maximum diversification, minimal maintenance.",
     "sel": lambda i: True, "rank": lambda i: 0},
    {"key": "momentum", "name": "Momentum", "tag": "Trend-following", "hold": 25,
     "thesis": "Ride the winners. Each month, buy the strongest uptrends — above the 50- and 200-day averages with the biggest 6-month gains — and rotate as leadership changes.",
     "sel": lambda i: i["above200"] and (i["r6"] or 0) > 15 and (i["vs200"] or 0) > 5, "rank": lambda i: i["r6"] or 0},
    {"key": "value", "name": "Deep Value", "tag": "Contrarian", "hold": 25,
     "thesis": "Buy the beaten-down. Each month, hold names 30-80% below their 52-week high (but not left for dead), betting on mean reversion and turnarounds.",
     "sel": lambda i: i["vs_high"] is not None and -80 < i["vs_high"] < -30, "rank": lambda i: -(i["vs_high"] or 0)},
    {"key": "steady", "name": "Steady Compounders", "tag": "Low volatility", "hold": 25,
     "thesis": "Sleep-at-night growth. Above the 200-day, below-average volatility, but still gaining — the low-drama winners that grind steadily higher.",
     "sel": lambda i: i["above200"] and 12 <= (i["vol"] or 999) < 32 and (i["r6"] or 0) > 8, "rank": lambda i: -(i["vol"] or 0)},
]


def build_agents(screener_prices, universe, congress_perf=None):
    """Point-in-time monthly-rebalanced backtests of each philosophy agent."""
    # clean, priced universe with a date->close map
    stocks = {}
    for tk, info in universe.items():
        if _SCR_JUNK.search(info.get("name", tk)):
            continue
        s = screener_prices.get(tk)
        if not s or len(s) < 220:
            continue
        items = sorted(s.items())
        stocks[tk] = {"name": info["name"], "dates": [d for d, _ in items], "closes": [c for _, c in items]}
    if len(stocks) < 50:
        return {"agents": [], "as_of": None}

    # monthly rebalance dates = last trading day of each month across the union axis
    month_last = {}
    for st in stocks.values():
        for d in st["dates"]:
            month_last[d[:7]] = max(month_last.get(d[:7], ""), d)
    targets = [month_last[m] for m in sorted(month_last)][-13:]  # ~1 year, 13 month-ends

    # snapshot indicators + price as-of each rebalance date (computed once, shared)
    import bisect
    snaps = {t: {} for t in targets}
    for tk, st in stocks.items():
        ds, cs = st["dates"], st["closes"]
        for t in targets:
            idx = bisect.bisect_right(ds, t) - 1
            if idx < 200:
                continue
            ind = _stock_indicators(cs[:idx + 1])
            if ind:
                snaps[t][tk] = (cs[idx], ind)

    def backtest(sel, rank, hold):
        val = 100.0
        series = [{"d": targets[0], "v": 100.0}]
        turnovers = []
        prev = set()
        for a, b in zip(targets, targets[1:]):
            picks = [(tk, px) for tk, (px, ind) in snaps[a].items() if sel(ind)]
            picks.sort(key=lambda x: rank(snaps[a][x[0]][1]), reverse=True)
            if hold:
                picks = picks[:hold]
            held = {tk for tk, _ in picks}
            rets = []
            for tk, p0 in picks:
                nb = snaps[b].get(tk)
                if nb and p0:
                    rets.append(nb[0] / p0 - 1)
            r = sum(rets) / len(rets) if rets else 0.0
            val *= (1 + r)
            series.append({"d": b, "v": round(val, 2)})
            if prev:
                turnovers.append(len(held - prev) / max(1, len(held)))
            prev = held
        total = round((series[-1]["v"] / 100 - 1) * 100, 1)
        turnover = round(sum(turnovers) / len(turnovers) * 100) if turnovers else 0
        return series, total, turnover

    mkt_series, mkt_total, _ = backtest(lambda i: True, lambda i: 0, None)

    agents = []
    for d in AGENT_DEFS:
        series, total, turnover = backtest(d["sel"], d["rank"], d["hold"])
        # current holdings from the latest snapshot
        latest = snaps[targets[-1]]
        picks = [(tk, ind) for tk, (px, ind) in latest.items() if d["sel"](ind)]
        picks.sort(key=lambda x: d["rank"](x[1]), reverse=True)
        if d["hold"]:
            picks = picks[:d["hold"]]
        # the own-everything agent is too broad to list -- show a note instead
        holdings = [] if d["hold"] is None else [
            {"ticker": tk, "company": stocks[tk]["name"], "price": ind["price"],
             "r6": ind["r6"], "vs200": ind["vs200"], "vs_high": ind["vs_high"]}
            for tk, ind in picks[:24]]
        agents.append({
            "key": d["key"], "name": d["name"], "tag": d["tag"], "thesis": d["thesis"],
            "hold": d["hold"] or len(picks), "n_holdings": len(picks),
            "total_return": total, "vs_market": round(total - mkt_total, 1),
            "turnover": turnover, "series": series, "market": mkt_series,
            "holdings": holdings,
        })

    # Follow Congress agent -- reuse the congressional follow backtest if present
    if congress_perf and congress_perf.get("series") and len(congress_perf["series"]) > 2:
        cs = congress_perf["series"]
        cong = [{"d": p["d"], "v": round(p["s"], 2)} for p in cs]
        cmkt = [{"d": p["d"], "v": round(p["m"], 2)} for p in cs]
        ctot = round((cs[-1]["s"] / cs[0]["s"] - 1) * 100, 1)
        cmt = round((cs[-1]["m"] / cs[0]["m"] - 1) * 100, 1)
        agents.append({
            "key": "congress", "name": "Follow Congress", "tag": "Political alpha",
            "thesis": "Mirror Congress. Buys every stock members of Congress disclose buying (equal-weighted), tracking the crowd that trades on Capitol Hill knowledge.",
            "hold": congress_perf.get("n_tickers", 0), "n_holdings": congress_perf.get("n_tickers", 0),
            "total_return": ctot, "vs_market": round(ctot - cmt, 1), "turnover": None,
            "series": cong, "market": cmkt, "holdings": [],
        })

    return {"as_of": targets[-1], "market_return": mkt_total, "agents": agents}


# ---------------------------------------------------------------------------
# Historical tracking -- persist daily snapshots so the site can show
# momentum over time (which stocks are GAINING congressional attention).
# ---------------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"snapshots": []}
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"snapshots": []}


def update_history(overview, stock_signals):
    """Upsert today's snapshot (keyed by UTC date, so the 6-hourly runs update
    the same day's entry) and trim to HISTORY_MAX_DAYS. Returns the history."""
    history = load_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot = {
        "date": today,
        "total_value": overview["total_value"],
        "party_value": overview.get("party_value", {}),
        # compact per-stock metrics: members (n), net dollars, total dollars
        "stocks": {s["ticker"]: {"n": s["member_count"], "net": s["net_value"], "v": s["total_value"]}
                   for s in stock_signals},
    }
    snaps = [s for s in history.get("snapshots", []) if s.get("date") != today]
    snaps.append(snapshot)
    snaps.sort(key=lambda s: s["date"])
    history["snapshots"] = snaps[-HISTORY_MAX_DAYS:]
    # member-count series per ticker, so a flow row can show its own shape
    series = {}
    for snap in history["snapshots"][-30:]:
        for tk, v in snap["stocks"].items():
            series.setdefault(tk, []).append(v["n"])
    history["stock_series"] = {tk: v for tk, v in series.items() if len(v) >= 8 and max(v) > 0}
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, separators=(",", ":"))  # compact -- this file grows
    return history


def compute_trends(history, stock_signals, lookback_days=7):
    """Compare today's per-stock metrics to ~lookback_days ago to surface
    what's GAINING attention (new congressional buyers) and momentum."""
    snaps = history.get("snapshots", [])
    if len(snaps) < 2:
        return {"has_history": False, "days_tracked": len(snaps),
                "gaining_attention": [], "momentum_buys": [], "momentum_sells": []}

    latest = snaps[-1]["stocks"]
    # pick the snapshot closest to lookback_days ago (or the oldest we have)
    target = datetime.strptime(snaps[-1]["date"], "%Y-%m-%d") - timedelta(days=lookback_days)
    prior = min(snaps[:-1], key=lambda s: abs((datetime.strptime(s["date"], "%Y-%m-%d") - target).days))
    prior_stocks = prior["stocks"]
    days_between = (datetime.strptime(snaps[-1]["date"], "%Y-%m-%d") - datetime.strptime(prior["date"], "%Y-%m-%d")).days

    meta = {s["ticker"]: s for s in stock_signals}
    rows = []
    for tk, now in latest.items():
        if meta.get(tk, {}).get("sector") == "OTHER" and now["n"] < 2:
            continue
        was = prior_stocks.get(tk, {"n": 0, "net": 0, "v": 0})
        rows.append({
            "ticker": tk,
            "company": meta.get(tk, {}).get("company", tk),
            "sector": meta.get(tk, {}).get("sector", "OTHER"),
            "d_members": now["n"] - was["n"],
            "d_net": now["net"] - was["net"],
            "members": now["n"],
            "net_value": now["net"],
        })

    gaining = sorted([r for r in rows if r["d_members"] > 0],
                     key=lambda r: (r["d_members"], r["net_value"]), reverse=True)[:8]
    mom_buy = sorted([r for r in rows if r["d_net"] > 0],
                     key=lambda r: r["d_net"], reverse=True)[:8]
    mom_sell = sorted([r for r in rows if r["d_net"] < 0],
                      key=lambda r: r["d_net"])[:8]
    return {
        "has_history": True,
        "days_tracked": len(snaps),
        "window_days": days_between,
        "gaining_attention": gaining,
        "momentum_buys": mom_buy,
        "momentum_sells": mom_sell,
    }


def build_stock_history(history, top_tickers, days=30):
    """Compact per-stock time series (member count) for sparklines, limited to
    the given tickers and recent days to keep data.json lean."""
    snaps = history.get("snapshots", [])[-days:]
    want = set(top_tickers)
    series = {}
    for tk in want:
        pts = [(s["date"], s["stocks"].get(tk, {}).get("n", 0)) for s in snaps if tk in s["stocks"]]
        if len(pts) >= 2:
            series[tk] = [n for _, n in pts]
    return series


def build_overview(bills, trades, members, stock_signals):
    """Front-door dashboard headline numbers -- the 'what's happening right now'
    summary a Quiver/Autopilot user sees first."""
    total_value = sum(t.get("est_amount", 0) for t in trades)
    buy_value = sum(t.get("est_amount", 0) for t in trades if _is_buy(t["type"]))
    sell_value = total_value - buy_value
    party_value = {"D": 0, "R": 0, "I": 0}
    party_trades = {"D": 0, "R": 0, "I": 0}
    for t in trades:
        p = t.get("party")
        if p in party_value:
            party_value[p] += t.get("est_amount", 0)
            party_trades[p] += 1
    # most bought / sold by net dollar direction (tracked tickers only)
    ranked_net = sorted(stock_signals, key=lambda s: s["net_value"], reverse=True)
    top_bought = ranked_net[0] if ranked_net and ranked_net[0]["net_value"] > 0 else None
    top_sold = ranked_net[-1] if ranked_net and ranked_net[-1]["net_value"] < 0 else None
    biggest = sorted(trades, key=lambda t: t.get("est_amount", 0), reverse=True)[:8]
    latest = sorted(trades, key=lambda t: _parse_mdy(t["filed_date"]), reverse=True)[:12]

    def trade_row(t):
        return {k: t.get(k) for k in ("member", "chamber", "ticker", "company", "sector",
                                       "type", "amount_range", "est_amount", "transaction_date",
                                       "filed_date", "report_url")}
    return {
        "total_trades": len(trades),
        "total_value": total_value,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "party_value": party_value,
        "party_trades": party_trades,
        "active_members": len(members),
        "tracked_tickers_traded": sum(1 for s in stock_signals if s["sector"] != "OTHER"),
        "bill_count": len(bills),
        "key_bill_count": sum(1 for b in bills if b.get("key_bill")),
        "appropriation_count": sum(1 for b in bills if b.get("is_appropriation")),
        "most_active_member": members[0]["member"] if members else None,
        "top_bought": {"ticker": top_bought["ticker"], "company": top_bought["company"],
                       "net_value": top_bought["net_value"]} if top_bought else None,
        "top_sold": {"ticker": top_sold["ticker"], "company": top_sold["company"],
                     "net_value": top_sold["net_value"]} if top_sold else None,
        "biggest_trades": [trade_row(t) for t in biggest],
        "latest_trades": [trade_row(t) for t in latest],
    }


def main():
    sectors = load_sectors()
    ticker_index = build_ticker_index(sectors)

    roster = fetch_member_roster()
    member_index = build_member_index(roster)

    bills = fetch_bills(sectors)
    bills = enrich_bills(bills)
    trades_cache = load_trades_cache()
    trade_start = datetime.now() - timedelta(days=TRADES_LOOKBACK_DAYS)
    scrape_senate_filings(trades_cache, trade_start, datetime.now())
    scrape_house_filings(trades_cache, trade_start, datetime.now())
    save_trades_cache(trades_cache)
    trades = build_trades_from_cache(trades_cache, ticker_index, trade_start)
    trades = annotate_trade_values(trades)
    trades = annotate_trade_parties(trades, member_index)

    # Corporate insider trades (SEC Form 4) -- independent of congressional data.
    insider_cache = load_insider_cache()
    scrape_insider_filings(insider_cache)
    save_insider_cache(insider_cache)
    # tickers insiders BOUGHT -- priced below so we can measure return-since-bought
    insider_tks = sorted({f["ticker"] for f in insider_cache.values()
                          if f.get("ticker") and any(t.get("code") == "P" for t in (f.get("txns") or []))})

    # Route every 'Other' trade into its real economic sector (GICS via Yahoo),
    # so the ~12 niche policy themes no longer leave blue-chips unclassified.
    print("Classifying traded tickers into economic sectors...", file=sys.stderr)
    ticker_gics = fetch_ticker_sectors([t["ticker"] for t in trades if t.get("ticker")])
    trades = reclassify_trades(trades, ticker_gics)

    # Price every traded ticker once (cached daily in prices.json), then reuse
    # for both per-trade P&L and the Congress-vs-market backtest.
    print("Fetching prices for trade P&L + backtest...", file=sys.stderr)
    prices = fetch_prices([t["ticker"] for t in trades if t.get("ticker")] + insider_tks)
    trades = annotate_trade_pnl(trades, prices)

    bills = [analyze_appropriation(b) for b in bills]
    bills = attach_beneficiary_stocks(bills, sectors)
    bills = attach_impact_analysis(bills)
    bills = flag_pre_filing_trades(bills, trades)
    bills = attach_bill_trades(bills, trades)
    bills = mark_key_bills(bills)
    sector_summaries = build_sector_summaries(sectors, bills, trades)
    members = build_member_profiles(trades)
    stock_signals = build_stock_signals(trades)
    # Insider alpha: now that prices + Congress signals exist, score each insider
    # buy by return-since-bought, conviction and cross-signal confluence.
    cong_buyers = {s["ticker"]: s["member_count"] for s in stock_signals if s.get("net_value", 0) > 0}
    insiders = build_insider_data(insider_cache, prices, cong_buyers)
    overview = build_overview(bills, trades, members, stock_signals)
    unusual = build_unusual_activity(stock_signals)
    performance = build_performance(trades, prices)
    # Home hero: cumulative-return curves of the members outpacing the S&P.
    member_race = build_member_race(members, trades, prices)
    lag_study = build_lag_study(trades, members)
    conviction_check = build_conviction_validation(insiders)
    if lag_study:
        print(f"  disclosure lag: median {lag_study['lag_median']}d, "
              f"trade-date excess {lag_study['trade_date']['mean']}% vs "
              f"filing-date {lag_study['filed_date']['mean']}%", file=sys.stderr)
    if conviction_check:
        print(f"  conviction score: spearman={conviction_check['r']} t={conviction_check['t']} "
              f"(pearson={conviction_check['r_pearson']}, trimmed={conviction_check['r_trimmed']}) "
              f"n={conviction_check['n']} "
              f"({'holds' if conviction_check['significant'] else 'not proven'})",
              file=sys.stderr)

    # Standalone whole-market technical screener (independent of congress).
    print("Building philosophy screener over the US stock universe...", file=sys.stderr)
    universe = load_universe()
    # Canonical, cleaned display names from the market universe -- override the raw
    # disclosure/PDF names on the aggregated signal tables (which conviction and
    # the sentiment view inherit) so every listed ticker reads cleanly.
    canon_names = {tk: _clean_company_name(info.get("name")) for tk, info in universe.items() if info.get("name")}
    for s in stock_signals:
        nm = canon_names.get(s["ticker"])
        if nm:
            s["company"] = nm
    screener_prices = fetch_screener_prices(list(universe.keys())) if universe else {}
    mcaps = fetch_market_caps(list(universe.keys())) if universe else {}
    screener = build_screener(screener_prices, universe, mcaps)
    conviction = build_conviction(stock_signals, insiders, screener)

    # Analyst ratings + retail social sentiment for the stocks that matter here.
    print("Fetching analyst ratings + social sentiment...", file=sys.stderr)
    street_tickers = ([r["ticker"] for r in conviction["stocks"]]
                      + [s["ticker"] for s in stock_signals[:120]]
                      + [s["ticker"] for s in screener["stocks"][:60]])
    street_tickers = list(dict.fromkeys(t for t in street_tickers if t))[:STREET_MAX_TICKERS]
    ratings = fetch_ratings(street_tickers)
    social = fetch_social(street_tickers)
    street = build_street(street_tickers, ratings, social, stock_signals, insiders, screener)

    # Cross-reference the screener with THIS site's own signals so it isn't just a
    # generic technical screen: tag every stock with its economic sector, how many
    # members of Congress are net-buying it, and the analyst view where we have
    # coverage. Sectors are cached (TTL) so the first run backfills the universe
    # and later runs are cheap.
    print("Cross-referencing screener with sector + Congress + analyst signals...", file=sys.stderr)
    scr_gics = fetch_ticker_sectors([s["ticker"] for s in screener["stocks"]])
    csig = {s["ticker"]: s for s in stock_signals}
    strat = {s["ticker"]: s for s in street["stocks"]}
    for s in screener["stocks"]:
        s["sector"] = GICS_TO_CODE.get(scr_gics.get(s["ticker"])) or ""
        c = csig.get(s["ticker"])
        s["cong"] = c["member_count"] if c and c.get("net_value", 0) > 0 else 0
        st = strat.get(s["ticker"])
        s["rate"] = st.get("rec") if st and st.get("rec") else None
        s["upside"] = st.get("upside") if st else None
    screener["sectors"] = sorted({s["sector"] for s in screener["stocks"] if s["sector"]})

    # Stock intelligence: weight each stock's Congress signal by buyer SKILL and
    # REALIZED performance, and fold in insider / analyst / technical cross-signals.
    enrich_stock_signals(stock_signals, trades, members, insiders, street, screener)
    # compact 6-month price sparkline per stock (for row + modal charts)
    scr_spark = {s["ticker"]: s.get("spark") for s in screener["stocks"]}
    for s in stock_signals:
        s["spark"] = scr_spark.get(s["ticker"]) or _sparkline(prices.get(s["ticker"]))

    history = update_history(overview, stock_signals)
    flow = build_flow(trades, stock_signals, history)
    if flow.get("has_history"):
        w = flow.get("w90") or {}
        print(f"  flow: {w.get('n_names', 0)} names filed in the last {w.get('days', 0)}d, "
              f"{len(w.get('building', []))} building / {len(w.get('fresh', []))} fresh / "
              f"{len(w.get('exiting', []))} exiting, {len(flow.get('rotation', []))} sectors",
              file=sys.stderr)
    trends = compute_trends(history, stock_signals)
    stock_history = build_stock_history(history, [s["ticker"] for s in stock_signals[:120]])

    # Slim the trades feed before shipping: image_url + bioguide were only needed
    # to build member profiles (which already carry them). Dropping them from all
    # ~9k trade rows trims the payload clients download with no loss of function.
    for t in trades:
        t.pop("image_url", None)
        t.pop("bioguide", None)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "congress": CONGRESS,
        "overview": overview,
        "performance": performance,
        "member_race": member_race,
        "unusual_activity": unusual,
        "insiders": insiders,
        "screener": screener,
        "conviction": conviction,
        "street": street,
        "trends": trends,
        "flow": flow,
        "stock_history": stock_history,
        "sectors": sector_summaries,
        "bills": bills,
        "trades": trades,
        "members": members,
        "lag_study": lag_study,
        "conviction_check": conviction_check,
        "stock_signals": stock_signals,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"))  # compact -- clients download this on every load
    appropriations = sum(1 for b in bills if b.get("is_appropriation"))
    key = sum(1 for b in bills if b.get("key_bill"))
    print(f"Wrote {OUTPUT_PATH}: {len(bills)} bills ({key} key, {appropriations} appropriations), "
          f"{len(trades)} trades, {len(members)} members, {len(stock_signals)} stock signals", file=sys.stderr)


if __name__ == "__main__":
    main()
