import hashlib
import json
import logging
import os
import re
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

    # curl_cffi sessions don't go through the urllib3 Retry adapter above,
    # so _get/_post implement their own retry loop using these.
    _RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
    _CF_CHALLENGE_MARKERS = (
        "Just a moment",
        "Checking your browser",
        "cf-error-details",
        "cf_chl_opt",
        "Attention Required",
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

    def _looks_like_cf_challenge(self, text: str | None) -> bool:
        if not text:
            return False
        return any(marker in text for marker in self._CF_CHALLENGE_MARKERS)

    def _request(self, method: str, url: str, max_attempts: int = 3, **kwargs) -> requests.Response:
        """
        GET/POST with retry + backoff baked in. Needed because curl_cffi's
        Session bypasses the urllib3 HTTPAdapter retry config entirely, so
        without this, any single transient failure (timeout, reset, a
        momentary 403/503/Cloudflare challenge) propagates straight up.
        """
        kwargs.setdefault("timeout", 30)
        if self._impersonate:
            kwargs.setdefault("impersonate", self._impersonate)

        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = getattr(self.session, method)(url, **kwargs)

                if resp.status_code in self._RETRYABLE_STATUS or self._looks_like_cf_challenge(getattr(resp, "text", None)):
                    reason = "challenge page" if self._looks_like_cf_challenge(getattr(resp, "text", None)) else f"HTTP {resp.status_code}"
                    self.logger.warning(f"{method.upper()} {url} → {reason} (attempt {attempt}/{max_attempts})")
                    last_exc = None
                    if attempt < max_attempts:
                        time.sleep(random.uniform(2, 4) * attempt)
                        continue

                resp.raise_for_status()
                return resp

            except Exception as exc:
                last_exc = exc
                self.logger.warning(f"{method.upper()} {url} raised {exc} (attempt {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    time.sleep(random.uniform(2, 4) * attempt)

        if last_exc:
            raise last_exc
        raise RuntimeError(f"{method.upper()} {url} failed after {max_attempts} attempts (non-2xx response)")

    def _get(self, url: str, **kwargs) -> requests.Response:
        return self._request("get", url, **kwargs)

    def _post(self, url: str, **kwargs) -> requests.Response:
        return self._request("post", url, **kwargs)

    def _polite_sleep(self) -> None:
        delay = random.uniform(*self.request_delay)
        self.logger.debug(f"Sleeping {delay:.2f}s …")
        time.sleep(delay)

    @abstractmethod
    def scrape(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════════
# CONSTANTS  (FWISD — Fort Worth ISD — same IonWave platform as Tarrant County)
# ══════════════════════════════════════════════════════════════════

BASE_URL = "https://fwisd.ionwave.net"
LISTING_URL = f"{BASE_URL}/SourcingEvents.aspx"
DETAIL_URL = f"{BASE_URL}/PublicDetail.aspx"

SOURCE_TYPES: dict[int, str] = {
    1: "open_bids",
    2: "closed_bids",
    3: "awarded_bids",
    4: "cancelled_bids",
}

BID_STATUS: dict[int, str] = {
    1: "open",
    2: "closed",
    3: "awarded",
    4: "cancelled",
}

_CONTENT_TYPE_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".zip": "application/zip",
}


# ══════════════════════════════════════════════════════════════════
# SCRAPER
# ══════════════════════════════════════════════════════════════════

class FWISDScraper(BaseScraper):
    LOGGER_NAME = "fwisd"

    def __init__(self) -> None:
        super().__init__()
        cf_clearance = os.getenv("FWISD_CF_CLEARANCE") or os.getenv("CF_CLEARANCE")
        if cf_clearance:
            self.session.cookies.set("cf_clearance", cf_clearance, domain="fwisd.ionwave.net")
            self.logger.info("cf_clearance cookie injected from environment.")

        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db = self.client["tender_bharo"]
        self.raw_col = self.db["fwisd_tenders"]
        self.meta_col = self.db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        self.bucket = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/fwisd"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION"),
        )
        self.debug_detail_saved = False

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
        url = f"{LISTING_URL}?SourceType={source_type}"
        self.logger.info("═" * 60)
        self.logger.info(f"Priming session for SourceType={source_type} ({SOURCE_TYPES[source_type]}) …")
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
        url = f"{LISTING_URL}?SourceType={source_type}"
        if page == 1:
            rows = self._parse_table_rows(prev_html, source_type)
            if rows:
                self.logger.info(f"DEBUG: First row on page 1: {rows[0]}")
            return rows, prev_html

        soup = BeautifulSoup(prev_html, "lxml")
        fields = self._extract_form_fields(soup)

        next_target = self._get_next_page_target(prev_html)
        if not next_target:
            self.logger.error("Could not find next page button, trying fallback constant 'ctl00$mainContent$rgBidList$ctl00$ctl03$ctl01$ctl08'")
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

        bid_id_map = self._extract_client_key_values(html)

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
        url = f"{DETAIL_URL}?bidID={bid_id}&SourceType={source_type}"
        self.logger.info(f"    Fetching detail bidID={bid_id} …")
        try:
            time.sleep(random.uniform(1, 2))
            resp = self._get(url, headers={"Referer": f"{LISTING_URL}?SourceType={source_type}"})
            if not self.debug_detail_saved:
                with open("debug_detail_fwisd.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                self.logger.info("DEBUG: Saved first detail HTML to debug_detail_fwisd.html")
                self.debug_detail_saved = True
            return self._parse_detail(resp.text, bid_id, url)
        except Exception as exc:
            self.logger.warning(f"    Detail fetch failed [{bid_id}] after retries: {exc}. Re-priming session and trying once more …")
            try:
                self.prime_session(source_type)
                time.sleep(random.uniform(1.5, 3))
                resp = self._get(url, headers={"Referer": f"{LISTING_URL}?SourceType={source_type}"})
                return self._parse_detail(resp.text, bid_id, url)
            except Exception as exc2:
                self.logger.error(f"    Detail fetch failed [{bid_id}] even after re-prime: {exc2}")
                return {}

    def _parse_detail(self, html: str, bid_id: str, detail_url: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        detail = {"bid_id": bid_id, "detail_url": detail_url}

        def get_text(elem_id: str) -> str | None:
            el = soup.find(id=elem_id)
            return clean_text(el.get_text()) if el else None

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

        detail["contact_name"] = get_text("ctl00_mainContent_lblName")
        # Clean address: replace newlines with spaces
        raw_address = get_text("ctl00_mainContent_lblAddress")
        if raw_address:
            detail["contact_address"] = re.sub(r"\s+", " ", raw_address).strip()
        else:
            detail["contact_address"] = None
        detail["contact_phone"] = get_text("ctl00_mainContent_lblPhone")
        detail["contact_fax"] = get_text("ctl00_mainContent_lblFax")
        email_el = soup.select_one("#ctl00_mainContent_lblEmail .__cf_email__")
        detail["contact_email"] = decode_cf_email(email_el["data-cfemail"]) if email_el and email_el.get("data-cfemail") else None

        detail["documents"] = self._extract_attachments(html, detail_url)
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
    # Document download (with retry + correct TLS impersonation)
    # -----------------------------------------------------------------
    def _download_with_retry(self, url: str, referer: str, max_attempts: int = 3) -> requests.Response | None:
        """Thin wrapper around the shared retrying _request(), returning None on
        total failure instead of raising (callers just skip/flag that document)."""
        try:
            return self._request(
                "get",
                url,
                max_attempts=max_attempts,
                headers={"Referer": referer, "Accept": "*/*"},
                timeout=60,
            )
        except Exception as exc:
            self.logger.warning(f"    Download failed (after retries) {url}: {exc}")
            return None

    # -----------------------------------------------------------------
    # S3 Upload
    # -----------------------------------------------------------------
    def _upload_to_s3(self, doc: dict, mongo_id) -> None:
        folder = f"{doc['teb_number'].replace('/', '_')}_{mongo_id}"
        updated = []
        for att in doc.get("documents", []):
            url = att.get("original_url")
            if not url:
                updated.append(att)
                continue

            r = self._download_with_retry(url, referer=doc.get("detail_url", LISTING_URL))
            if r is None:
                self.logger.warning(f"    Download failed (after retries): {url}")
                updated.append(att)
                time.sleep(random.uniform(0.3, 0.8))
                continue

            try:
                fname = re.sub(r"[^\w\-. ]", "_", att.get("title") or os.path.basename(urlparse(url).path) or "file")
                ext = os.path.splitext(fname)[-1].lower()
                content_type = _CONTENT_TYPE_MAP.get(ext, "application/octet-stream")
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
            time.sleep(random.uniform(0.5, 1.2))

        self.raw_col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})

    # -----------------------------------------------------------------
    # TEB ID generator (separate global counter key so it doesn't clash
    # with the Tarrant County scraper's counter)
    # -----------------------------------------------------------------
    def _generate_teb_id(self) -> str:
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id_fwisd"},
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
            "bid_status": BID_STATUS[source_type],
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
            "source": "FWISD (Fort Worth ISD) IonWave eProcurement",
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
        }

        try:
            result = self.raw_col.insert_one(payload)
            self.logger.info(f"    ✓ Inserted (TEB: {teb_no})")
        except DuplicateKeyError:
            self.logger.info(f"    ⚠ Duplicate — skipping: {bid_number_resolved}")
            return

        if payload.get("documents"):
            self.logger.info(f"    Uploading {len(payload['documents'])} file(s) to S3 …")
            self._upload_to_s3(payload, result.inserted_id)

        self._polite_sleep()

    # -----------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------
    def scrape(self, source_types: list[int] | None = None) -> None:
        source_types = source_types or list(SOURCE_TYPES.keys())
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
            self.logger.info(f"{SOURCE_TYPES[source_type]} done. Total records inserted: {total_inserted}")


if __name__ == "__main__":
    FWISDScraper().scrape()