#!/usr/bin/env python3
"""
Scrape Colorado bills signed into law from the Governor's TFA page,
enrich each row with the long title and subjects scraped from
leg.colorado.gov, and write the result to a CSV.

Usage:
    python scrape_bills.py                   # writes co_bills_2026.csv
    python scrape_bills.py out.csv           # custom output path
    python scrape_bills.py --debug-html      # save raw TFA HTML for inspection
    python scrape_bills.py --html page.html  # parse a saved HTML file instead
                                             # (handy after --debug-html)

Requirements:
    pip install requests beautifulsoup4
    # Optional fallback for JS-rendered pages:
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

TFA_URL = "https://cogov.my.salesforce-sites.com/TFA"
COLEG_BASE = "https://leg.colorado.gov/bills/"

HEADERS = {
    "User-Agent": (
        "co-bills-scraper/3.0 "
        "(+https://leg.colorado.gov; research script)"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN = 1.0  # seconds between coleg.gov requests

# A bill ID can appear as "HB26-1011", "HB 26-1011", or "HB26 - 1011".
# This pattern accepts whitespace inside the id.
BILL_ID_RE = re.compile(r"\b([HS]B)\s*(\d{2})\s*-\s*(\d{3,4})\b")


# ---------------------------------------------------------------------------
# Step 1: load the TFA page (static-first, headless-browser fallback)
# ---------------------------------------------------------------------------

def fetch_tfa_html_static(session: requests.Session) -> str | None:
    try:
        resp = session.get(TFA_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"  ! static GET failed: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(
            f"  ! static GET returned HTTP {resp.status_code}",
            file=sys.stderr,
        )
        return None
    return resp.text


def fetch_tfa_html_headless() -> str | None:
    """Render the TFA page with headless Chromium via Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "\n! The TFA page appears to require JavaScript, but "
            "Playwright is not installed.\n"
            "  Install it with:\n"
            "      pip install playwright\n"
            "      playwright install chromium\n",
            file=sys.stderr,
        )
        return None

    print("  - falling back to headless Chromium (Playwright)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(TFA_URL, wait_until="networkidle", timeout=60_000)
            try:
                page.wait_for_function(
                    "/[HS]B\\s*\\d{2}\\s*-\\s*\\d{3,4}/.test("
                    "document.body.innerText)",
                    timeout=20_000,
                )
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


def looks_like_real_data(html: str) -> bool:
    """A simple gate: the page must mention at least one bill ID."""
    return bool(BILL_ID_RE.search(html or ""))


def normalize_bill_id(text: str) -> str | None:
    """Find a bill id anywhere in `text` and return canonical form."""
    m = BILL_ID_RE.search(text or "")
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}-{m.group(3)}"


# ---------------------------------------------------------------------------
# Step 2: parse the TFA page into a list of row dicts.
# Tries multiple strategies in order of specificity; the first one that
# produces results wins.
# ---------------------------------------------------------------------------

def parse_tfa(html: str) -> tuple[list[dict], str]:
    """
    Return (rows, strategy_name). The keys of each dict are taken from
    the page when possible, otherwise fall back to "Bill #" and
    heuristic column names.
    """
    soup = BeautifulSoup(html, "html.parser")

    for strategy in (
        _strategy_html_table,
        _strategy_repeated_blocks,
        _strategy_grid_cells,
    ):
        rows = strategy(soup)
        if rows:
            return rows, strategy.__name__

    return [], "none"


# --- Strategy A: real <table> ----------------------------------------------

