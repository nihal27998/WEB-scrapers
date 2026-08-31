

import os
import re
import time
import random
import hashlib
import logging
import requests

from datetime import datetime, timezone
from dateutil import parser as dateparser
from urllib.parse import urlparse

import boto3
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

load_dotenv()


class DCOCPContractsScraper:

    BASE_URL     = "https://contracts.ocp.dc.gov"
    SEARCH_API   = f"{BASE_URL}/api/contracts/search"
    DETAIL_API   = f"{BASE_URL}/api/contracts/details"
    DOC_DOWNLOAD = f"{BASE_URL}/api/filedownload"

    # Fiscal years to scrape: 2022 → 2027 (fixed range)
    # Override via CLI: --years 2024 2025 2026
    FISCAL_YEARS = list(range(2022, 2028))   # [2022, 2023, 2024, 2025, 2026, 2027]

    # ─────────────────────────────────────────────────────────────────────
    # INIT
    # ─────────────────────────────────────────────────────────────────────
    def __init__(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("DC_OCP_CONTRACTS")

        # ── Requests session with retry ──────────────────────────────────
        self.SESSION = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 502, 503, 504])
        self.SESSION.mount("https://", HTTPAdapter(max_retries=retries))
        self.SESSION.headers.update({
            "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/149.0.0.0 Safari/537.36",
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "en-US,en;q=0.9",
            "Accept-Encoding":    "gzip, deflate, br, zstd",
            "Content-Type":       "application/json",
            "Origin":             self.BASE_URL,
            "Referer":            f"{self.BASE_URL}/contracts/results",
            "sec-ch-ua":          '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest":     "empty",
            "sec-fetch-mode":     "cors",
            "sec-fetch-site":     "same-origin",
        })

        # Fiscal years — starts as class constant, can be overridden via CLI
        self.fiscal_years = list(self.FISCAL_YEARS)

        # ── Playwright warm-up (CF clearance) ────────────────────────────
        self._warm_up_with_playwright()

        # ── MongoDB ──────────────────────────────────────────────────────
        mongo_uri            = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client          = MongoClient(mongo_uri)
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["dc_ocp_awards"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)
        self.raw_collection.create_index("contract_number")

        # ── S3 ───────────────────────────────────────────────────────────
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/dc_ocp_contracts"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name           = os.getenv("AWS_REGION"),
        )

    # ─────────────────────────────────────────────────────────────────────
    # PLAYWRIGHT WARM-UP
    # Visits the contracts search page, collects CF cookies,
    # and auto-detects available fiscal years from the UI checkboxes.
    # ─────────────────────────────────────────────────────────────────────
    def _warm_up_with_playwright(self):
        self.logger.info("Warming up session via Playwright (headless Chrome)…")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/149.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                    timezone_id="America/New_York",
                )
                page = context.new_page()

                # Step 1: hit base URL to get CF clearance cookie
                self.logger.info("  → [1/2] BASE URL (CF clearance)…")
                page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(4)

                # Step 2: navigate to the contracts search page so CF recognises
                # the path and sets any additional cookies
                self.logger.info("  → [2/2] /contracts/search…")
                page.goto(
                    f"{self.BASE_URL}/contracts/search",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                time.sleep(5)

                browser_cookies = context.cookies()
                browser.close()

            # Transfer all browser cookies into the requests session
            for c in browser_cookies:
                self.SESSION.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"),
                )

            names = [c["name"] for c in browser_cookies]
            cf_ok = any(c["name"] == "cf_clearance" for c in browser_cookies)
            self.logger.info(f"  → Cookies acquired: {names}")
            self.logger.info(f"  → cf_clearance: {'✓' if cf_ok else '⚠ NOT FOUND'}")
            self.logger.info("Warm-up complete ✓")

        except ImportError:
            self.logger.error(
                "playwright not installed — run:\n"
                "  pip install playwright && playwright install chromium"
            )
            raise
        except Exception as e:
            self.logger.error(f"Playwright warm-up failed: {e}")
            raise

    # ─────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────
    def generate_teb_id(self) -> str:
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_contracts_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1:"A", 2:"B", 3:"C", 4:"D", 5:"E", 6:"F",
            7:"G", 8:"H", 9:"I", 10:"J", 11:"K", 12:"L",
        }
        return f"TEBC/{now.year}/{month_map[now.month]}/{seq:08d}"

    def generate_hash(self, contract_id: str) -> str:
        """Hash on the contract's API `id` field (unique per contract + option period)."""
        return hashlib.md5(str(contract_id).encode()).hexdigest()

    def parse_date(self, date_val):
        try:
            if not date_val:
                return None
            dt = dateparser.parse(str(date_val).strip(), dayfirst=False)
            return dt.replace(tzinfo=timezone.utc) if dt else None
        except Exception as e:
            self.logger.error(f"Date parse failed: {date_val} | {e}")
            return None

    def parse_amount(self, amount_val):
        """Convert '$900,000.00' -> 900000  (integer, USD cents dropped)."""
        try:
            if not amount_val:
                return None
            cleaned = re.sub(r'[^\d.]', '', str(amount_val))
            return int(float(cleaned)) if cleaned else None
        except Exception as e:
            self.logger.error(f"Amount parse failed: {amount_val} | {e}")
            return None


    # ─────────────────────────────────────────────────────────────────────
    # SEARCH API  —  POST /api/contracts/search
    #
    # Payload structure (reverse-engineered from your browser capture):
    # {
    #   "FilterBy": [
    #     {"id": 0, "name": "FiscalYear", "value": [2022, 2023, ...]}
    #   ],
    #   "OrderBy": [],
    #   "Page": 1,
    #   "Size": 100
    # }
    # ─────────────────────────────────────────────────────────────────────
    def fetch_search_page(self, fiscal_years: list, page: int = 1, size: int = 100) -> dict:
        payload = {
            "FilterBy": [
                {
                    "id":    0,
                    "name":  "FiscalYear",
                    "value": fiscal_years,          # list of ints e.g. [2022, 2023]
                }
            ],
            "OrderBy": [],
            "Page":    page,
            "Size":    size,
        }
        self.logger.debug(f"POST {self.SEARCH_API} | years={fiscal_years} | page={page}")
        resp = self.SESSION.post(self.SEARCH_API, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorMessage"):
            self.logger.warning(f"API error page {page}: {data['errorMessage']}")
        return data

    # ─────────────────────────────────────────────────────────────────────
    # DETAIL API  —  GET /api/contracts/details?id=<id>
    # ─────────────────────────────────────────────────────────────────────
    # Tracks whether we have already logged the raw detail keys (once is enough)
    _detail_keys_logged = False

    def fetch_detail(self, contract_id: str) -> dict:
        try:
            resp = self.SESSION.get(
                self.DETAIL_API,
                params={"id": contract_id},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Log all top-level keys on the very first detail call so we can
            # confirm the exact phone field name returned by the API
            if not DCOCPContractsScraper._detail_keys_logged:
                phone_keys = [k for k in data.keys() if "phone" in k.lower()
                              or "tel" in k.lower() or "mobile" in k.lower()]
                self.logger.info(f"  [detail keys — first call] all keys: {list(data.keys())}")
                self.logger.info(f"  [detail keys — phone-like] {phone_keys}")
                DCOCPContractsScraper._detail_keys_logged = True

            return data
        except Exception as e:
            self.logger.error(f"Detail fetch failed for id={contract_id}: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # DOCUMENT PARSING
    # ─────────────────────────────────────────────────────────────────────
    def _parse_documents(self, detail: dict) -> list:
        docs = []
        for doc in detail.get("documents", []):
            doc_id = doc.get("id", "")
            if not doc_id:
                continue
            download_url = f"{self.DOC_DOWNLOAD}?id={doc_id}"
            docs.append({
                "type":         "contract_document",
                "title":        doc.get("name", ""),
                "doc_id":       doc_id,
                "original_url": download_url,
                "s3_path":      None,
                "uploaded_at":  None,
            })
        return docs

    # ─────────────────────────────────────────────────────────────────────
    # BUILD MONGODB PAYLOAD
    # Merges search-result row + detail response into one clean document.
    # ─────────────────────────────────────────────────────────────────────
    def build_payload(
        self,
        row: dict,
        detail: dict,
        teb_no: str,
        hash_id: str,
        fiscal_year: int,
        documents: list,
    ) -> dict:
        contract_id     = row.get("id", "")
        contract_number = detail.get("contractNumber") or row.get("contractNumber", "")
        detail_url      = f"{self.BASE_URL}/contracts/details?id={contract_id}"

        # Commodity codes: detail has expanded strings, row has raw codes
        commodity_codes_raw      = row.get("commodityCodes", [])
        commodity_codes_expanded = detail.get("commodityCodes", [])

        return {
            # ── Identity ────────────────────────────────────────────────
            "hash_id":           hash_id,
            "teb_number":        teb_no,
            "record_type":       "contract",
            "source":            "DC OCP Contracts",

            # ── Contract identifiers ────────────────────────────────────
            "contract_id":       contract_id,           # base64 API id (unique per option period)
            "contract_number":   contract_number,
            "fiscal_year":       fiscal_year,

            # ── Core fields ─────────────────────────────────────────────
            "title":             detail.get("title")              or row.get("title"),
            "vendor":            detail.get("vendor")             or row.get("vendor"),
            "contract_amount":          detail.get("contractAmount")     or row.get("contractAmount"),
            "contract_amount_original":  self.parse_amount(detail.get("contractAmount") or row.get("contractAmount")),
            "contract_type":     detail.get("contractType"),       # e.g. "Indefinite Delivery Indefinite Quantity"
            "market_type":       detail.get("marketType")         or row.get("marketType"),
            "current_option_period": detail.get("currentOptionPeriod") or row.get("currentOptionPeriod"),

            # ── Agencies — joined to a single string, nulls filtered out ──
            "agency_names":      ", ".join(
                                     a for a in (detail.get("agencyNames") or row.get("agencyNames") or [])
                                     if a
                                 ) or None,

            # ── Dates ───────────────────────────────────────────────────
            "start_date":        self.parse_date(detail.get("startDate")  or row.get("startDate")),
            "end_date":          self.parse_date(detail.get("endDate")    or row.get("endDate")),
            "award_date":        self.parse_date(detail.get("awardDate")  or row.get("awardDate")),
            "start_date_raw":    detail.get("startDate")  or row.get("startDate"),
            "end_date_raw":      detail.get("endDate")    or row.get("endDate"),
            "award_date_raw":    detail.get("awardDate")  or row.get("awardDate"),

            # ── Commodity codes ─────────────────────────────────────────
            "commodity_codes":          commodity_codes_raw,
            "commodity_codes_expanded": commodity_codes_expanded,

            # ── Contracting address — "Name, email, phone" strings ─────
            "contracting_officer":   ", ".join(filter(None, [
                                         detail.get("contractingOfficerName"),
                                         detail.get("contractingOfficerEmail"),
                                         detail.get("contractingOfficerPhone"),
                                     ])) or None,
            "contracting_specialist": ", ".join(filter(None, [
                                         detail.get("contractingSpecialistName"),
                                         detail.get("contractingSpecialistEmail"),
                                         detail.get("contractingSpecialistPhone"),
                                     ])) or None,

            # ── Vendor address ──────────────────────────────────────────
            "vendor_street":     detail.get("vendorStreet"),
            "vendor_city":       detail.get("vendorCity"),
            "vendor_state":      detail.get("vendorState"),
            "vendor_zip":        detail.get("vendorZip"),

            # ── Documents ───────────────────────────────────────────────
            "documents":         documents,

            # ── URLs ────────────────────────────────────────────────────
            "detail_url":        detail_url,
            "source_url":        self.SEARCH_API,

            # ── ETL metadata ─────────────────────────────────────────────
            "etl_status":        "pending",
            "created_at":        datetime.now(timezone.utc),
            "updated_at":        datetime.now(timezone.utc),
        }

    # ─────────────────────────────────────────────────────────────────────
    # S3 UPLOAD
    # ─────────────────────────────────────────────────────────────────────
    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        folder       = teb_number.replace("/", "_")
        updated_docs = []

        for d in documents:
            url = d.get("original_url")
            if not url:
                updated_docs.append(d)
                continue
            try:
                response = self.SESSION.get(
                    url,
                    headers={"Referer": self.BASE_URL},
                    timeout=60,
                )
                if response.status_code != 200:
                    self.logger.warning(f"  Doc download failed ({response.status_code}): {url}")
                    updated_docs.append(d)
                    continue

                # Derive filename
                title = d.get("title") or os.path.basename(urlparse(url).path) or "document"
                title = re.sub(r'[^\w\-. ]', '_', title)
                if not any(title.lower().endswith(e) for e in [
                    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.txt'
                ]):
                    ct = response.headers.get("Content-Type", "")
                    if "excel" in ct or "spreadsheet" in ct:
                        title += ".xlsx"
                    elif "word" in ct:
                        title += ".docx"
                    else:
                        title += ".pdf"

                ct_map = {
                    '.xlsx': 'application/vnd.ms-excel',
                    '.xls':  'application/vnd.ms-excel',
                    '.docx': 'application/msword',
                    '.doc':  'application/msword',
                    '.zip':  'application/zip',
                }
                content_type = next(
                    (v for k, v in ct_map.items() if title.lower().endswith(k)),
                    "application/pdf",
                )

                key = f"{self.base_folder}/{folder}/{title}"
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=response.content,
                    ContentType=content_type,
                )
                d["s3_path"]     = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"  ↑ S3: {key}")

            except Exception as e:
                self.logger.error(f"  S3 upload error: {e}")
                d["s3_path"] = d["uploaded_at"] = None

            updated_docs.append(d)

        # Persist updated doc list back to MongoDB
        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}},
        )
        return updated_docs

    def _retry_missing_uploads(self, hash_id: str, contract_id: str):
        existing = self.raw_collection.find_one({"hash_id": hash_id})
        if not existing:
            return
        docs    = existing.get("documents", [])
        pending = [d for d in docs if not d.get("s3_path") and d.get("original_url")]
        if pending:
            self.logger.info(f"  Retry {len(pending)} pending uploads for {contract_id}")
            self.upload_to_s3(pending, existing.get("teb_number", f"UNKNOWN"), existing["_id"])
        else:
            self.logger.info(f"  {contract_id}: all docs already uploaded")

    # ─────────────────────────────────────────────────────────────────────
    # PROCESS A SINGLE ROW
    # ─────────────────────────────────────────────────────────────────────
    def _process_row(self, row: dict, fiscal_year: int) -> bool:
        """
        Fetch detail for one search result row, store to MongoDB, upload docs.
        Returns True if a new record was inserted.
        """
        contract_id = row.get("id")
        if not contract_id:
            self.logger.warning("Row missing 'id' field, skipping.")
            return False

        hash_id = self.generate_hash(contract_id)

        # ── Duplicate check ──────────────────────────────────────────────
        if self.raw_collection.find_one({"hash_id": hash_id}):
            self.logger.info(f"  [dup] {row.get('contractNumber', contract_id)} — checking uploads")
            self._retry_missing_uploads(hash_id, contract_id)
            return False

        # ── Fetch detail ─────────────────────────────────────────────────
        contract_number = row.get("contractNumber", contract_id)
        self.logger.info(f"  [detail] {contract_number}")
        detail    = self.fetch_detail(contract_id)
        documents = self._parse_documents(detail)
        teb_no    = self.generate_teb_id()
        payload   = self.build_payload(row, detail, teb_no, hash_id, fiscal_year, documents)

        # ── Store to MongoDB ─────────────────────────────────────────────
        try:
            result = self.raw_collection.insert_one(payload)
            self.logger.info(f"  [stored] {contract_number}  TEB={teb_no}")
        except DuplicateKeyError:
            self.logger.info(f"  [dup-race] {contract_number}")
            self._retry_missing_uploads(hash_id, contract_id)
            return False

        # ── Upload documents to S3 ───────────────────────────────────────
        if documents:
            self.upload_to_s3(documents, teb_no, result.inserted_id)

        return True

    # ─────────────────────────────────────────────────────────────────────
    # CORE SCRAPE: paginate one fiscal year batch at a time
    # Fetch page → process all rows → fetch next page
    # ─────────────────────────────────────────────────────────────────────
    def _scrape_fiscal_years(self, fiscal_years: list) -> int:
        """
        Scrape all pages for the given list of fiscal years.
        Processes and stores each page before fetching the next.
        """
        page_num   = 1
        page_size  = 100
        total_new  = 0
        years_str  = ", ".join(str(y) for y in fiscal_years)

        while True:
            self.logger.info(f"[FY {years_str}] ── Page {page_num} ──────────────────────")

            # ── Fetch one page ───────────────────────────────────────────
            try:
                data = self.fetch_search_page(fiscal_years, page=page_num, size=page_size)
            except Exception as e:
                self.logger.error(f"[FY {years_str}] Search failed page {page_num}: {e}")
                break

            rows = data.get("results") or data.get("Results") or []
            if not rows:
                self.logger.info(f"[FY {years_str}] No rows on page {page_num} — done.")
                break

            total_rows = data.get("totalCount") or data.get("total") or "?"
            self.logger.info(
                f"[FY {years_str}] Page {page_num}: {len(rows)} rows "
                f"(total reported: {total_rows})"
            )

            # ── Process every row on this page ───────────────────────────
            # We use the first year in the list as the "primary" fiscal year label.
            # When scraping all years together the label is "ALL".
            fy_label  = fiscal_years[0] if len(fiscal_years) == 1 else "ALL"
            page_new  = 0
            for row in rows:
                try:
                    if self._process_row(row, fy_label):
                        page_new  += 1
                        total_new += 1
                except Exception as e:
                    cid = row.get("contractNumber", row.get("id", "?"))
                    self.logger.error(f"[FY {years_str}] Row error {cid}: {e}")
                time.sleep(random.uniform(0.5, 1.2))

            self.logger.info(
                f"[FY {years_str}] Page {page_num} complete — "
                f"new: {page_new}/{len(rows)}  (running total: {total_new})"
            )

            # ── Stop if last page ────────────────────────────────────────
            if len(rows) < page_size:
                self.logger.info(f"[FY {years_str}] Last page reached.")
                break

            page_num += 1
            time.sleep(random.uniform(1.0, 2.0))

        return total_new

    # ─────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────
    def scrape(self, scrape_mode: str = "all_at_once"):
        """
        scrape_mode:
          "all_at_once"  — single API call with all fiscal years (recommended,
                           matches what the browser does when you tick all years)
          "per_year"     — one API call per fiscal year (useful for targeted re-runs)
        """
        self.logger.info("=" * 60)
        self.logger.info(f"DC OCP Contracts Scraper starting")
        self.logger.info(f"Fiscal years: {self.fiscal_years}  ({min(self.fiscal_years)}–{max(self.fiscal_years)})")
        self.logger.info(f"Mode: {scrape_mode}")
        self.logger.info("=" * 60)

        grand_total = 0

        if scrape_mode == "per_year":
            for fy in self.fiscal_years:
                self.logger.info(f"\n{'='*60}")
                self.logger.info(f"Scraping Fiscal Year {fy}")
                self.logger.info("=" * 60)
                inserted     = self._scrape_fiscal_years([fy])
                grand_total += inserted
                self.logger.info(f"[FY {fy}] Done. Inserted: {inserted}")
                time.sleep(random.uniform(2.0, 4.0))

        else:  # all_at_once
            inserted    = self._scrape_fiscal_years(self.fiscal_years)
            grand_total = inserted

        self.logger.info("=" * 60)
        self.logger.info(f"All done. Total new records inserted: {grand_total}")
        self.logger.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="DC OCP Contracts Scraper")
    ap.add_argument(
        "--mode",
        choices=["all_at_once", "per_year"],
        default="all_at_once",
        help="all_at_once: single API call with all years (default); per_year: one call per FY",
    )
    ap.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help="Override fiscal years, e.g. --years 2024 2025 2026",
    )
    args = ap.parse_args()

    scraper = DCOCPContractsScraper()

    if args.years:
        scraper.fiscal_years = sorted(args.years)
        scraper.logger.info(f"Year override from CLI: {scraper.fiscal_years}")

    scraper.scrape(scrape_mode=args.mode)