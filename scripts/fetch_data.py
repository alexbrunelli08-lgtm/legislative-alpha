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

import io
import json
import os
import re
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

CONGRESS = 119  # 119th Congress: 2025-2027
BILLS_LOOKBACK_DAYS = 45          # only scan bills updated in this window
MAX_MATCHED_BILLS = 60            # cap how many matched bills we keep
SENATE_TRADES_LOOKBACK_DAYS = 45  # PTR filings to scan for tracked tickers
REQUEST_TIMEOUT = 20
USER_AGENT = "legislative-alpha-tracker/1.0 (personal project; contact via github repo)"

CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
IMPACT_MODEL = "claude-opus-4-8"


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


def fetch_bills(sectors):
    print("Fetching recently updated bills from Congress.gov...", file=sys.stderr)
    from_dt = (datetime.now(timezone.utc) - timedelta(days=BILLS_LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    matched = []
    offset = 0
    page_size = 250
    max_pages = 4  # scan up to 1000 recently-updated bills
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
            sector = match_sector(title, sectors)
            if not sector:
                continue
            bill_type = b.get("type", "").upper()
            number = b.get("number")
            latest_action = b.get("latestAction") or {}
            record = {
                "id": f"{CONGRESS}-{bill_type}-{number}",
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
            matched.append(record)
            if len(matched) >= MAX_MATCHED_BILLS:
                break
        if len(matched) >= MAX_MATCHED_BILLS:
            break
        offset += page_size
        time.sleep(0.2)

    print(f"  matched {len(matched)} bills to a sector", file=sys.stderr)
    return matched


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


def fetch_senate_trades(ticker_index):
    print("Scraping Senate periodic transaction reports (efdsearch.senate.gov)...", file=sys.stderr)
    try:
        session, csrf_token = efd_open_session()
    except (requests.RequestException, RuntimeError) as e:
        print(f"  WARN: could not open eFD session, skipping trades: {e}", file=sys.stderr)
        return []

    end_date = datetime.now()
    start_date = end_date - timedelta(days=SENATE_TRADES_LOOKBACK_DAYS)
    try:
        reports = search_ptr_reports(session, csrf_token, start_date, end_date)
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN: PTR search failed, skipping trades: {e}", file=sys.stderr)
        return []

    print(f"  found {len(reports)} PTR filings in the last {SENATE_TRADES_LOOKBACK_DAYS} days, scanning for tracked tickers...", file=sys.stderr)
    trades = []
    seen = set()  # dedupe: an amendment report restates the original's transactions
    for report in reports:
        try:
            transactions = fetch_ptr_transactions(session, report["report_path"])
        except requests.RequestException:
            continue
        for tx in transactions:
            # Every disclosed trade is kept. Trades in a tracked constituent
            # are attributed to that constituent's sector(s); everything else
            # (including non-ticker assets like bonds and funds) goes in the
            # OTHER bucket so nothing is silently dropped.
            matches = (ticker_index.get(tx["ticker"]) if tx["ticker"] else None) or [
                {"sector": "OTHER", "company": tx["asset_name"]}
            ]
            for info in matches:
                dedupe_key = (report["senator"], tx["ticker"], tx["asset_name"], tx["transaction_date"], tx["type"], tx["amount_range"], info["sector"])
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                trades.append({
                    "sector": info["sector"],
                    "ticker": tx["ticker"],
                    "company": info["company"],
                    "member": f"Sen. {report['senator']}",
                    "chamber": "Senate",
                    "transaction_date": tx["transaction_date"],
                    "filed_date": report["filed_date"],
                    "type": tx["type"],
                    "amount_range": tx["amount_range"],
                    "report_url": f"{EFD_BASE}{report['report_path']}",
                })
        time.sleep(0.25)

    matched = sum(1 for t in trades if t["sector"] != "OTHER")
    print(f"  {len(trades)} trades captured ({matched} matched a tracked sector)", file=sys.stderr)
    return trades


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


def fetch_house_trades(ticker_index):
    print("Fetching House periodic transaction reports (disclosures-clerk.house.gov)...", file=sys.stderr)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=SENATE_TRADES_LOOKBACK_DAYS)
    years = sorted({start_date.year, end_date.year})

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

    print(f"  found {len(in_window)} House PTR filings in the last {SENATE_TRADES_LOOKBACK_DAYS} days, parsing PDFs...", file=sys.stderr)
    trades = []
    seen = set()
    skipped_paper = 0
    for report in in_window:
        pdf_url = f"{HOUSE_BASE}/ptr-pdfs/{report['year']}/{report['doc_id']}.pdf"
        try:
            resp = requests.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  WARN: could not download House PTR {report['doc_id']}: {e}", file=sys.stderr)
            continue
        transactions = parse_house_ptr_pdf(resp.content)
        if transactions is None:
            skipped_paper += 1
            continue
        member = f"Rep. {report['name']} ({report['state_district']})"
        for tx in transactions:
            matches = (ticker_index.get(tx["ticker"]) if tx["ticker"] else None) or [
                {"sector": "OTHER", "company": tx["asset_name"]}
            ]
            for info in matches:
                dedupe_key = (member, tx["ticker"], tx["asset_name"], tx["transaction_date"], tx["type"], tx["amount_range"], info["sector"])
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                trades.append({
                    "sector": info["sector"],
                    "ticker": tx["ticker"],
                    "company": info["company"],
                    "member": member,
                    "chamber": "House",
                    "transaction_date": tx["transaction_date"],
                    "filed_date": report["filed_date"],
                    "type": tx["type"],
                    "amount_range": tx["amount_range"],
                    "report_url": pdf_url,
                })
        time.sleep(0.3)

    matched = sum(1 for t in trades if t["sector"] != "OTHER")
    print(f"  {len(trades)} House trades captured ({matched} matched a tracked sector; {skipped_paper} paper filings skipped)", file=sys.stderr)
    return trades


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
    ]))
    bill["is_appropriation"] = bool(APPROPRIATION_TERMS.search(blob))
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
        })
        p["trade_count"] += 1
        val = t.get("est_amount", 0)
        if _is_buy(t["type"]):
            p["buy_count"] += 1
            p["buy_value"] += val
        else:
            p["sell_count"] += 1
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
        })
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
            "company": s["company"],
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