def _strategy_html_table(soup: BeautifulSoup) -> list[dict]:
    target_table: Tag | None = None
    target_headers: list[str] = []

    for table in soup.find_all("table"):
        headers = _table_headers(table)
        if not headers:
            continue
        normalized = [
            h.strip().lower().replace("#", "number") for h in headers
        ]
        if any(
            ("bill" in h and ("number" in h or "no" in h or h == "bill"))
            for h in normalized
        ):
            target_table = table
            target_headers = headers
            break

    if target_table is None:
        # Largest table that contains bill-id text.
        candidates = []
        for table in soup.find_all("table"):
            txt = table.get_text(" ", strip=True)
            if BILL_ID_RE.search(txt):
                candidates.append((len(txt), table))
        if candidates:
            candidates.sort(reverse=True, key=lambda t: t[0])
            target_table = candidates[0][1]
            target_headers = _table_headers(target_table) or [
                "Bill #",
                "Bill Title",
                "House Sponsors",
                "Senate Sponsors",
                "Action Date",
                "Final Bill Action",
            ]

    if target_table is None:
        return []

    rows: list[dict] = []
    for tr in target_table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        values = [_clean(c.get_text(" ", strip=True)) for c in cells]
        if len(values) < len(target_headers):
            values += [""] * (len(target_headers) - len(values))
        elif len(values) > len(target_headers):
            values = values[: len(target_headers)]
        row = dict(zip(target_headers, values))

        bill_field = _find_bill_field(row)
        if not bill_field:
            continue
        bill_id = normalize_bill_id(row[bill_field])
        if not bill_id:
            continue
        row[bill_field] = bill_id
        rows.append(row)
    return rows


def _table_headers(table: Tag) -> list[str]:
    th_row = table.find("tr")
    if th_row is None:
        return []
    ths = th_row.find_all("th")
    if ths:
        return [_clean(th.get_text(" ", strip=True)) for th in ths]
    tds = th_row.find_all("td")
    if tds and not table.find("tbody"):
        return [_clean(td.get_text(" ", strip=True)) for td in tds]
    return []


# --- Strategy B: repeated block elements -----------------------------------

def _strategy_repeated_blocks(soup: BeautifulSoup) -> list[dict]:
    """
    Visualforce/Lightning pages often render a list of records as
    repeated <div>, <li>, <article>, or <tr>-without-table blocks
    that each contain one bill ID. Find the largest group of such
    sibling blocks under a common parent and parse each.
    """
    candidate_blocks: list[Tag] = []
    for tag in soup.find_all(
        ["div", "li", "article", "section", "tr"]
    ):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        ids = BILL_ID_RE.findall(text)
        if len(ids) == 1 and len(text) < 1000:
            candidate_blocks.append(tag)

    if not candidate_blocks:
        return []

    # Group by (parent identity, tag name, class signature). Largest
    # group is the row set.
    groups: dict[tuple, list[Tag]] = {}
    for block in candidate_blocks:
        parent = block.parent
        if parent is None:
            continue
        cls = " ".join(block.get("class", []))
        key = (id(parent), block.name, cls)
        groups.setdefault(key, []).append(block)

    if not groups:
        return []

    _, blocks = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(blocks) < 2:
        return []

    rows: list[dict] = []
    for block in blocks:
        row = _block_to_row(block)
        if row:
            rows.append(row)
    return rows


def _block_to_row(block: Tag) -> dict | None:
    text = block.get_text(" ", strip=True)
    bill_id = normalize_bill_id(text)
    if not bill_id:
        return None

    pairs = _read_dl_pairs(block)
    if not pairs:
        pairs = _read_th_td_pairs(block)
    if not pairs:
        pairs = _read_label_value_pairs(block)
    if not pairs:
        pairs = _read_columnar_chunks(block)

    if pairs:
        row = {k: v for k, v in pairs}
    else:
        row = {"raw": text}

    bill_field = _find_bill_field(row)
    if bill_field is None:
        row = {"Bill #": bill_id, **row}
    else:
        row[bill_field] = bill_id
    return row


def _read_dl_pairs(block: Tag) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for dl in block.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = _clean(dt.get_text(" ", strip=True)).rstrip(":")
            value = _clean(dd.get_text(" ", strip=True))
            if label:
                pairs.append((label, value))
    return pairs


