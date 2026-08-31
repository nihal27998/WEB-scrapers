import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import random
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import boto3
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from PIL import Image
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry

try:
    from curl_cffi import requests as cf_requests
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False

load_dotenv()


# ══════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════

def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def decode_cf_email(cfemail_hex: str) -> str:
    try:
        raw = bytes.fromhex(cfemail_hex)
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except Exception:
        return ""


def parse_date(raw: str | None, context: str = "") -> datetime | None:
    if not raw:
        return None
    # Remove timezone suffix like (CT), (CST), etc.
    cleaned = re.sub(r'\s*\([A-Z]{2,3}\)\s*', '', raw)
    cleaned = re.sub(r'\s+[A-Z]{2,3}\s*$', '', cleaned)
    try:
        dt = dateutil_parser.parse(cleaned.strip(), dayfirst=False)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception as e:
        logging.getLogger("date_parser").warning(f"Failed to parse '{raw}' [{context}]: {e}")
        return None


def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════
# BASE SCRAPER
# ══════════════════════════════════════════════════════════════════

class BaseScraper(ABC):
    LOGGER_NAME: str = "base_scraper"

    _RETRY_CONFIG = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 502, 503, 504],
    )

    _DEFAULT_HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }

    def __init__(self, use_curl_cffi: bool = True, impersonate: str = "chrome124", request_delay: tuple[float, float] = (0.8, 1.8)):
        self.logger = self._build_logger()
        self.request_delay = request_delay
        self._impersonate: str | None = None
        self.session = self._build_session(use_curl_cffi, impersonate)

    def _build_logger(self) -> logging.Logger:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        return logging.getLogger(self.LOGGER_NAME)

    def _build_session(self, use_curl_cffi: bool, impersonate: str):
        if use_curl_cffi and HAVE_CURL_CFFI:
            session = cf_requests.Session()
            self._impersonate = impersonate
            self.logger.info(f"HTTP session: curl_cffi (impersonating {impersonate})")
        else:
            session = requests.Session()
            adapter = HTTPAdapter(max_retries=self._RETRY_CONFIG)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self.logger.info("HTTP session: requests (with retry adapter)")
        session.headers.update(self._DEFAULT_HEADERS)
        return session

    def _get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        if self._impersonate:
            kwargs.setdefault("impersonate", self._impersonate)
        resp = self.session.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        if self._impersonate:
            kwargs.setdefault("impersonate", self._impersonate)
        resp = self.session.post(url, **kwargs)
        resp.raise_for_status()
        return resp

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        """Best-effort extraction of an HTTP status code from a raised
        exception, since curl_cffi's exception types don't always expose
        `.response.status_code` the same way requests does."""
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status is not None:
            return status
        match = re.search(r"\b(\d{3})\b", str(exc))
        return int(match.group(1)) if match else None

    def _get_with_retry(
        self,
        url: str,
        max_retries: int = 4,
        base_delay: float = 8.0,
        **kwargs,
    ) -> requests.Response:
        """
        Like _get(), but with explicit retry/backoff specifically for 429
        (Too Many Requests). This exists because when curl_cffi is the
        active session (HAVE_CURL_CFFI True), requests' HTTPAdapter/Retry
        config configured in _RETRY_CONFIG never actually applies — so
        without this, a single 429 fails the call outright with no retry.
        """
        attempt = 0
        while True:
            try:
                return self._get(url, **kwargs)
            except Exception as exc:
                status = self._status_of(exc)
                attempt += 1
                if status != 429 or attempt > max_retries:
                    raise
                delay = base_delay * attempt + random.uniform(0, 3)
                self.logger.warning(
                    f"    429 rate-limited on {url} "
                    f"(attempt {attempt}/{max_retries}) — backing off {delay:.1f}s …"
                )
                time.sleep(delay)

    def _polite_sleep(self) -> None:
        delay = random.uniform(*self.request_delay)
        self.logger.debug(f"Sleeping {delay:.2f}s …")
        time.sleep(delay)

    @abstractmethod
    def scrape(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════════
# MANSFIELD ISD SCRAPER
# ══════════════════════════════════════════════════════════════════

class MansfieldISDScraper(BaseScraper):
    LOGGER_NAME = "mansfield_isd"

    def __init__(self) -> None:
        super().__init__()

        # Base URLs for Mansfield ISD
        self.base_url = "https://misd.ionwave.net"
        self.listing_url = f"{self.base_url}/SourcingEvents.aspx"
        self.detail_url = f"{self.base_url}/PublicDetail.aspx"

        # Source types (may differ – inspect the site)
        self.source_types: dict[int, str] = {
            1: "current_bids",
            2: "closed_bids",
        }

        # Optional: inject Cloudflare clearance cookie if needed
        cf_clearance = os.getenv("MISD_CF_CLEARANCE") or os.getenv("CF_CLEARANCE")
        if cf_clearance:
            self.session.cookies.set("cf_clearance", cf_clearance, domain="misd.ionwave.net")
            self.logger.info("cf_clearance cookie injected for Mansfield ISD.")

        # MongoDB setup (separate collection)
        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db = self.client["tender_bharo"]
        self.raw_col = self.db["mansfield_isd_notice"]
        self.meta_col = self.db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        # S3 setup (separate folder)
        self.bucket = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/mansfield_isd"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION"),
        )
        self.debug_detail_saved = False

        # ── Persistent Playwright handles for tender-notice screenshots ──
        # Lazily launched on first use (see _ensure_screenshot_browser),
        # then reused for every record so we don't pay the cost of
        # relaunching Chromium per bid. Torn down via close().
        self._pw = None
        self._browser = None
        self._browser_context = None
        self._screenshot_page = None

    # -----------------------------------------------------------------
    # Helper: get next page target dynamically
    # -----------------------------------------------------------------
    def _get_next_page_target(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "lxml")
        next_btn = soup.select_one(".rgPageNext")
        if next_btn and next_btn.get("name"):
            return next_btn["name"]
        next_input = soup.find("input", {"value": re.compile(r"Next", re.I)})
        if next_input and next_input.get("name"):
            return next_input["name"]
        return None

    # -----------------------------------------------------------------
    # Session & Pagination
    # -----------------------------------------------------------------
    def prime_session(self, source_type: int) -> str:
        url = f"{self.listing_url}?SourceType={source_type}"
        self.logger.info("═" * 60)
        self.logger.info(f"Priming session for SourceType={source_type} ({self.source_types[source_type]}) …")
        resp = self._get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
        })
        self.logger.info(f"Session primed. Cookies: {list(self.session.cookies.keys())}")
        return resp.text

    def _fetch_total_pages(self, page1_html: str) -> tuple[int | None, int]:
        soup = BeautifulSoup(page1_html, "lxml")
        info_part = soup.select_one("#ctl00_mainContent_rgBidList_ctl00 .rgInfoPart")
        total_items, total_pages = None, 1
        if info_part:
            nums = [int(s.get_text(strip=True)) for s in info_part.find_all("strong") if s.get_text(strip=True).isdigit()]
            if len(nums) >= 2:
                total_items, total_pages = nums[0], nums[1]
        self.logger.info(f"Pager: total_items={total_items}  total_pages={total_pages}")
        return total_items, total_pages

    def _fetch_list_page(self, page: int, source_type: int, prev_html: str) -> tuple[list[dict], str]:
        url = f"{self.listing_url}?SourceType={source_type}"
        if page == 1:
            rows = self._parse_table_rows(prev_html, source_type)
            if rows:
                self.logger.info(f"DEBUG: First row on page 1: {rows[0]}")
            return rows, prev_html

        soup = BeautifulSoup(prev_html, "lxml")
        fields = self._extract_form_fields(soup)

        next_target = self._get_next_page_target(prev_html)
        if not next_target:
            self.logger.error("Could not find next page button, using fallback constant")
            next_target = "ctl00$mainContent$rgBidList$ctl00$ctl03$ctl01$ctl08"

        fields["__EVENTTARGET"] = next_target
        fields["__EVENTARGUMENT"] = ""

        self.logger.info(f"Posting to page {page} with __EVENTTARGET={next_target}")

        resp = self._post(url, data=fields, headers={"Referer": url, "Content-Type": "application/x-www-form-urlencoded"})
        rows = self._parse_table_rows(resp.text, source_type)
        if rows:
            self.logger.info(f"DEBUG: First row on page {page}: {rows[0]}")
        return rows, resp.text

    def _parse_table_rows(self, html: str, source_type: int) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        rows = []
        table = soup.select_one("#ctl00_mainContent_rgBidList_ctl00")
        if not table:
            self.logger.warning("Listing table not found")
            return rows

        # Extract column headers dynamically
        headers_row = table.select_one("thead tr.rgHeaderRow")
        if not headers_row:
            headers_row = table.select_one("thead tr")
        if not headers_row:
            self.logger.warning("Could not find header row; using fallback mapping")
            col_map = {"bid_number": 1, "title": 2, "bid_type": 3, "organization": 4, "open_date": 5, "close_date": 6}
        else:
            col_map = {}
            for idx, th in enumerate(headers_row.find_all("th")):
                header_text = clean_text(th.get_text())
                if not header_text:
                    continue
                lower_header = header_text.lower()
                if "number" in lower_header:
                    col_map["bid_number"] = idx
                elif "title" in lower_header:
                    col_map["title"] = idx
                elif "type" in lower_header:
                    col_map["bid_type"] = idx
                elif "department" in lower_header or "organization" in lower_header:
                    col_map["organization"] = idx
                elif "issue" in lower_header and "date" in lower_header:
                    col_map["open_date"] = idx
                elif "close" in lower_header and "date" in lower_header:
                    col_map["close_date"] = idx
            self.logger.info(f"Dynamic column mapping for SourceType={source_type}: {col_map}")

        # Extract bid_id mapping from client key values
        bid_id_map = self._extract_client_key_values(html)

        # Parse data rows
        for i, tr in enumerate(table.select("tbody tr.rgRow, tbody tr.rgAltRow")):
            cells = tr.find_all("td")
            if len(cells) < max(col_map.values(), default=0) + 1:
                self.logger.warning(f"Row {i} has only {len(cells)} cells, skipping")
                continue

            row = {
                "bid_id": bid_id_map.get(i),
                "bid_number": clean_text(cells[col_map["bid_number"]].get_text()) if "bid_number" in col_map else None,
                "title": clean_text(cells[col_map["title"]].get_text()) if "title" in col_map else None,
                "bid_type": clean_text(cells[col_map["bid_type"]].get_text()) if "bid_type" in col_map else None,
                "organization": clean_text(cells[col_map["organization"]].get_text()) if "organization" in col_map else None,
                "open_date": clean_text(cells[col_map["open_date"]].get_text()) if "open_date" in col_map else None,
                "close_date": clean_text(cells[col_map["close_date"]].get_text()) if "close_date" in col_map else None,
            }
            rows.append(row)

        return rows

    # -----------------------------------------------------------------
    # Detail Page
    # -----------------------------------------------------------------
    def fetch_detail(self, bid_id: str, source_type: int) -> dict:
        url = f"{self.detail_url}?bidID={bid_id}&SourceType={source_type}"
        self.logger.info(f"    Fetching detail bidID={bid_id} …")
        try:
            time.sleep(random.uniform(2, 4))
            resp = self._get_with_retry(url, headers={"Referer": f"{self.listing_url}?SourceType={source_type}"})
            if not self.debug_detail_saved:
                with open("debug_mansfield_detail.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                self.logger.info("DEBUG: Saved first detail HTML to debug_mansfield_detail.html")
                self.debug_detail_saved = True
            return self._parse_detail(resp.text, bid_id, url)
        except Exception as exc:
            self.logger.error(f"    Detail fetch failed [{bid_id}]: {exc}")
            return {}

    def _parse_detail(self, html: str, bid_id: str, detail_url: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        detail = {"bid_id": bid_id, "detail_url": detail_url}

        def get_text(elem_id: str) -> str | None:
            el = soup.find(id=elem_id)
            return clean_text(el.get_text()) if el else None

        # Primary IDs (may need adjustment if different)
        detail["type"] = get_text("ctl00_mainContent_lblType")
        detail["status"] = get_text("ctl00_mainContent_lblStatus")
        detail["number"] = get_text("ctl00_mainContent_lblNumber")
        detail["issue_datetime"] = get_text("ctl00_mainContent_lblIssue")
        detail["close_datetime"] = get_text("ctl00_mainContent_lblClose")
        detail["question_cutoff"] = get_text("ctl00_mainContent_lblQuestionCutoff")

        # Fallback label search
        if not detail["issue_datetime"]:
            issue_label = soup.find(string=re.compile(r"Issue Date", re.I))
            if issue_label:
                parent = issue_label.find_parent("td") or issue_label.find_parent("div")
                if parent:
                    value = parent.find_next_sibling("td") or parent.find_next("div")
                    if value:
                        detail["issue_datetime"] = clean_text(value.get_text())
        if not detail["close_datetime"]:
            close_label = soup.find(string=re.compile(r"Close Date|Bid Close", re.I))
            if close_label:
                parent = close_label.find_parent("td") or close_label.find_parent("div")
                if parent:
                    value = parent.find_next_sibling("td") or parent.find_next("div")
                    if value:
                        detail["close_datetime"] = clean_text(value.get_text())
        if not detail["question_cutoff"]:
            q_label = soup.find(string=re.compile(r"Question Cutoff|Deadline for Questions", re.I))
            if q_label:
                parent = q_label.find_parent("td") or q_label.find_parent("div")
                if parent:
                    value = parent.find_next_sibling("td") or parent.find_next("div")
                    if value:
                        detail["question_cutoff"] = clean_text(value.get_text())

        self.logger.debug(f"Raw dates: issue={detail['issue_datetime']}, close={detail['close_datetime']}, qcutoff={detail['question_cutoff']}")

        notes_el = soup.find(id="ctl00_mainContent_lblNotes")
        detail["description"] = clean_text(notes_el.get_text(" ")) if notes_el else None

        # Contact details
        detail["contact_name"] = get_text("ctl00_mainContent_lblName")
        raw_address = get_text("ctl00_mainContent_lblAddress")
        if raw_address:
            detail["contact_address"] = re.sub(r"\s+", " ", raw_address).strip()
        else:
            detail["contact_address"] = None
        detail["contact_phone"] = get_text("ctl00_mainContent_lblPhone")
        detail["contact_fax"] = get_text("ctl00_mainContent_lblFax")
        email_el = soup.select_one("#ctl00_mainContent_lblEmail .__cf_email__")
        detail["contact_email"] = decode_cf_email(email_el["data-cfemail"]) if email_el and email_el.get("data-cfemail") else None

        # Attachments, Questions, Activities
        detail["documents"] = self._extract_attachments(html, detail_url)
        self.logger.info(f"Attachments extracted = {len(detail['documents'])}")
        detail["questions"] = self._parse_questions(soup)
        detail["participation_activities"] = self._parse_activities(soup)

        return detail

    def _extract_attachments(self, html: str, detail_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        attachments = []
        seen = set()

        def _add(title, url, fmt_name=None, fmt_type=None, description=None, file_size=None):
            if url not in seen:
                seen.add(url)
                attachments.append({
                    "type": "Tender_document",
                    "title": clean_text(title) or os.path.basename(urlparse(url).path),
                    "format_name": fmt_name,
                    "format_type": fmt_type,
                    "description": description,
                    "file_size": file_size,
                    "original_url": url,
                    "s3_path": None,
                    "uploaded_at": None,
                })

        doc_table = soup.select_one("#ctl00_mainContent_rgBidDocuments_ctl00 > tbody")
        if doc_table:
            for tr in doc_table.select("tr.rgRow, tr.rgAltRow"):
                link = tr.select_one("a.procLink")
                if not link:
                    continue
                fmt_name = tr.select_one("[id*=lblFormatName]")
                fmt_type = tr.select_one("[id*=lblFormatType]")
                _add(
                    title=link.get_text(),
                    url=urljoin(detail_url, link["href"]),
                    fmt_name=clean_text(fmt_name.get_text()) if fmt_name else None,
                    fmt_type=clean_text(fmt_type.get_text()) if fmt_type else None,
                )

        att_table = soup.select_one("#ctl00_mainContent_rgBidAttachments_ctl00 > tbody")
        if att_table:
            for tr in att_table.select("tr.rgRow, tr.rgAltRow"):
                cells = tr.find_all("td")
                link = tr.select_one("a.procLink")
                if not link:
                    continue
                _add(
                    title=link.get_text(),
                    url=urljoin(detail_url, link["href"]),
                    description=clean_text(cells[1].get_text()) if len(cells) > 1 else None,
                    file_size=clean_text(cells[2].get_text()) if len(cells) > 2 else None,
                )

        if not attachments:
            for a in soup.find_all("a", href=re.compile(r"[Ee]xtract\.aspx", re.I)):
                _add(title=a.get_text(), url=urljoin(detail_url, a["href"]))

        self.logger.info(f"    Attachments: {len(attachments)}")
        return attachments

    @staticmethod
    def _parse_questions(soup: BeautifulSoup) -> list[dict]:
        questions = []
        container = soup.select_one("#ctl00_mainContent_divOnlinePublicQuestionsExist")
        if not container:
            return questions
        for item in container.select("table.rptItem, table.rptAltItem"):
            row_map = {}
            for tr in item.find_all("tr"):
                label = tr.select_one("td.fieldLabel")
                value = tr.select_one("td.fieldValue")
                if label and value:
                    row_map[clean_text(label.get_text()).lower()] = clean_text(value.get_text(" "))
            if row_map:
                questions.append(row_map)
        return questions

    @staticmethod
    def _parse_activities(soup: BeautifulSoup) -> list[dict]:
        activities = []
        table = soup.select_one("#ctl00_mainContent_rgParticipationActivities_ctl00 > tbody")
        if not table:
            return activities
        for tr in table.select("tr.rgRow, tr.rgAltRow"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            activities.append({
                "activity_date": clean_text(cells[1].get_text()),
                "activity_name": clean_text(cells[2].get_text()),
                "description": clean_text(cells[3].get_text(" ")),
            })
        return activities

    # -----------------------------------------------------------------
    # Form helpers
    # -----------------------------------------------------------------
    @staticmethod
    def _extract_form_fields(soup: BeautifulSoup) -> dict[str, str]:
        fields = {}
        for inp in soup.select("form#aspnetForm input"):
            name = inp.get("name")
            itype = (inp.get("type") or "text").lower()
            if not name or itype in ("submit", "image", "button", "checkbox", "radio"):
                continue
            fields[name] = inp.get("value", "")
        return fields

    @staticmethod
    def _extract_client_key_values(html: str) -> dict[int, str]:
        match = re.search(r'"_clientKeyValues"\s*:\s*(\{.*?\})\s*,\s*"_controlToFocus"', html, re.DOTALL)
        if not match:
            return {}
        try:
            raw = json.loads(match.group(1))
        except Exception:
            return {}
        return {int(idx): str(val["BidID"]) for idx, val in raw.items() if "BidID" in val}

    # -----------------------------------------------------------------
    # S3 Upload — contract/bid documents
    # -----------------------------------------------------------------
    def _upload_to_s3(self, doc: dict, mongo_id) -> None:
        folder = f"{doc['teb_number'].replace('/', '_')}_{mongo_id}"
        updated = []
        for att in doc.get("documents", []):
            url = att.get("original_url")
            if not url:
                updated.append(att)
                continue
            try:
                r = self.session.get(url, headers={"Referer": doc.get("detail_url", self.listing_url), "Accept": "*/*"}, timeout=60)
                if r.status_code != 200:
                    self.logger.warning(f"    Download failed {url}: HTTP {r.status_code}")
                    updated.append(att)
                    continue

                fname = re.sub(r"[^\w\-. ]", "_", att.get("title") or os.path.basename(urlparse(url).path) or "file")
                ext = os.path.splitext(fname)[-1].lower()
                content_type = {
                    ".pdf": "application/pdf",
                    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ".xls": "application/vnd.ms-excel",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".doc": "application/msword",
                    ".zip": "application/zip",
                }.get(ext, "application/octet-stream")
                key = f"{self.base_folder}/{folder}/{fname}"

                self.s3.put_object(Bucket=self.bucket, Key=key, Body=r.content, ContentType=content_type)
                att["s3_path"] = f"s3://{self.bucket}/{key}"
                att["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"    S3 ✓ {fname}")
            except Exception as exc:
                self.logger.error(f"    S3 error {url}: {exc}")
                att["s3_path"] = None
                att["uploaded_at"] = None
            updated.append(att)
            time.sleep(random.uniform(0.3, 0.8))
        # Merge against whatever is currently in Mongo (e.g. a notice screenshot
        # pushed by a concurrent/prior step) instead of blindly overwriting.
        current = self.raw_col.find_one({"_id": mongo_id}, {"documents": 1}) or {}
        current_docs = current.get("documents", [])
        updated_by_url = {d.get("original_url"): d for d in updated if d.get("original_url")}
        merged = []
        for d in current_docs:
            url = d.get("original_url")
            merged.append(updated_by_url.pop(url, d) if url in updated_by_url else d)
        merged.extend(updated_by_url.values())
        self.raw_col.update_one({"_id": mongo_id}, {"$set": {"documents": merged}})

    # -----------------------------------------------------------------
    # TENDER NOTICE — full-page screenshot, saved as PDF, uploaded to S3
    # -----------------------------------------------------------------
    def _ensure_screenshot_browser(self) -> bool:
        """
        Lazily launches a persistent headless Chromium instance used only
        for screenshotting detail pages. Reused for every record across
        the whole scrape; torn down via close(). Returns True if the
        browser is ready to use.
        """
        if self._browser_context:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error(
                "playwright not installed — run:\n"
                "  pip install playwright && playwright install chromium"
            )
            return False

        try:
            self.logger.info("Launching Playwright browser for tender-notice screenshots…")
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            self._browser_context = self._browser.new_context(
                user_agent=self._DEFAULT_HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            self._screenshot_page = self._browser_context.new_page()
            return True
        except Exception as e:
            self.logger.error(f"Failed to launch screenshot browser: {e}")
            return False

    def _sync_cookies_to_browser(self) -> None:
        """
        Copy the scraping session's current cookies (ASP.NET_SessionId,
        cf_clearance if present, etc.) into the Playwright browser context
        so screenshots render as the same authenticated session instead of
        a fresh, unauthenticated browser hitting a login/challenge page.
        """
        if not self._browser_context:
            return
        pw_cookies = []
        try:
            for c in self.session.cookies:
                name = getattr(c, "name", None)
                value = getattr(c, "value", None)
                domain = (getattr(c, "domain", "") or "misd.ionwave.net").lstrip(".")
                path = getattr(c, "path", "/") or "/"
                if name and value is not None:
                    pw_cookies.append({"name": name, "value": value, "domain": domain, "path": path})
        except TypeError:
            # Fallback for cookie containers that aren't iterable the same way
            try:
                for name, value in dict(self.session.cookies).items():
                    pw_cookies.append({"name": name, "value": value, "domain": "misd.ionwave.net", "path": "/"})
            except Exception as e:
                self.logger.debug(f"Could not read session cookies for screenshot sync: {e}")

        if pw_cookies:
            try:
                self._browser_context.add_cookies(pw_cookies)
            except Exception as e:
                self.logger.debug(f"Could not sync cookies into browser context: {e}")

    def _take_full_page_screenshot_as_pdf(self, url: str, bid_number: str = "") -> str | None:
        """
        Navigate to `url` with the authenticated screenshot browser, capture
        a full-page screenshot in memory, convert it into a single-page PDF
        (via Pillow — preserves the exact on-screen visual), and write that
        PDF to a temp file.

        Returns the path to the temp PDF file, or None on failure.
        The caller is responsible for deleting the temp file after upload.
        """
        if not self._ensure_screenshot_browser():
            return None
        self._sync_cookies_to_browser()

        tmp_path = None
        try:
            self.logger.info(f"    [screenshot] navigating to {url}")
            self._screenshot_page.goto(url, wait_until="networkidle", timeout=60_000)

            # Give any lazy-loaded grids/widgets a moment to finish rendering.
            time.sleep(2)

            screenshot_bytes = self._screenshot_page.screenshot(full_page=True)

            image = Image.open(io.BytesIO(screenshot_bytes))
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name
            image.save(tmp_path, "PDF")

            self.logger.info(
                f"    [screenshot] captured & saved as PDF for {bid_number or url}: {tmp_path}"
            )
            return tmp_path

        except Exception as e:
            self.logger.error(f"    Screenshot capture failed for {bid_number or url}: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None

    def upload_tender_notice(self, detail_url: str, teb_number: str, mongo_id, bid_number: str = "") -> dict:
        """
        Capture a full-page screenshot of the bid's detail page, convert it
        to a PDF, upload it to S3 (using the SAME folder naming convention
        as `_upload_to_s3`, i.e. `{teb_number}_{mongo_id}`, so the screenshot
        lands alongside the bid's other documents), and clean up the temp file.

        Unlike the old version, this NEVER silently returns "nothing
        happened" — it always returns a status dict so the caller can still
        record a `documents` entry even when the upload itself fails (e.g.
        bad/missing AWS credentials). Only the screenshot capture step can
        prevent any record from being written, since without a PDF there is
        nothing to reference.

        Returns:
            {"s3_path": str | None, "error": str | None}
            - s3_path is set only on a fully successful upload.
            - error holds a short description of what went wrong otherwise
              (still useful to store in Mongo for later retry/auditing).
        """
        if not detail_url:
            msg = "no detail_url available"
            self.logger.warning(f"    [notice] {msg} for TEB={teb_number}, skipping screenshot")
            return {"s3_path": None, "error": msg}

        tmp_path = None
        try:
            tmp_path = self._take_full_page_screenshot_as_pdf(detail_url, bid_number)
            if not tmp_path:
                return {"s3_path": None, "error": "screenshot capture failed"}

            folder = f"{teb_number.replace('/', '_')}_{mongo_id}"
            file_name = "tender_notice.pdf"
            key = f"{self.base_folder}/{folder}/{file_name}"

            try:
                self.s3.upload_file(
                    tmp_path,
                    self.bucket,
                    key,
                    ExtraArgs={"ContentType": "application/pdf"},
                )
            except Exception as e:
                # AWS-side failure (bad/missing creds, bucket issue, etc.) —
                # we still have the screenshot, we just couldn't ship it to
                # S3. Report this back rather than swallowing it, so the
                # caller can still record the attempt in Mongo.
                self.logger.error(f"    Tender notice S3 upload failed: {e}")
                return {"s3_path": None, "error": str(e)}

            s3_path = f"s3://{self.bucket}/{key}"
            self.logger.info(f"    Tender notice PDF uploaded: {key}")
            return {"s3_path": s3_path, "error": None}

        except Exception as e:
            self.logger.error(f"    Tender notice processing failed: {e}")
            return {"s3_path": None, "error": str(e)}

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def close(self) -> None:
        """Cleanly shut down the persistent Playwright screenshot browser."""
        try:
            if self._screenshot_page:
                self._screenshot_page.close()
        except Exception as e:
            self.logger.debug(f"Error closing screenshot page: {e}")
        try:
            if self._browser:
                self._browser.close()
        except Exception as e:
            self.logger.debug(f"Error closing browser: {e}")
        try:
            if self._pw:
                self._pw.stop()
        except Exception as e:
            self.logger.debug(f"Error stopping playwright: {e}")
        self._screenshot_page = None
        self._browser = None
        self._browser_context = None
        self._pw = None
        self.logger.info("Playwright screenshot browser closed.")

    # -----------------------------------------------------------------
    # TEB ID generator
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
    # Process one row
    # -----------------------------------------------------------------
    def _process_row(self, row: dict, source_type: int, row_num: int, total: int | str) -> None:
        bid_id = row.get("bid_id", "")
        listing_bid_number = row.get("bid_number")
        self.logger.info(f"  [{row_num}/{total}] {listing_bid_number}  (bid_id={bid_id})")

        detail = self.fetch_detail(bid_id, source_type) if bid_id else {}
        self.logger.info(f"Detail documents: {detail.get('documents')}")

        # Prefer listing's clean bid number; otherwise extract from detail's number
        if listing_bid_number:
            bid_number_resolved = listing_bid_number
        else:
            raw_number = detail.get("number")
            if raw_number:
                match = re.match(r'^([A-Z0-9]+)', raw_number)
                bid_number_resolved = match.group(1) if match else raw_number
            else:
                bid_number_resolved = f"bid_{row_num}"

        title = row.get("title") or ""

        hash_id = generate_hash(bid_id or bid_number_resolved)
        teb_no = self._generate_teb_id()

        # Build contact_info as plain text without labels (each field on new line)
        contact_lines = []
        if detail.get("contact_name"):
            contact_lines.append(detail["contact_name"])
        if detail.get("contact_address"):
            contact_lines.append(detail["contact_address"])
        if detail.get("contact_phone"):
            contact_lines.append(detail["contact_phone"])
        if detail.get("contact_fax"):
            contact_lines.append(detail["contact_fax"])
        if detail.get("contact_email"):
            contact_lines.append(detail["contact_email"])
        contact_info = "\n".join(contact_lines) if contact_lines else None

        payload = {
            "hash_id": hash_id,
            "teb_number": teb_no,
            "bid_id": bid_id or None,
            "bid_number": bid_number_resolved,
            "title": title,
            "bid_type": row.get("bid_type"),
            "organization": row.get("organization"),
            "source_type": source_type,
            "source_label": self.source_types[source_type],
            "open_date": parse_date(row.get("open_date"), context="listing_open"),
            "close_date": parse_date(row.get("close_date"), context="listing_close"),
            "type": detail.get("type"),
            "status": detail.get("status"),
            "issue_datetime": parse_date(detail.get("issue_datetime"), context="detail_issue"),
            "close_datetime": parse_date(detail.get("close_datetime"), context="detail_close"),
            "question_cutoff": parse_date(detail.get("question_cutoff"), context="detail_question"),
            "description": detail.get("description"),
            "contact_info": contact_info,
            "documents": detail.get("documents", []),
            "questions": detail.get("questions", []),
            "participation_activities": detail.get("participation_activities", []),
            "detail_url": detail.get("detail_url"),
            "source": "Mansfield ISD IonWave eProcurement",
            "tender_notice_s3": None,
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
        }

        try:
            result = self.raw_col.insert_one(payload)
            self.logger.info(f"    ✓ Inserted (TEB: {teb_no})")
        except DuplicateKeyError:
            self.logger.info(f"    ⚠ Duplicate — skipping: {bid_number_resolved}")
            return

        # ── Real bid attachments scraped from the detail page ──
        if payload.get("documents"):
            self.logger.info(f"    Uploading {len(payload['documents'])} file(s) to S3 …")
            self._upload_to_s3(payload, result.inserted_id)

        # ── Tender notice: screenshot the detail page, save as PDF, upload ──
        # Runs unconditionally for every inserted record, using the same
        # mongo_id-based folder naming as the attachment upload above so
        # both end up in the same S3 prefix.
        notice_result = self.upload_tender_notice(
            payload.get("detail_url"),
            teb_no,
            result.inserted_id,
            bid_number_resolved,
        )
        notice_s3 = notice_result["s3_path"]

        # ALWAYS write a documents entry for the notice, even when the S3
        # upload failed (e.g. bad/missing AWS credentials). The record
        # carries an "upload_error" instead of an s3_path in that case, so
        # nothing is silently dropped and a later job can retry the upload
        # using the same detail_url.
        notice_doc = {
            "type": "tender_notice_screenshot",
            "title": "Tender Notice (Page Screenshot)",
            "original_url": payload.get("detail_url"),
            "s3_path": notice_s3,
            
            "uploaded_at": datetime.now(timezone.utc) if notice_s3 else None,
        }

        # Atomic update: set tender_notice_s3 and append the screenshot
        # doc to the documents array in one shot — no separate
        # find_one + manual append + full-array $set needed.
        update_result = self.raw_col.update_one(
            {"_id": result.inserted_id},
            {
                "$set": {"tender_notice_s3": notice_s3},
                "$push": {"documents": notice_doc},
            },
        )

        self.logger.info(
            f"Screenshot update -> matched={update_result.matched_count}, modified={update_result.modified_count}"
        )

        if notice_s3:
            self.logger.info(f"    [notice] screenshot uploaded for TEB={teb_no}")
        else:
            self.logger.warning(
                f"    [notice] upload failed for TEB={teb_no} (recorded in Mongo with error: {notice_result['error']})"
            )

    # -----------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------
    def scrape(self, source_types: list[int] | None = None) -> None:
        source_types = source_types or list(self.source_types.keys())
        try:
            for source_type in source_types:
                total_inserted = 0
                page1_html = self.prime_session(source_type)
                total_items, total_pages = self._fetch_total_pages(page1_html)
                self.logger.info(f"Plan: ~{total_items} records across {total_pages} page(s)")

                global_row_num = 0
                prev_html = page1_html

                for page in range(1, total_pages + 1):
                    self.logger.info("═" * 60)
                    self.logger.info(f"PAGE {page}/{total_pages}  —  fetching list …")
                    try:
                        rows, prev_html = self._fetch_list_page(page, source_type, prev_html)
                    except Exception as exc:
                        self.logger.error(f"Page {page} fetch failed: {exc}. Retrying …")
                        time.sleep(3)
                        try:
                            rows, prev_html = self._fetch_list_page(page, source_type, prev_html)
                        except Exception as exc2:
                            self.logger.error(f"Page {page} retry failed: {exc2}. Skipping.")
                            continue

                    if not rows:
                        self.logger.info(f"Page {page} returned 0 rows — stopping.")
                        break

                    self.logger.info(f"Page {page}: got {len(rows)} row(s). Processing now …")
                    for row in rows:
                        global_row_num += 1
                        try:
                            self._process_row(row, source_type, global_row_num, total_items or "?")
                            total_inserted += 1
                        except Exception as exc:
                            self.logger.error(f"  ✗ Row failed [{row.get('bid_number','?')}]: {exc}")

                    self.logger.info(f"Page {page} done. Rows this page: {len(rows)} | Grand total so far: {total_inserted}")
                    if page < total_pages:
                        delay = random.uniform(1.5, 3.0)
                        self.logger.info(f"Sleeping {delay:.1f}s before page {page + 1} …")
                        time.sleep(delay)

                self.logger.info("═" * 60)
                self.logger.info(f"{self.source_types[source_type]} done. Total records inserted: {total_inserted}")
        finally:
            # Always release the persistent screenshot browser, even on error.
            self.close()


if __name__ == "__main__":
    scraper = MansfieldISDScraper()
    scraper.scrape()