def build_sector_summaries(sectors, bills, trades=()):
    summaries = {}
    for code, sector in sectors.items():
        sector_bills = [b for b in bills if b["sector"] == code]
        avg_momentum = round(sum(b["momentum"] for b in sector_bills) / len(sector_bills)) if sector_bills else 0
        summaries[code] = {
            "name": sector["name"],
            "short": sector.get("short", sector["name"]),
            "etf": sector["etf"],
            "color": sector["color"],
            "bill_count": len(sector_bills),
            "appropriation_count": sum(1 for b in sector_bills if b.get("is_appropriation")),
            "trade_count": sum(1 for t in trades if t["sector"] == code),
            "stock_count": len(sector.get("constituents", {})),
            "avg_momentum": avg_momentum,
        }
    other_trades = sum(1 for t in trades if t["sector"] == "OTHER")
    if other_trades:
        summaries["OTHER"] = {
            "name": "Other / Unclassified",
            "short": "Other",
            "etf": None,
            "color": "#565F73",
            "bill_count": 0,
            "appropriation_count": 0,
            "trade_count": other_trades,
            "stock_count": 0,
            "avg_momentum": 0,
        }
    return summaries


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
    trades = fetch_senate_trades(ticker_index) + fetch_house_trades(ticker_index)
    trades = annotate_trade_values(trades)
    trades = annotate_trade_parties(trades, member_index)

    bills = [analyze_appropriation(b) for b in bills]
    bills = attach_beneficiary_stocks(bills, sectors)
    bills = attach_impact_analysis(bills)
    bills = flag_pre_filing_trades(bills, trades)
    bills = attach_bill_trades(bills, trades)
    bills = mark_key_bills(bills)
    sector_summaries = build_sector_summaries(sectors, bills, trades)
    members = build_member_profiles(trades)
    stock_signals = build_stock_signals(trades)
    overview = build_overview(bills, trades, members, stock_signals)
    unusual = build_unusual_activity(stock_signals)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "congress": CONGRESS,
        "overview": overview,
        "unusual_activity": unusual,
        "sectors": sector_summaries,
        "bills": bills,
        "trades": trades,
        "members": members,
        "stock_signals": stock_signals,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    appropriations = sum(1 for b in bills if b.get("is_appropriation"))
    key = sum(1 for b in bills if b.get("key_bill"))
    print(f"Wrote {OUTPUT_PATH}: {len(bills)} bills ({key} key, {appropriations} appropriations), "
          f"{len(trades)} trades, {len(members)} members, {len(stock_signals)} stock signals", file=sys.stderr)


if __name__ == "__main__":
    main()