def _read_th_td_pairs(block: Tag) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for tr in block.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            label = _clean(cells[0].get_text(" ", strip=True)).rstrip(":")
            value = _clean(cells[1].get_text(" ", strip=True))
            if label:
                pairs.append((label, value))
    return pairs


def _read_label_value_pairs(block: Tag) -> list[tuple[str, str]]:
    """Look for <span class="label">X:</span><span>Y</span> style markup."""
    pairs: list[tuple[str, str]] = []
    label_nodes = block.find_all(
        attrs={
            "class": re.compile(
                r"\b(label|fieldLabel|labelCol)\b", re.I
            )
        }
    )
    for ln in label_nodes:
        label = _clean(ln.get_text(" ", strip=True)).rstrip(":")
        value_node = ln.find_next_sibling()
        if value_node is None:
            continue
        value = _clean(value_node.get_text(" ", strip=True))
        if label:
            pairs.append((label, value))
    return pairs


def _read_columnar_chunks(block: Tag) -> list[tuple[str, str]]:
    """
    Last resort: each direct child element with text becomes one
    column. If that yields nothing, split the block's text on '|'
    (the TFA page is known to use pipes as in-cell separators).
    """
    chunks: list[str] = []
    for child in block.children:
        if isinstance(child, Tag):
            text = _clean(child.get_text(" ", strip=True))
            if text:
                chunks.append(text)
    if len(chunks) < 2:
        flat = _clean(block.get_text(" | ", strip=True))
        chunks = [c.strip() for c in flat.split("|") if c.strip()]
    if len(chunks) < 2:
        return []

    default_headers = [
        "Bill #",
        "Bill Title",
        "House Sponsors",
        "Senate Sponsors",
        "Action Date",
        "Final Bill Action",
    ]
    pairs: list[tuple[str, str]] = []
    for i, chunk in enumerate(chunks):
        key = (
            default_headers[i]
            if i < len(default_headers)
            else f"col_{i + 1}"
        )
        pairs.append((key, chunk))
    return pairs


# --- Strategy C: extract directly from raw text near each bill id ---------

def _strategy_grid_cells(soup: BeautifulSoup) -> list[dict]:
    """
    Final fallback: find every bill ID and capture a small text
    window around it. Better than nothing; leg.colorado.gov
    enrichment fills in title and subjects.
    """
    text = soup.get_text("\n", strip=True)
    rows: list[dict] = []
    seen: set[str] = set()
    for m in BILL_ID_RE.finditer(text):
        bill_id = f"{m.group(1)}{m.group(2)}-{m.group(3)}"
        if bill_id in seen:
            continue
        seen.add(bill_id)
        window = text[m.end(): m.end() + 200].split("\n")
        nearby = " | ".join(s for s in (w.strip() for w in window) if s)
        rows.append({"Bill #": bill_id, "Context": nearby[:300]})
    return rows


# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _find_bill_field(row: dict) -> str | None:
    for key in row.keys():
        kl = key.lower()
        if "bill" in kl and (
            "#" in key or "no" in kl or kl.strip() == "bill"
        ):
            return key
    for key, val in row.items():
        if isinstance(val, str) and BILL_ID_RE.search(val):
            return key
    return None


# ---------------------------------------------------------------------------
# Step 3: enrich each bill from leg.colorado.gov
# ---------------------------------------------------------------------------

def fetch_bill_page(session: requests.Session, bill_id: str) -> str | None:
    url = urljoin(COLEG_BASE, bill_id)
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"  ! network error for {bill_id}: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(
            f"  ! HTTP {resp.status_code} for {bill_id} ({url})",
            file=sys.stderr,
        )
        return None
    return resp.text


def extract_long_title(soup: BeautifulSoup) -> str:
    el = soup.find(class_="bill-long-title")
    if el and el.get_text(strip=True):
        return _clean(el.get_text(" ", strip=True))
    for tag in soup.find_all(["p", "div"]):
        text = tag.get_text(" ", strip=True)
        if text.startswith("Concerning ") and len(text) < 800:
            return _clean(text)
    return ""


