import os
import logging
import requests
import time
import random
import boto3
import re
import hashlib

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from dateutil import parser
from pymongo import MongoClient, ReturnDocument
from requests.adapters import HTTPAdapter, Retry
from pymongo.errors import DuplicateKeyError

from dotenv import load_dotenv
load_dotenv()


class DelawareMmpBidScraper:

    # ─────────────────────────────────────────
    # INIT
    # ─────────────────────────────────────────
    def __init__(self):
        self.BASE_URL        = "https://mmp.delaware.gov"
        self.BIDS_API        = f"{self.BASE_URL}/Bids/GetBids"
        self.DETAIL_API      = f"{self.BASE_URL}/Bids/GetBidDetail"
        self.DOC_LIST_API    = f"{self.BASE_URL}/Bids/GetBidDocumentList"
        self.DETAIL_PAGE_URL = f"{self.BASE_URL}/Bids/Details"

        self.STATUSES  = ["Open", "RecentlyClosed"]
        self.PAGE_SIZE = 100

        # ── requests Session ──────────────────
        self.SESSION = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504])
        self.SESSION.mount("https://", HTTPAdapter(max_retries=retries))
        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language":  "en-US,en;q=0.9",
            "Referer":          f"{self.BASE_URL}/Bids",
            "Origin":           self.BASE_URL,
        })

        # ── MongoDB ──────────────────────────
        self.client          = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["delaware_mmp_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        # ── S3 ───────────────────────────────
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/delaware_mmp_bids"
        self.s3          = boto3.client(
            "s3",
            aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name           = os.getenv("AWS_REGION"),
        )

        # ── Logging ──────────────────────────
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s"
        )
        self.logger = logging.getLogger("Delaware_MMP_Bids")

    # ─────────────────────────────────────────
    # PRIME SESSION COOKIES
    # ─────────────────────────────────────────
    def prime_session(self):
        self.logger.info("Priming session cookies via GET /Bids ...")
        resp = self.SESSION.get(f"{self.BASE_URL}/Bids", timeout=30)
        resp.raise_for_status()
        self.logger.info(f"Session primed. Cookies: {list(self.SESSION.cookies.keys())}")

    # ─────────────────────────────────────────
    # TEB ID GENERATION
    # ─────────────────────────────────────────
    def generate_teb_id(self):
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
            7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"
        }
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # ─────────────────────────────────────────
    # DATE PARSING
    # ─────────────────────────────────────────
    def parse_date(self, date_val):
        try:
            if not date_val:
                return None
            dt = parser.parse(str(date_val).strip(), dayfirst=False)
            return dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            self.logger.error(f"Date parse failed: {date_val} | {e}")
            return None

    # ─────────────────────────────────────────
    # HASH ID
    # ─────────────────────────────────────────
    def generate_hash(self, bid_id) -> str:
        return hashlib.md5(str(bid_id).encode()).hexdigest()

    # ─────────────────────────────────────────
    # S3 UPLOAD
    # FIX: accepts teb_number and mongo_id as explicit args instead of
    # pulling from the dict, so it works even if the dict key is missing.
    # FIX: returns the updated documents list so the caller can persist it.
    # ─────────────────────────────────────────
    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        folder       = f"{teb_number.replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in documents:
            try:
                url = d.get("original_url")
                if not url:
                    updated_docs.append(d)
                    continue

                response = self.SESSION.get(url, timeout=60)
                if response.status_code != 200:
                    self.logger.warning(f"Could not download document: {url}")
                    updated_docs.append(d)
                    continue

                title = d.get("title") or os.path.basename(urlparse(url).path) or "document"
                title = re.sub(r'[^\w\-. ]', '_', title)
                if not any(title.lower().endswith(ext) for ext in
                           ['.pdf', '.doc', '.docx', '.xls', '.xlsx']):
                    title += ".pdf"

                key          = f"{self.base_folder}/{folder}/{title}"
                content_type = "application/pdf"
                if title.lower().endswith(('.xls', '.xlsx')):
                    content_type = "application/vnd.ms-excel"
                elif title.lower().endswith(('.doc', '.docx')):
                    content_type = "application/msword"

                self.s3.put_object(
                    Bucket=self.bucket, Key=key,
                    Body=response.content, ContentType=content_type
                )
                d["s3_path"]     = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"Uploaded to S3: {key}")

            except Exception as e:
                self.logger.error(f"S3 upload failed for {d.get('original_url')}: {e}")
                d["s3_path"]     = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        # Persist the updated documents (with s3_path) back to Mongo
        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}}
        )
        return updated_docs

    # ─────────────────────────────────────────
    # FETCH BID LIST
    # ─────────────────────────────────────────
    def fetch_bid_list(self, status: str) -> list:
        all_rows  = []
        page      = 1
        max_pages = 1

        while page <= max_pages:
            try:
                resp = self.SESSION.post(
                    self.BIDS_API,
                    params={"status": status},
                    json={
                        "_search": False,
                        "nd":      int(time.time() * 1000),
                        "rows":    self.PAGE_SIZE,
                        "page":    page,
                        "sidx":    "OpenDate",
                        "sord":    "desc",
                    },
                    headers={
                        "Content-Type":     "application/json; charset=UTF-8",
                        "Accept":           "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=30,
                )

                if not resp.ok:
                    self.logger.error(
                        f"[{status}] Page {page} – HTTP {resp.status_code}: "
                        f"{resp.text[:300]}"
                    )
                    break

                data      = resp.json()
                max_pages = int(data.get("total", 1))
                rows      = data.get("rows", [])

                if not rows:
                    self.logger.info(f"[{status}] No rows on page {page}, stopping.")
                    break

                all_rows.extend(rows)
                total_records = data.get("records", "?")
                self.logger.info(
                    f"[{status}] Page {page}/{max_pages} – "
                    f"fetched {len(rows)} rows "
                    f"(cumulative: {len(all_rows)}/{total_records})"
                )

                page += 1
                time.sleep(random.uniform(0.5, 1.2))

            except Exception as e:
                self.logger.error(f"Failed fetching bid list page {page} [{status}]: {e}")
                break

        return all_rows

    # ─────────────────────────────────────────
    # FETCH BID DETAIL
    # ─────────────────────────────────────────
    def fetch_bid_detail(self, bid_id: int) -> dict:
        try:
            resp = self.SESSION.get(
                self.DETAIL_API,
                params={"id": bid_id, "_": int(time.time() * 1000)},
                timeout=30,
            )
            resp.raise_for_status()
            return self._parse_detail_html(resp.text, bid_id)
        except Exception as e:
            self.logger.error(f"Failed fetching detail for bid {bid_id}: {e}")
            return {}

    def _parse_detail_html(self, html: str, bid_id: int) -> dict:
        soup = BeautifulSoup(html, "lxml")

        h1_tags         = soup.find_all("h1")
        title           = h1_tags[1].get_text(strip=True) if len(h1_tags) > 1 else None
        contract_number = h1_tags[2].get_text(strip=True) if len(h1_tags) > 2 else None

        ad_date_raw   = None
        deadline_raw  = None
        deadline_time = None
        contact_email = None

        for strong in soup.find_all("strong"):
            label = strong.get_text(strip=True).lower()
            p     = strong.find_next_sibling("p")

            if "solicitation ad date" in label:
                if p:
                    ad_date_raw = p.get_text(strip=True)

            elif "deadline" in label:
                if p:
                    txt           = p.get_text(strip=True)
                    dm            = re.match(r'(\d{1,2}/\d{1,2}/\d{4})', txt)
                    tm            = re.search(r'at\s+([\d:]+\s*[APap][Mm])', txt)
                    deadline_raw  = dm.group(1) if dm else txt
                    deadline_time = tm.group(1).strip() if tm else None

            elif "contact" in label:
                a = strong.find_next("a")
                if a:
                    contact_email = a.get_text(strip=True)

        return {
            "detail_title":    title,
            "contract_number": contract_number,
            "ad_date_raw":     ad_date_raw,
            "deadline_raw":    deadline_raw,
            "deadline_time":   deadline_time,
            "contact_email":   contact_email,
            "detail_page_url": f"{self.DETAIL_PAGE_URL}/{bid_id}",
        }

    # ─────────────────────────────────────────
    # FETCH BID DOCUMENTS
    # FIX: added retry loop (up to 3 attempts) so transient network errors
    # don't silently drop all documents for a bid.
    # ─────────────────────────────────────────
    def fetch_bid_documents(self, bid_id: int, retries: int = 3) -> list:
        documents = []
        for attempt in range(1, retries + 1):
            try:
                resp = self.SESSION.get(
                    self.DOC_LIST_API,
                    params={"id": bid_id, "currentCount": 0},
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept":           "text/html, */*; q=0.01",
                        "Referer":          f"{self.BASE_URL}/Bids/Details/{bid_id}",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if not href or href.startswith("#"):
                        continue
                    full_url  = href if href.startswith("http") else urljoin(self.BASE_URL, href)
                    doc_title = a.get_text(strip=True) or os.path.basename(urlparse(full_url).path)
                    documents.append({
                        "type":         "Tender_document",
                        "title":        doc_title,
                        "original_url": full_url,
                        "s3_path":      None,
                        "uploaded_at":  None,
                    })
                self.logger.info(f"  Found {len(documents)} document(s) for bid {bid_id}")
                return documents  # success — exit retry loop

            except Exception as e:
                self.logger.warning(
                    f"Attempt {attempt}/{retries} failed fetching documents "
                    f"for bid {bid_id}: {e}"
                )
                if attempt < retries:
                    time.sleep(2 ** attempt)  # exponential back-off

        self.logger.error(f"All {retries} attempts failed for bid {bid_id} documents.")
        return documents

    # ─────────────────────────────────────────
    # PARSE A SINGLE BID ROW
    # ─────────────────────────────────────────
    def parse_bid_row(self, row: dict, status: str) -> dict:
        bid_id    = row.get("Id")
        detail    = self.fetch_bid_detail(bid_id)
        documents = self.fetch_bid_documents(bid_id)

        return {
            "bid_id":               bid_id,
            "bid_title":            row.get("Title"),
            "contract_number":      row.get("ContractNumber"),
            "agency_code":          row.get("AgencyCode"),
            "bid_status":           status,
            "open_date":            self.parse_date(row.get("OpenDate")),
            "deadline_date":        self.parse_date(row.get("DeadlineDate")),
            "awarded_date":         self.parse_date(row.get("AwardedDate")),
            "contact_email":        row.get("ContactEmail") or detail.get("contact_email"),
            "unspsc_codes":         row.get("BidUnspscCodesString"),
            "deldot_entry":         row.get("DeldotEntry", False),
            "solicitation_ad_date": self.parse_date(detail.get("ad_date_raw")),
            "deadline_time":        detail.get("deadline_time"),
            "detail_page_url":      detail.get("detail_page_url"),
            "documents":            documents,
        }

    # ─────────────────────────────────────────
    # RETRY DOCUMENTS FOR EXISTING RECORD
    # FIX: on DuplicateKeyError, check whether the existing record has
    # documents with missing s3_path and re-upload them rather than
    # skipping entirely.
    # ─────────────────────────────────────────
    def _retry_missing_uploads(self, hash_id: str, bid_id: int):
        existing = self.raw_collection.find_one({"hash_id": hash_id})
        if not existing:
            return

        docs = existing.get("documents", [])

        # Re-fetch document list if the record has none stored at all
        if not docs:
            self.logger.info(
                f"Duplicate bid {bid_id} has no documents stored — re-fetching."
            )
            docs = self.fetch_bid_documents(bid_id)
            if docs:
                self.raw_collection.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"documents": docs}}
                )

        # Upload any documents that are missing an s3_path
        pending = [d for d in docs if not d.get("s3_path")]
        if pending:
            self.logger.info(
                f"Duplicate bid {bid_id} has {len(pending)} un-uploaded doc(s) — uploading now."
            )
            # Temporarily replace the full list with pending-only for upload,
            # then merge back so we don't overwrite already-uploaded entries.
            uploaded = self.upload_to_s3(
                pending,
                teb_number=existing.get("teb_number", f"UNKNOWN_{bid_id}"),
                mongo_id=existing["_id"],
            )
            # Merge: replace pending entries with their uploaded versions
            uploaded_map = {d["original_url"]: d for d in uploaded if d.get("original_url")}
            merged = []
            for d in docs:
                url = d.get("original_url")
                merged.append(uploaded_map.get(url, d) if url else d)
            self.raw_collection.update_one(
                {"_id": existing["_id"]},
                {"$set": {"documents": merged}}
            )
        else:
            self.logger.info(
                f"Duplicate bid {bid_id} — all documents already uploaded, skipping."
            )

    # ─────────────────────────────────────────
    # CORE ENGINE
    # ─────────────────────────────────────────
    def scrape(self):
        global_total = 0

        self.prime_session()

        for status in self.STATUSES:
            self.logger.info(f"{'='*60}")
            self.logger.info(f"Starting scrape for status: [{status}]")
            self.logger.info(f"{'='*60}")

            bid_rows = self.fetch_bid_list(status)
            self.logger.info(f"[{status}] Total bids fetched: {len(bid_rows)}")

            status_total = 0

            for row in bid_rows:
                bid_id          = row.get("Id")
                contract_number = row.get("ContractNumber", "UNKNOWN")

                # ── FIX: check for duplicate BEFORE fetching detail/docs
                # to avoid wasting API calls on already-stored bids.
                hash_id = self.generate_hash(bid_id)
                if self.raw_collection.find_one({"hash_id": hash_id}):
                    self.logger.info(
                        f"[{status}] Duplicate – checking uploads: {contract_number}"
                    )
                    self._retry_missing_uploads(hash_id, bid_id)
                    continue

                try:
                    parsed = self.parse_bid_row(row, status)
                    teb_no = self.generate_teb_id()

                    bid_payload = {
                        "hash_id":              hash_id,
                        "teb_number":           teb_no,
                        "bid_id":               parsed["bid_id"],
                        "quotation_number":     parsed["contract_number"],
                        "quotation_subject":    parsed["bid_title"],
                        "agency_code":          parsed["agency_code"],
                        "bid_status":           parsed["bid_status"],
                        "start_date":           parsed["open_date"],
                        "end_date":             parsed["deadline_date"],
                        "awarded_date":         parsed["awarded_date"],
                        "deadline_time":        parsed["deadline_time"],
                        "solicitation_ad_date": parsed["solicitation_ad_date"],
                        "contact_email":        parsed["contact_email"],
                        "unspsc_codes":         parsed["unspsc_codes"],
                        "deldot_entry":         parsed["deldot_entry"],
                        "detail_page_url":      parsed["detail_page_url"],
                        # Store documents immediately; s3_path filled in after upload
                        "documents":            parsed["documents"],
                        "source":               "State of Delaware - MMP Bids",
                        "etl_status":           "pending",
                        "created_at":           datetime.now(timezone.utc),
                    }

                    try:
                        res = self.raw_collection.insert_one(bid_payload)
                        self.logger.info(
                            f"[{status}] Stored: {contract_number} (TEB: {teb_no})"
                        )
                    except DuplicateKeyError:
                        # Race condition between the pre-check and insert —
                        # treat the same as a normal duplicate.
                        self.logger.info(
                            f"[{status}] Race-condition duplicate – checking uploads: "
                            f"{contract_number}"
                        )
                        self._retry_missing_uploads(hash_id, bid_id)
                        continue

                    # ── FIX: pass teb_number and mongo_id explicitly
                    if bid_payload.get("documents"):
                        self.upload_to_s3(
                            bid_payload["documents"],
                            teb_number=teb_no,
                            mongo_id=res.inserted_id,
                        )

                    status_total += 1
                    global_total += 1

                except Exception as e:
                    self.logger.error(
                        f"[{status}] Failed processing bid {bid_id} ({contract_number}): {e}"
                    )

                time.sleep(random.uniform(0.8, 1.8))

            self.logger.info(f"[{status}] Completed. Records inserted: {status_total}")

        self.logger.info(f"All runs complete. Total records inserted: {global_total}")


if __name__ == "__main__":
    DelawareMmpBidScraper().scrape()