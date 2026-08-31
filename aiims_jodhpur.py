#!/usr/bin/env python3
"""
AIIMS Jodhpur — Tenders & Quotations scraper (Mongo + S3 pipeline)
====================================================================

Scrapes both listing pages on aiimsjodhpur.edu.in:
    - quotations.php?page=N&search=...
    - tenders.php?page=N&search=...

Both pages are plain server-rendered HTML (no JS/ASP.NET postbacks), so
pagination is a direct GET per page number — no session priming or
__EVENTTARGET dance needed.

Pipeline (mirrors the tender_bharo / GpVine scraper conventions):
  1. GET page 1 of a category, read the pagination <nav> to find the total
     page count.
  2. GET each page directly, parse rows generically from <thead>/<tbody>
     (works for quotations.php and tenders.php even if their column sets
     differ — columns are matched by header name, not fixed position).
  3. Each row gets a stable hash_id (md5 of category + reference/first
     column) and a generated TEB tracking number.
  4. hash_id has a unique index in Mongo, so re-running the script is
     idempotent: already-seen rows are detected and skipped — this IS
     the "only new postings since last run" mechanism.
  5. New rows have their linked documents (PDFs etc.) downloaded and
     uploaded to S3; the Mongo doc is updated with the resulting s3_path.
  6. EARLY-STOP OPTIMIZATION for scheduled/incremental runs: both listing
     pages show newest postings first (see the sample dates in
     quotations.php, most recent at the top). So once a whole page comes
     back 100% already-known, we assume everything after it is old too
     and stop paging — a cron run only has to touch page 1 (sometimes 2)
     instead of crawling all N pages every time. Use --full-scan to
     disable this and force a complete crawl (e.g. for the first backfill
     run, or if you ever suspect out-of-order insertion on the site).

Scheduling: this script has no built-in scheduler — run it periodically
with cron / systemd timer / Task Scheduler / a GitHub Actions cron
workflow. It's safe to invoke every N minutes since dedup + early-stop
make repeat runs cheap and idempotent. Example crontab (every 2 hours):

    0 */2 * * *  cd /path/to/scraper && /usr/bin/python3 aiims_scraper.py >> aiims_scraper.log 2>&1

Required environment variables (.env or real env):
    LOCAL_MONGO_URI        e.g. mongodb://localhost:27017
    MONGO_DB_NAME           default: tender_bharo
    MONGO_COLLECTION        default: aiims_jodhpur_notices
    S3_BUCKET_NAME
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION

Usage:
    python3 aiims_scraper.py                         # incremental run, both categories
    python3 aiims_scraper.py --full-scan              # full backfill (first run)
    python3 aiims_scraper.py --categories tenders
    python3 aiims_scraper.py --search vitamin
    python3 aiims_scraper.py --dry-run                # parse + dedup-preview only, no S3/Mongo writes
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode, urlparse

import boto3
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ══════════════════════════════════════════════════════════════════
# CONFIG / CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL = "https://aiimsjodhpur.edu.in/"

CATEGORY_PATHS = {
    "quotations": "quotations.php",
    "tenders": "tenders.php",
}

# candidate substrings (checked in this order) used to guess which
# normalized column holds the record's human reference number, since
# quotations.php uses "Quotation Reference" and tenders.php's own
# heading text is unconfirmed (site is robots-disallowed for automated
# fetch, so this is deliberately fuzzy rather than hard-coded).
REFERENCE_KEY_HINTS = ["reference", "ref_no", "ref", "tender_no", "no", "number", "id"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Deliberately NOT setting Accept-Encoding here. requests/urllib3
    # negotiates this automatically based on which decompressors are
    # actually installed (gzip/deflate always; brotli only if the
    # `brotli`/`brotlicffi` package is present). The site responds with
    # Brotli (content-encoding: br) — forcing "br" into this header when
    # the local install can't decode Brotli silently returns garbled
    # bytes as resp.text, which then parses to 0 rows with no error
    # raised. If you want Brotli explicitly available: pip install brotli
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

CONTENT_TYPE_MAP = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".zip": "application/zip",
}


# ══════════════════════════════════════════════════════════════════
# PURE HELPERS (no I/O — unit-testable without Mongo/S3/network)
# ══════════════════════════════════════════════════════════════════

def clean_text(value):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def normalize_key(header_text):
    """'Quotation Subject / Quotation Document' -> 'quotation_subject'
       'Remarks / Documents'                    -> 'remarks'
       'Start Date'                              -> 'start_date'
    """
    key = header_text.lower().split("/")[0]
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key or "field"


def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def parse_date(raw, context=""):
    if not raw:
        return None
    try:
        dt = dateutil_parser.parse(raw.strip(), dayfirst=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        logging.getLogger("aiims_jodhpur").warning(f"Failed to parse date '{raw}' [{context}]: {e}")
        return None


def get_total_pages(soup: BeautifulSoup) -> int:
    """Reads the Bootstrap pagination nav and returns the highest page
    number linked anywhere in it (handles the '...' ellipsis-truncated
    style AIIMS uses, e.g. 1 2 3 ... 11 Next)."""
    nav = soup.find("nav", attrs={"aria-label": re.compile("pagination", re.I)})
    if not nav:
        return 1

    page_numbers = []
    for a in nav.select("a.page-link"):
        href = a.get("href", "")
        match = re.search(r"[?&]page=(\d+)", href)
        if match:
            page_numbers.append(int(match.group(1)))
            continue
        text = clean_text(a.get_text())
        if text and text.isdigit():
            page_numbers.append(int(text))

    return max(page_numbers) if page_numbers else 1


def parse_listing_table(soup: BeautifulSoup, page_url: str) -> list:
    """Generic parser: reads <thead> to get column names, then walks each
    <tbody> row extracting cleaned text + any document links per cell.
    Works for quotations.php and tenders.php even if their column sets
    differ, since columns are matched by header name rather than a fixed
    index."""
    table = soup.select_one("div.table-responsive table") or soup.find("table")
    if table is None:
        return []

    header_cells = table.select("thead th")
    headers = [normalize_key(clean_text(th.get_text(" ")) or f"col_{i}") for i, th in enumerate(header_cells)]

    tbody = table.find("tbody")
    if tbody is None:
        return []

    records = []
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        record = {}
        for idx, td in enumerate(cells):
            key = headers[idx] if idx < len(headers) else f"col_{idx}"

            links = []
            for a in td.select("a[href]"):
                href = a.get("href")
                if not href or href.startswith("#"):
                    continue
                links.append({
                    "label": clean_text(a.get_text(" ")),
                    "url": urljoin(page_url, href),
                })

            text = clean_text(td.get_text(" "))
            record[key] = {"text": text, "documents": links} if links else text

        if any(v for v in record.values()):
            records.append(record)

    return records


def cell_text(value):
    """Record values are either a plain string or {"text":..,"documents":[..]}."""
    if isinstance(value, dict):
        return value.get("text")
    return value


def guess_reference(record: dict):
    """Best-effort pick of the record's human reference/ID field, trying
    known hint substrings first, falling back to the first non-empty
    column."""
    for hint in REFERENCE_KEY_HINTS:
        for key, value in record.items():
            if key.startswith("_"):
                continue
            if hint in key:
                text = cell_text(value)
                if text:
                    return text
    for key, value in record.items():
        if key.startswith("_"):
            continue
        text = cell_text(value)
        if text:
            return text
    return None


def extract_documents(record: dict) -> list:
    docs = []
    for key, value in record.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            for doc in value.get("documents", []):
                docs.append({**doc, "field": key, "type": "Tender_document"})
    return docs


def category_url(category: str, page: int, search: str) -> str:
    path = CATEGORY_PATHS[category]
    query = urlencode({"page": page, "search": search})
    return f"{BASE_URL}{path}?{query}"


# ══════════════════════════════════════════════════════════════════
# SCRAPER (I/O: HTTP, Mongo, S3)
# ══════════════════════════════════════════════════════════════════

class AiimsJodhpurScraper:
    LOGGER_NAME = "aiims_jodhpur"

    def __init__(self, dry_run: bool = False, dump_html_dir=None, show_fields: bool = False):
        self.logger = self._build_logger()
        self.dry_run = dry_run
        self.dump_html_dir = dump_html_dir
        self.show_fields = show_fields
        if self.dump_html_dir:
            os.makedirs(self.dump_html_dir, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        if self.dry_run:
            self.logger.info("DRY RUN: skipping Mongo/S3 connections — parse + dedup-preview only.")
            self.client = self.db = self.raw_col = self.meta_col = self.s3 = self.bucket = None
        else:
            self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
            self.db = self.client[os.getenv("MONGO_DB_NAME", "tender_bharo")]
            self.raw_col = self.db[os.getenv("MONGO_COLLECTION", "aiims_jodhpur_tenders")]
            self.meta_col = self.db["meta_data"]
            self.raw_col.create_index("hash_id", unique=True)

            self.bucket = os.getenv("S3_BUCKET_NAME")
            self.base_folder = "tender_documents/aiims_jodhpur"
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION"),
            )

    def _build_logger(self) -> logging.Logger:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        return logging.getLogger(self.LOGGER_NAME)

    # -----------------------------------------------------------------
    # HTTP
    # -----------------------------------------------------------------
    def _fetch(self, url: str, max_attempts: int = 4, dump_name=None) -> str:
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    resp.encoding = resp.encoding or "utf-8"
                    text = resp.text
                    if self.dump_html_dir and dump_name:
                        path = os.path.join(self.dump_html_dir, dump_name)
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(text)
                        self.logger.info(f"    (saved raw HTML -> {path})")
                    # Sanity check: a successful HTML fetch should contain
                    # basic markup. If it doesn't, the body likely came
                    # back compressed-but-undecoded (e.g. a Brotli
                    # mismatch) rather than actual HTML — surface that
                    # loudly instead of silently returning "0 rows" later.
                    if "<html" not in text.lower() and "<!doctype" not in text.lower():
                        self.logger.warning(
                            f"GET {url} returned HTTP 200 but the body doesn't look like HTML "
                            f"(len={len(text)}, first 80 chars={text[:80]!r}). This usually means "
                            f"a compression mismatch (e.g. server sent Brotli but it wasn't decoded) "
                            f"or a bot-block/challenge page. Retrying …"
                        )
                        if attempt < max_attempts:
                            time.sleep(random.uniform(1.5, 3.0) * attempt)
                            continue
                    return text
                if resp.status_code in RETRYABLE_STATUS:
                    self.logger.warning(f"GET {url} -> HTTP {resp.status_code} (attempt {attempt}/{max_attempts})")
                else:
                    resp.raise_for_status()
            except Exception as exc:
                last_exc = exc
                self.logger.warning(f"GET {url} raised {exc} (attempt {attempt}/{max_attempts})")
            if attempt < max_attempts:
                time.sleep(random.uniform(1.5, 3.0) * attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"GET {url} failed after {max_attempts} attempts (non-2xx response)")

    def _download_with_retry(self, url: str, referer: str, max_attempts: int = 3):
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.get(url, headers={"Referer": referer, "Accept": "*/*"}, timeout=60)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in RETRYABLE_STATUS:
                    self.logger.warning(f"Download {url} -> HTTP {resp.status_code} (attempt {attempt}/{max_attempts})")
                else:
                    resp.raise_for_status()
            except Exception as exc:
                last_exc = exc
                self.logger.warning(f"Download {url} raised {exc} (attempt {attempt}/{max_attempts})")
            if attempt < max_attempts:
                time.sleep(random.uniform(1.5, 3.0) * attempt)
        self.logger.warning(f"Download failed (after retries): {url}: {last_exc}")
        return None

    # -----------------------------------------------------------------
    # TEB tracking number (shared counter convention with other
    # tender_bharo scrapers, e.g. TEB/2026/G/00000123)
    # -----------------------------------------------------------------
    def _generate_teb_id(self) -> str:
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F",
                     7: "G", 8: "H", 9: "I", 10: "J", 11: "K", 12: "L"}
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # -----------------------------------------------------------------
    # S3 upload
    # -----------------------------------------------------------------
    def _upload_documents_to_s3(self, documents: list, folder: str, page_url: str) -> list:
        updated = []
        for doc in documents:
            url = doc.get("url")
            entry = {**doc, "s3_path": None, "uploaded_at": None}
            if not url:
                updated.append(entry)
                continue

            resp = self._download_with_retry(url, referer=page_url)
            if resp is None:
                updated.append(entry)
                time.sleep(random.uniform(0.3, 0.8))
                continue

            try:
                # prefer the actual filename from the document URL (e.g.
                # "QUOTATION 27(I)-2026-110726.pdf"); link label text like
                # "View Document" is not a real filename and is only used
                # as a last-resort fallback if the URL has no path segment.
                fname = os.path.basename(urlparse(url).path) or doc.get("label") or "file"
                fname = re.sub(r"[^\w\-. ]", "_", fname)
                ext = os.path.splitext(fname)[-1].lower()
                content_type = CONTENT_TYPE_MAP.get(ext, "application/octet-stream")
                key = f"{self.base_folder}/{folder}/{fname}"

                self.s3.put_object(Bucket=self.bucket, Key=key, Body=resp.content, ContentType=content_type)
                entry["s3_path"] = f"s3://{self.bucket}/{key}"
                entry["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"    S3 \u2713 {fname}")
            except Exception as exc:
                self.logger.error(f"    S3 error {url}: {exc}")

            updated.append(entry)
            time.sleep(random.uniform(0.5, 1.2))

        return updated

    # -----------------------------------------------------------------
    # Row processing
    # -----------------------------------------------------------------
    RESERVED_KEYS = {"hash_id", "teb_number", "category", "reference", "documents",
                      "source_url", "source", "etl_status", "created_at", "_id"}

    def _process_row(self, category: str, record: dict, page_url: str, row_num: int) -> bool:
        """Returns True if this was a NEW row (inserted), False if already known."""
        reference = guess_reference(record)
        dedupe_key = f"{category}:{reference or repr(sorted(record.items()))}"
        hash_id = generate_hash(dedupe_key)

        # flatten each parsed column straight onto the document (plain
        # text only — link/document info lives in the top-level
        # "documents" list instead, not nested per-field).
        flat_fields = {}
        for key, value in record.items():
            if key.startswith("_"):
                continue
            out_key = key
            if out_key in self.RESERVED_KEYS:
                out_key = f"col_{out_key}"
                self.logger.warning(
                    f"    column '{key}' collides with a reserved field name — stored as '{out_key}' instead"
                )
            flat_fields[out_key] = cell_text(value)

        if self.dry_run:
            self.logger.info(f"  [{row_num}] {reference} — (dry-run, dedup not checked)")
            if self.show_fields:
                self.logger.info(f"    parsed columns: {list(record.keys())}")
                self.logger.info(f"    flattened fields: {json.dumps(flat_fields, indent=2, ensure_ascii=False, default=str)}")
                self.logger.info(f"    documents: {json.dumps(extract_documents(record), indent=2, ensure_ascii=False, default=str)}")
            return True

        exists = self.raw_col.find_one({"hash_id": hash_id}, {"_id": 1}) is not None
        if exists:
            self.logger.info(f"  [{row_num}] {reference} — already known, skipping")
            return False

        self.logger.info(f"  [{row_num}] {reference} — NEW")

        documents = extract_documents(record)
        teb_no = self._generate_teb_id()

        payload = {
            "hash_id": hash_id,
            "teb_number": teb_no,
            "category": category,
            "reference": reference,
            **flat_fields,
            "documents": documents,
            "source_url": page_url,
            "source": "AIIMS Jodhpur",
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
        }

        try:
            result = self.raw_col.insert_one(payload)
        except DuplicateKeyError:
            self.logger.info(f"    \u26a0 Duplicate on insert race — skipping: {reference}")
            return False

        if documents:
            self.logger.info(f"    Uploading {len(documents)} document(s) to S3 …")
            safe_ref = re.sub(r"[^\w\-]", "_", reference or str(result.inserted_id))
            folder = f"{category}_{safe_ref}_{result.inserted_id}"
            updated_docs = self._upload_documents_to_s3(documents, folder, page_url)
            self.raw_col.update_one({"_id": result.inserted_id}, {"$set": {"documents": updated_docs}})

        self.logger.info(f"    \u2713 Inserted (TEB: {teb_no})")
        return True

    # -----------------------------------------------------------------
    # Category orchestration
    # -----------------------------------------------------------------
    def scrape_category(
        self,
        category: str,
        search: str = "",
        max_pages=None,
        full_scan: bool = False,
        delay=(0.6, 1.4),
        stop_after_n_empty_pages: int = 2,
    ) -> dict:
        self.logger.info("\u2550" * 60)
        self.logger.info(f"[{category}] starting scrape (search={search!r}, full_scan={full_scan})")

        first_url = category_url(category, 1, search)
        html = self._fetch(first_url, dump_name=f"{category}_page1.html")
        soup = BeautifulSoup(html, "lxml")

        total_pages = get_total_pages(soup)
        if max_pages:
            total_pages = min(total_pages, max_pages)
        self.logger.info(f"[{category}] detected {total_pages} page(s)")

        header_cells = soup.select("thead th")
        detected_columns = [normalize_key(clean_text(th.get_text(" ")) or f"col_{i}") for i, th in enumerate(header_cells)]
        self.logger.info(f"[{category}] detected columns: {detected_columns}")

        total_new, total_seen, row_num = 0, 0, 0
        consecutive_empty_pages = 0

        for page in range(1, total_pages + 1):
            if page == 1:
                page_soup, url = soup, first_url
            else:
                url = category_url(category, page, search)
                page_soup = BeautifulSoup(self._fetch(url, dump_name=f"{category}_page{page}.html"), "lxml")
                time.sleep(random.uniform(*delay))

            records = parse_listing_table(page_soup, url)
            if not records:
                table_found = page_soup.select_one("div.table-responsive table") or page_soup.find("table")
                title = page_soup.title.get_text(strip=True) if page_soup.title else None
                self.logger.info(
                    f"[{category}] page {page} returned 0 rows — stopping. "
                    f"(diagnostic: <table> found={bool(table_found)}, page <title>={title!r})"
                )
                break

            page_new = 0
            for record in records:
                row_num += 1
                try:
                    is_new = self._process_row(category, record, url, row_num)
                    if is_new:
                        page_new += 1
                        total_new += 1
                    else:
                        total_seen += 1
                except Exception as exc:
                    self.logger.error(f"  \u2717 Row failed [{row_num}]: {exc}")

            self.logger.info(f"[{category}] page {page}/{total_pages}: {page_new} new / {len(records)} rows")

            if page_new == 0:
                consecutive_empty_pages += 1
            else:
                consecutive_empty_pages = 0

            if not full_scan and not self.dry_run and consecutive_empty_pages >= stop_after_n_empty_pages:
                self.logger.info(
                    f"[{category}] {consecutive_empty_pages} consecutive page(s) with 0 new rows — "
                    f"assuming older pages are fully known, stopping early (use --full-scan to disable)."
                )
                break

        self.logger.info(f"[{category}] done — {total_new} new, {total_seen} already known")
        return {"category": category, "new": total_new, "seen": total_seen}

    def scrape(self, categories, search="", max_pages=None, full_scan=False, stop_after_n_empty_pages: int = 2):
        summary = []
        for category in categories:
            summary.append(self.scrape_category(
                category, search=search, max_pages=max_pages, full_scan=full_scan,
                stop_after_n_empty_pages=stop_after_n_empty_pages,
            ))
        return summary


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scrape AIIMS Jodhpur tenders & quotations into Mongo + S3")
    parser.add_argument("--categories", nargs="+", choices=list(CATEGORY_PATHS), default=list(CATEGORY_PATHS))
    parser.add_argument("--search", default="", help="Value passed through to the site's own ?search= filter")
    parser.add_argument("--max-pages", type=int, default=None, help="Cap pages per category (testing)")
    parser.add_argument("--full-scan", action="store_true",
                         help="Disable early-stop; crawl every detected page even once duplicates appear "
                              "(use for the first backfill run)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse pages and log what would happen; no Mongo/S3 writes, no dedup persisted")
    parser.add_argument("--dump-html", default=None,
                         help="Directory to save each fetched page's raw HTML into, for debugging parsing issues")
    parser.add_argument("--show-fields", action="store_true",
                         help="In dry-run, print the full parsed record (all columns) for every row, not just the reference")
    parser.add_argument("--stop-after-n-empty-pages", type=int, default=2,
                         help="Require this many consecutive fully-known pages before early-stopping "
                              "(default 2; guards against a single stray already-known row halting the whole run)")
    args = parser.parse_args()

    scraper = AiimsJodhpurScraper(dry_run=args.dry_run, dump_html_dir=args.dump_html, show_fields=args.show_fields)
    summary = scraper.scrape(
        args.categories, search=args.search, max_pages=args.max_pages, full_scan=args.full_scan,
        stop_after_n_empty_pages=args.stop_after_n_empty_pages,
    )

    scraper.logger.info("\u2550" * 60)
    for row in summary:
        scraper.logger.info(f"SUMMARY [{row['category']}]: {row['new']} new, {row['seen']} already known")


if __name__ == "__main__":
    main()