def extract_subjects(soup: BeautifulSoup) -> str:
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip(":").lower()
        if label != "subjects":
            continue
        value_cell = cells[1]
        items: list[str] = []
        for a in value_cell.find_all("a"):
            t = a.get_text(strip=True)
            if t:
                items.append(t)
        if not items:
            for li in value_cell.find_all("li"):
                t = li.get_text(strip=True)
                if t:
                    items.append(t)
        if not items:
            raw = value_cell.get_text(" ", strip=True)
            if raw:
                for chunk in raw.replace("\n", ",").split(","):
                    chunk = chunk.strip()
                    if chunk:
                        items.append(chunk)
        seen, unique = set(), []
        for it in items:
            k = it.lower()
            if k not in seen:
                seen.add(k)
                unique.append(it)
        return "; ".join(unique)
    return ""


def load_existing_enrichment(path: Path) -> dict[str, dict[str, str]]:
    """
    Read a previous run's CSV (if present) and return a mapping of
    Bill # -> {"Long Title": ..., "Subjects": ...} for any rows that
    have at least a long title. Bills without a long title aren't
    cached so they get retried next run.
    """
    if not path.exists():
        return {}
    cache: dict[str, dict[str, str]] = {}
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bill_id = (row.get("Bill #") or "").strip()
                long_title = (row.get("Long Title") or "").strip()
                subjects = (row.get("Subjects") or "").strip()
                if bill_id and long_title:
                    cache[bill_id] = {
                        "Long Title": long_title,
                        "Subjects": subjects,
                    }
    except Exception as exc:  # malformed file shouldn't kill the run
        print(
            f"  ! could not read existing CSV at {path}: {exc} "
            f"(proceeding without cache)",
            file=sys.stderr,
        )
        return {}
    return cache


def enrich_rows(
    rows: list[dict],
    bill_field: str,
    cache: dict[str, dict[str, str]],
) -> Iterable[dict]:
    session = requests.Session()
    total = len(rows)
    cached_hits = 0
    for i, row in enumerate(rows, start=1):
        bill_id = (row.get(bill_field) or "").strip()

        # Use the cached enrichment if we already have it. Bill summaries
        # on coleg.gov are stable once a bill is signed, so a single
        # successful fetch is good for the life of this dataset.
        if bill_id in cache:
            row["Long Title"] = cache[bill_id]["Long Title"]
            row["Subjects"] = cache[bill_id]["Subjects"]
            cached_hits += 1
            print(f"[{i}/{total}] {bill_id} ... cached")
            yield row
            continue

        print(f"[{i}/{total}] {bill_id} ...", end=" ", flush=True)

        long_title, subjects = "", ""
        html = fetch_bill_page(session, bill_id)
        if html is not None:
            soup = BeautifulSoup(html, "html.parser")
            long_title = extract_long_title(soup)
            subjects = extract_subjects(soup)

        row["Long Title"] = long_title
        row["Subjects"] = subjects

        bits = []
        bits.append("title OK" if long_title else "title MISSING")
        if subjects:
            bits.append(f"subjects: {subjects[:60]}")
        else:
            bits.append("subjects MISSING")
        print(" | ".join(bits))

        # Polite pause only between live fetches.
        time.sleep(SLEEP_BETWEEN)
        yield row

    if cached_hits:
        print(
            f"  - skipped {cached_hits} bill(s) already enriched in "
            f"the existing CSV"
        )


# ---------------------------------------------------------------------------
# Step 4: glue
# ---------------------------------------------------------------------------

