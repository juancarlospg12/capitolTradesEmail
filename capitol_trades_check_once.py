import os
import re
import json
import smtplib
from email.message import EmailMessage
from typing import List, Dict, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.capitoltrades.com"
TRADES_URL = f"{BASE_URL}/trades"
STATE_FILE = os.getenv("STATE_FILE", "seen_trade_ids.json")
PAGES_TO_SCAN = int(os.getenv("PAGES_TO_SCAN", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
BOOTSTRAP_SKIP_EXISTING = os.getenv("BOOTSTRAP_SKIP_EXISTING", "true").lower() in {"1", "true", "yes"}

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_TO = [x.strip() for x in os.getenv("SMTP_TO", "").split(",") if x.strip()]
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; CapitolTradesWatcher/1.0; +https://github.com/)"
)

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

TRADE_ID_RE = re.compile(r'href=["\'](?:https://www\.capitoltrades\.com)?/trades/(\d+)["\']')


def fetch_html(url: str) -> str:
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def extract_trade_ids_from_list_page(html: str) -> List[str]:
    ids = TRADE_ID_RE.findall(html)
    out, seen = [], set()
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def text_lines(soup: BeautifulSoup) -> List[str]:
    txt = soup.get_text("\n", strip=True)
    return [line.strip() for line in txt.splitlines() if line.strip()]


def get_prev_line(lines: List[str], label: str) -> Optional[str]:
    label_norm = label.strip().lower()
    for i, line in enumerate(lines):
        if line.strip().lower() == label_norm and i > 0:
            return lines[i - 1].strip()
    return None


def get_anchor_href_by_text(soup: BeautifulSoup, pattern: str) -> Optional[str]:
    rx = re.compile(pattern, re.I)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if rx.search(text or ""):
            return a["href"]
    return None


def parse_trade_detail(trade_id: str, html: str) -> Dict[str, Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    lines = text_lines(soup)
    all_text = "\n".join(lines)

    title_text = normalize_whitespace(soup.title.string) if (soup.title and soup.title.string) else (lines[0] if lines else "")

    headline_match = re.search(
        r"(?P<politician>.+?)\s+(?P<verb>bought|sold)\s+(?P<issuer>.+?)\s+\((?P<ticker>[^)]+)\)\s+on\s+(?P<traded>\d{4}-\d{2}-\d{2})",
        title_text,
        flags=re.I
    )

    politician = issuer = ticker = traded_date = action = None
    if headline_match:
        politician = headline_match.group("politician").strip()
        issuer = headline_match.group("issuer").strip()
        ticker = headline_match.group("ticker").strip()
        traded_date = headline_match.group("traded").strip()
        action = "buy" if headline_match.group("verb").lower() == "bought" else "sell"

    published_date = get_prev_line(lines, "Published")
    traded_date = traded_date or get_prev_line(lines, "Traded")
    filed_on = get_prev_line(lines, "Filed on")

    price = None
    m_price = re.search(r"\b([0-9][0-9,]*\.?[0-9]*)\s+Price\b", all_text, flags=re.I)
    if m_price:
        price = m_price.group(1)

    reporting_gap_days = None
    m_gap = re.search(r"\b(\d+)\s+days?\s+Reporting Gap\b", all_text, flags=re.I)
    if m_gap:
        reporting_gap_days = m_gap.group(1)

    shares = None
    m_shares = re.search(r"\b([0-9][0-9,]*)\s*-\s*([0-9][0-9,]*)\s+Shares\b", all_text, flags=re.I)
    if m_shares:
        shares = f"{m_shares.group(1)} - {m_shares.group(2)} Shares"

    size_range = None
    m_size = re.search(r"#\s*([0-9A-Za-z,\.\-\u2013\u2014 ]+)", all_text)
    if m_size:
        candidate = normalize_whitespace(m_size.group(1))
        if len(candidate) <= 30:
            size_range = candidate

    owner = None
    m_owner = re.search(r"\b(Undisclosed|Spouse|Joint|Self)\s+Owner\b", all_text, flags=re.I)
    if m_owner:
        owner = m_owner.group(1).title()

    original_filing_url = get_anchor_href_by_text(soup, r"View Original Filing")
    if original_filing_url and original_filing_url.startswith("/"):
        original_filing_url = urljoin(BASE_URL, original_filing_url)

    return {
        "trade_id": trade_id,
        "page_url": f"{BASE_URL}/trades/{trade_id}",
        "politician": politician,
        "issuer": issuer,
        "ticker": ticker,
        "action": action,
        "traded_date": traded_date,
        "published_date": published_date,
        "filed_on": filed_on,
        "reporting_gap_days": reporting_gap_days,
        "size_range": size_range,
        "price": price,
        "shares": shares,
        "owner": owner,
        "original_filing_url": original_filing_url,
        "title_text": title_text,
    }


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"bootstrapped": False, "seen_ids": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def collect_latest_trade_ids() -> List[str]:
    all_ids = []
    seen = set()
    for page in range(1, PAGES_TO_SCAN + 1):
        url = TRADES_URL if page == 1 else f"{TRADES_URL}?page={page}"
        html = fetch_html(url)
        ids = extract_trade_ids_from_list_page(html)
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                all_ids.append(tid)
    return all_ids


def send_email_for_trade(trade: Dict[str, Optional[str]]) -> None:
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        raise RuntimeError("Missing SMTP configuration.")

    subject = f"[CapitolTrades] New trade - {trade.get('politician') or 'Unknown'} {str(trade.get('action') or '').upper()} {trade.get('ticker') or ''}".strip()

    body = "\n".join([
        "New CapitolTrades trade detected",
        "",
        f"Trade ID: {trade.get('trade_id')}",
        f"Politician: {trade.get('politician') or 'N/A'}",
        f"Issuer: {trade.get('issuer') or 'N/A'}",
        f"Ticker: {trade.get('ticker') or 'N/A'}",
        f"Action: {trade.get('action') or 'N/A'}",
        f"Size: {trade.get('size_range') or 'N/A'}",
        f"Price: {trade.get('price') or 'N/A'}",
        f"Shares: {trade.get('shares') or 'N/A'}",
        f"Owner: {trade.get('owner') or 'N/A'}",
        f"Traded date: {trade.get('traded_date') or 'N/A'}",
        f"Published date: {trade.get('published_date') or 'N/A'}",
        f"Filed on: {trade.get('filed_on') or 'N/A'}",
        f"Reporting gap (days): {trade.get('reporting_gap_days') or 'N/A'}",
        "",
        f"Trade detail page: {trade.get('page_url')}",
        f"Original filing: {trade.get('original_filing_url') or 'N/A'}",
        "",
        f"Parsed title: {trade.get('title_text') or 'N/A'}",
    ])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(SMTP_TO)
    msg.set_content(body)

    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)


def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    current_ids = collect_latest_trade_ids()

    # First run: bootstrap without emailing old trades (default)
    if not state.get("bootstrapped", False):
        if BOOTSTRAP_SKIP_EXISTING:
            state["seen_ids"] = sorted(list(set(current_ids)), key=lambda x: int(x))
            state["bootstrapped"] = True
            save_state(state)
            print(f"Bootstrapped with {len(current_ids)} existing trades. No emails sent.")
            return
        else:
            state["bootstrapped"] = True
            save_state(state)

    new_ids = [tid for tid in current_ids if tid not in seen_ids]
    if not new_ids:
        print("No new trades.")
        return

    try:
        new_ids = sorted(new_ids, key=lambda x: int(x))
    except ValueError:
        pass

    print(f"Found {len(new_ids)} new trades: {new_ids}")

    for tid in new_ids:
        html = fetch_html(f"{BASE_URL}/trades/{tid}")
        trade = parse_trade_detail(tid, html)
        send_email_for_trade(trade)
        seen_ids.add(tid)
        print(f"Emailed trade {tid}")

    state["seen_ids"] = sorted(list(seen_ids), key=lambda x: int(x))
    state["bootstrapped"] = True
    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