def load_html(args, session: requests.Session) -> str | None:
    if args.html:
        path = Path(args.html)
        if not path.exists():
            sys.exit(f"--html file not found: {path}")
        print(f"reading saved HTML from {path}")
        return path.read_text(encoding="utf-8", errors="replace")

    print(f"fetching {TFA_URL} ...")
    html = fetch_tfa_html_static(session)

    if args.debug_html and html:
        debug_path = Path("tfa_debug.html")
        debug_path.write_text(html, encoding="utf-8")
        print(f"  - saved raw static HTML to {debug_path}")

    if not html or not looks_like_real_data(html):
        print(
            "  - no bill data in static HTML; page is likely "
            "JavaScript-rendered."
        )
        html = fetch_tfa_html_headless()
        if args.debug_html and html:
            debug_path = Path("tfa_debug_rendered.html")
            debug_path.write_text(html, encoding="utf-8")
            print(f"  - saved rendered HTML to {debug_path}")

    if not html or not looks_like_real_data(html):
        sys.exit(
            "could not retrieve bill data from the TFA page. Pass "
            "--debug-html to save what we did fetch for inspection."
        )
    return html


def run(output_path: Path, args) -> int:
    session = requests.Session()
    html = load_html(args, session)

    print("  - parsing ...")
    rows, strategy = parse_tfa(html)
    print(f"  - parser strategy used: {strategy}")
    if not rows:
        debug_path = Path("tfa_debug.html")
        debug_path.write_text(html, encoding="utf-8")
        print(
            f"no bill rows extracted. The raw HTML was saved to "
            f"{debug_path} so you can inspect (or send) the structure.",
            file=sys.stderr,
        )
        return 1
    print(f"  - found {len(rows)} bills")

    bill_field = _find_bill_field(rows[0]) or "Bill #"

    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    fieldnames += ["Long Title", "Subjects"]

    # Load any prior run's enrichment so we don't re-fetch bills whose
    # summaries we already have.
    cache = load_existing_enrichment(output_path)
    if cache:
        print(f"  - cache: {len(cache)} bill(s) already enriched")

    print(f"\nenriching from {COLEG_BASE} ...")
    enriched = list(enrich_rows(rows, bill_field, cache))

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(enriched)

    # Surface partial failures so a scheduled run can be flagged. Each
    # missing field is counted separately; a bill could be missing one
    # but not the other.
    missing_titles = [r for r in enriched if not r.get("Long Title")]
    missing_subjects = [r for r in enriched if not r.get("Subjects")]
    print(f"\nwrote {len(enriched)} rows to {output_path}")

    if missing_titles or missing_subjects:
        if missing_titles:
            ids = ", ".join(r.get(bill_field, "?") for r in missing_titles[:8])
            extra = "" if len(missing_titles) <= 8 else f" (+{len(missing_titles) - 8} more)"
            print(
                f"  ! {len(missing_titles)} bill(s) missing long title: "
                f"{ids}{extra}",
                file=sys.stderr,
            )
        if missing_subjects:
            ids = ", ".join(r.get(bill_field, "?") for r in missing_subjects[:8])
            extra = "" if len(missing_subjects) <= 8 else f" (+{len(missing_subjects) - 8} more)"
            print(
                f"  ! {len(missing_subjects)} bill(s) missing subjects: "
                f"{ids}{extra}",
                file=sys.stderr,
            )
        return 2  # partial failure

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape Colorado bills signed into law from the Governor's "
            "TFA page and enrich each with long title and subjects "
            "from leg.colorado.gov."
        )
    )
    parser.add_argument(
        "output_csv",
        type=Path,
        nargs="?",
        default=Path("co_bills_2026.csv"),
        help="Path to output CSV (default: co_bills_2026.csv)",
    )
    parser.add_argument(
        "--debug-html",
        action="store_true",
        help=(
            "Save the fetched TFA HTML to tfa_debug.html for inspection."
        ),
    )
    parser.add_argument(
        "--html",
        type=str,
        default=None,
        help=(
            "Skip the TFA fetch and parse a saved HTML file instead "
            "(useful for offline debugging)."
        ),
    )
    args = parser.parse_args()
    sys.exit(run(args.output_csv, args))


if __name__ == "__main__":
    main()