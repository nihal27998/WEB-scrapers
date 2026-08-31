import hashlib
import io
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


def parse_date(raw: str | None, context: str = "") -> datetime | None:
    if not raw:
        return None
    cleaned = re.sub(r'\s*\([A-Z]{2,4}\)\s*', '', raw)
    cleaned = re.sub(r'\s+[A-Z]{2,4}\s*$', '', cleaned).strip()
    if re.search(r'POSTPONED|TBD|N/A', cleaned, re.I):
        return None
    try:
        dt = dateutil_parser.parse(cleaned, dayfirst=False)
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

    def __init__(self, use_curl_cffi: bool = True, impersonate: str = "chrome124",
                 request_delay: tuple[float, float] = (0.8, 1.8)):
        self.logger = self._build_logger()
        self.request_delay = request_delay
        self._impersonate: str | None = None
        self.session = self._build_session(use_curl_cffi, impersonate)

    def _build_logger(self) -> logging.Logger:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status is not None:
            return status
        match = re.search(r"\b(\d{3})\b", str(exc))
        return int(match.group(1)) if match else None

    def _get_with_retry(self, url: str, max_retries: int = 4,
                        base_delay: float = 8.0, **kwargs) -> requests.Response:
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
                self.logger.warning(f"    429 on {url} (attempt {attempt}/{max_retries}) — backing off {delay:.1f}s")
                time.sleep(delay)

    def _polite_sleep(self) -> None:
        delay = random.uniform(*self.request_delay)
        self.logger.debug(f"Sleeping {delay:.2f}s")
        time.sleep(delay)

    @abstractmethod
    def scrape(self) -> None:
        pass


# ══════════════════════════════════════════════════════════════════
# MONTGOMERY COUNTY MD SCRAPER
# ══════════════════════════════════════════════════════════════════

class MontgomeryCountyScraper(BaseScraper):
    """
    Scrapes formal solicitations from Montgomery County, MD.

    Listing  : https://apps.montgomerycountymd.gov/prosolicitation/SolicitationsAndBids.aspx?type=Formal
    Detail   : https://apps.montgomerycountymd.gov/prosolicitation/SolicitationsAndBidsDetails.aspx?type=Formal&id=<id>
    """

    LOGGER_NAME = "montgomery_county"

    BASE_APP_URL = "https://apps.montgomerycountymd.gov/prosolicitation"
    LISTING_URL  = f"{BASE_APP_URL}/SolicitationsAndBids.aspx"
    DETAIL_URL   = f"{BASE_APP_URL}/SolicitationsAndBidsDetails.aspx"

    # ── Construction ──────────────────────────────────────────────────────────
    def __init__(self) -> None:
        super().__init__()

        # MongoDB
        self.client  = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db      = self.client["tender_bharo"]
        self.raw_col = self.db["montgomery_county_notice"]
        self.meta_col = self.db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        # S3
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/montgomery_county"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name           = os.getenv("AWS_REGION"),
        )

        self.debug_listing_saved = False
        self.debug_detail_saved  = False

        # Playwright handles (lazily initialised)
        self._pw               = None
        self._browser          = None
        self._browser_context  = None
        self._screenshot_page  = None

    # ── Listing ───────────────────────────────────────────────────────────────

    def _fetch_listing(self, sol_type: str = "Formal") -> list[dict]:
        url = f"{self.LISTING_URL}?type={sol_type}"
        self.logger.info(f"Fetching listing: {url}")
        resp = self._get_with_retry(url, headers={"Referer": "https://www.montgomerycountymd.gov/"})
        if not self.debug_listing_saved:
            with open("debug_mc_listing.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            self.logger.info("DEBUG: saved listing HTML → debug_mc_listing.html")
            self.debug_listing_saved = True
        return self._parse_listing(resp.text, sol_type)

    def _parse_listing(self, html: str, sol_type: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        rows_out: list[dict] = []

        table = soup.find("table", class_="myTable")
        if not table:
            self.logger.warning("myTable not found — check debug_mc_listing.html")
            return rows_out

        data_rows = table.find_all("tr")[1:]  # skip header row

        for tr in data_rows:
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue

            # ── Col 0: solicitation number + detail URL ──────────────────────
            sol_cell = cells[0]
            a_tag    = sol_cell.find("a", href=True)
            if not a_tag:
                continue

            sol_number  = clean_text(a_tag.get_text())     # e.g. "RFP # 1194188"
            detail_href = a_tag["href"]                     # relative URL
            detail_url  = urljoin(self.LISTING_URL, detail_href)
            id_match = re.search(r'id=(\d+)', detail_href, re.I)
            row_id = id_match.group(1) if id_match else None

            # ── Col 1: title, amendments, pre-submission conference ───────────
            desc_cell = cells[1]

            title_tag = desc_cell.find("b")
            title = clean_text(title_tag.get_text()) if title_tag else None
            if title:
                title = re.sub(r'\s*This solicitation is hosted on BidNet\*?\s*', '', title).strip()

            amendment_tag = desc_cell.find("font", color="red")
            amendments = clean_text(amendment_tag.get_text()) if amendment_tag else None
            if amendments:
                amendments = amendments.strip("[]").strip()

            pre_conf = None
            pre_b = desc_cell.find("b", string=re.compile(r"Pre.?(Submission|Bid)", re.I))
            if pre_b:
                raw = ""
                for sib in pre_b.next_siblings:
                    t = getattr(sib, "get_text", lambda: str(sib))()
                    raw += t
                    if raw.strip():
                        break
                pre_conf = clean_text(raw)

            # ── Col 2 (index 2): hidden scope cell ───────────────────────────
            scope_cell = cells[2]

            scope_html = scope_cell.decode_contents()
            scope_soup = BeautifulSoup(scope_html, "lxml")
            for a in scope_soup.find_all("a"):
                a.decompose()
            scope_text = clean_text(scope_soup.get_text(" "))

            documents: list[dict] = []
            seen_urls: set[str] = set()
            for a in scope_cell.find_all("a", href=True):
                href = a["href"]
                abs_url = urljoin(self.LISTING_URL, href)
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)
                link_text = clean_text(a.get_text()) or os.path.basename(urlparse(abs_url).path)
                if "soldesc.asp" in href:
                    abs_url = abs_url.replace(
                        "soldesc.asp?type=Formal&",
                        f"{self.BASE_APP_URL}/SolicitationsAndBidsDetails.aspx?type=Formal&"
                    )
                documents.append({
                    "type": "Tender_document",
                    "title": link_text,
                    "original_url": abs_url,
                    "s3_path": None,
                    "uploaded_at": None,
                })

            # ── Col 4 (index 4): closing date ────────────────────────────────
            closing_raw = clean_text(cells[4].get_text())

            rows_out.append({
                "row_id":          row_id,
                "solicitation_number": sol_number,
                "title":           title,
                "amendments":      amendments,
                "pre_submission_conference": pre_conf,
                "scope_text":      scope_text,
                "documents":       documents,
                "closing_date_raw": closing_raw,
                "detail_url":      detail_url,
                "sol_type":        sol_type,
            })

        self.logger.info(f"Listing parsed: {len(rows_out)} solicitation(s)")
        return rows_out

    # ── Detail page ───────────────────────────────────────────────────────────

    def fetch_detail(self, row: dict) -> dict:
        detail_url = row.get("detail_url")
        sol_number = row.get("solicitation_number", "")
        self.logger.info(f"    Fetching detail: {detail_url}")
        try:
            time.sleep(random.uniform(1.5, 3.0))
            resp = self._get_with_retry(detail_url, headers={"Referer": self.LISTING_URL})
            if not self.debug_detail_saved:
                with open("debug_mc_detail.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                self.logger.info("DEBUG: saved detail HTML → debug_mc_detail.html")
                self.debug_detail_saved = True
            return self._parse_detail(resp.text, sol_number, detail_url)
        except Exception as exc:
            self.logger.error(f"    Detail fetch failed [{sol_number}]: {exc}")
            return {}

    def _parse_detail(self, html: str, sol_number: str, detail_url: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        detail: dict = {"solicitation_number": sol_number, "detail_url": detail_url}

        def _sibling_value(label_pattern: str) -> str | None:
            tag = soup.find(string=re.compile(label_pattern, re.I))
            if not tag:
                return None
            parent = tag.find_parent(["td", "th", "label", "div", "span"])
            if not parent:
                return None
            nxt = parent.find_next_sibling("td")
            if nxt:
                return clean_text(nxt.get_text("\n"))
            row = parent.find_parent("tr")
            if row:
                tds = row.find_all("td")
                if len(tds) >= 2:
                    return clean_text(tds[-1].get_text("\n"))
            return None

        detail["bid_opening_date"]  = _sibling_value(r"Bid\s*Opening")
        detail["rfp_closing_date"]  = (
            _sibling_value(r"RFP\s*Closing")
            or _sibling_value(r"IFB\s*Closing")
            or _sibling_value(r"Closing\s*Date")
        )
        detail["title"]             = _sibling_value(r"^Title$") or _sibling_value(r"Solicitation\s*Title")
        detail["amendments"]        = _sibling_value(r"Amendments?")
        detail["pre_submission_conference"] = (
            _sibling_value(r"Pre.?Submission\s*Conference")
            or _sibling_value(r"Pre.?Bid\s*(Meeting|Conference)")
        )
        detail["contacts"]          = _sibling_value(r"^Contacts?$") or _sibling_value(r"Contact\s*Info")
        detail["description"]       = (
            _sibling_value(r"^Scope$")
            or _sibling_value(r"Scope\s*of\s*(Work|Services)")
            or _sibling_value(r"^Description$")
        )

        detail["documents"] = self._extract_detail_attachments(html, detail_url)

        return detail

    def _extract_detail_attachments(self, html: str, detail_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        attachments: list[dict] = []
        seen: set[str] = set()

        FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|zip|pptx?|txt|csv)(\?|$)", re.I)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not (FILE_EXT.search(href) or "download" in href.lower() or "soldesc" in href.lower()):
                continue
            abs_url = urljoin(detail_url, href)
            if "soldesc.asp" in abs_url:
                abs_url = abs_url.replace(
                    "soldesc.asp?type=Formal&",
                    f"{self.BASE_APP_URL}/SolicitationsAndBidsDetails.aspx?type=Formal&"
                )
            if abs_url in seen:
                continue
            seen.add(abs_url)
            attachments.append({
                "type": "Tender_document",
                "title": clean_text(a.get_text()) or os.path.basename(urlparse(abs_url).path),
                "original_url": abs_url,
                "s3_path": None,
                "uploaded_at": None,
            })

        return attachments

    # ── S3 — document upload ──────────────────────────────────────────────────

    def _upload_to_s3(self, payload: dict, mongo_id) -> None:
        folder = f"{payload['teb_number'].replace('/', '_')}_{mongo_id}"
        updated: list[dict] = []

        for att in payload.get("documents", []):
            url = att.get("original_url")
            if not url:
                updated.append(att)
                continue
            try:
                r = self.session.get(
                    url,
                    headers={"Referer": payload.get("detail_url", self.LISTING_URL), "Accept": "*/*"},
                    timeout=60,
                )
                if r.status_code != 200:
                    self.logger.warning(f"    Download failed {url}: HTTP {r.status_code}")
                    updated.append(att)
                    continue

                fname = re.sub(
                    r"[^\w\-. ]", "_",
                    att.get("title") or os.path.basename(urlparse(url).path) or "file"
                )
                ext = os.path.splitext(fname)[-1].lower()
                content_type = {
                    ".pdf":  "application/pdf",
                    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ".xls":  "application/vnd.ms-excel",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".doc":  "application/msword",
                    ".zip":  "application/zip",
                }.get(ext, "application/octet-stream")

                key = f"{self.base_folder}/{folder}/{fname}"
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=r.content, ContentType=content_type)
                att["s3_path"]    = f"s3://{self.bucket}/{key}"
                att["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"    S3 ✓ {fname}")

            except Exception as exc:
                self.logger.error(f"    S3 error {url}: {exc}")
                att["s3_path"]    = None
                att["uploaded_at"] = None

            updated.append(att)
            time.sleep(random.uniform(0.3, 0.8))

        current = self.raw_col.find_one({"_id": mongo_id}, {"documents": 1}) or {}
        current_docs = current.get("documents", [])
        updated_by_url = {d.get("original_url"): d for d in updated if d.get("original_url")}
        merged: list[dict] = []
        for d in current_docs:
            u = d.get("original_url")
            merged.append(updated_by_url.pop(u, d) if u in updated_by_url else d)
        merged.extend(updated_by_url.values())
        self.raw_col.update_one({"_id": mongo_id}, {"$set": {"documents": merged}})

    # ── Tender notice — Playwright screenshot → PDF → S3 ─────────────────────

    def _ensure_screenshot_browser(self) -> bool:
        if self._browser_context:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error("playwright not installed — pip install playwright && playwright install chromium")
            return False
        try:
            self.logger.info("Launching Playwright browser for tender-notice screenshots…")
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
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
        if not self._browser_context:
            return
        pw_cookies: list[dict] = []
        try:
            for c in self.session.cookies:
                name  = getattr(c, "name", None)
                value = getattr(c, "value", None)
                domain = (getattr(c, "domain", "") or "apps.montgomerycountymd.gov").lstrip(".")
                path  = getattr(c, "path", "/") or "/"
                if name and value is not None:
                    pw_cookies.append({"name": name, "value": value, "domain": domain, "path": path})
        except TypeError:
            try:
                for name, value in dict(self.session.cookies).items():
                    pw_cookies.append({"name": name, "value": value,
                                       "domain": "apps.montgomerycountymd.gov", "path": "/"})
            except Exception as e:
                self.logger.debug(f"Cookie sync error: {e}")
        if pw_cookies:
            try:
                self._browser_context.add_cookies(pw_cookies)
            except Exception as e:
                self.logger.debug(f"Could not sync cookies: {e}")

    def _take_full_page_screenshot_as_pdf(self, url: str, sol_number: str = "") -> str | None:
        """
        Navigate to the detail page and screenshot ONLY the content from the
        top of the page through the bottom of the "Scope" row.

        Two layers of defense, both anchored on the stable ASP.NET control id
        `divCellFormalScope` (present on every Formal solicitation detail page):

          1. Remove every <tr> that comes after the Scope row from the DOM
             (covers Bid Holders, the malformed "Bid Holder For" stray text,
             "Link to Solicitation", and the Contact Option form).
          2. Resize the viewport to end exactly at the Scope row's bottom
             edge, then take a normal (non full-page) screenshot. This is a
             hard physical boundary — even if step 1 misses something, it
             cannot appear in the captured image because it's outside the
             captured area entirely.
        """
        if not self._ensure_screenshot_browser():
            return None
        self._sync_cookies_to_browser()

        tmp_path = None
        try:
            self.logger.info(f"    [screenshot] navigating to {url}")
            self._screenshot_page.goto(url, wait_until="networkidle", timeout=60_000)

            # Step 1: remove junk rows (defense in depth — doesn't need to be perfect)
            self._screenshot_page.evaluate("""
                () => {
                    const remove = (el) => el && el.parentNode && el.parentNode.removeChild(el);
                    const scopeCell = document.querySelector('[id*="Scope"]');
                    const scopeRow = scopeCell ? scopeCell.closest('tr') : null;
                    if (scopeRow) {
                        let sibling = scopeRow.nextElementSibling;
                        while (sibling) {
                            const next = sibling.nextElementSibling;
                            if (sibling.tagName === 'TR') remove(sibling);
                            sibling = next;
                        }
                    }
                    document.querySelectorAll('[id*="DownloadOption"], [id*="BidHolders"]')
                        .forEach(el => remove(el.closest('tr') || el));
                    document.querySelectorAll('table').forEach(table => {
                        let node = table.previousSibling;
                        while (node && node.nodeType === Node.TEXT_NODE) {
                            const prev = node.previousSibling;
                            if (node.textContent.trim()) node.remove();
                            node = prev;
                        }
                    });
                    window.scrollTo(0, 0);
                }
            """)
            self._screenshot_page.wait_for_timeout(300)

            # Step 2: hard clip boundary at the bottom of the Scope row.
            clip_bottom = self._screenshot_page.evaluate("""
                () => {
                    const scopeCell = document.querySelector('[id*="Scope"]');
                    const scopeRow = scopeCell ? scopeCell.closest('tr') : null;
                    if (!scopeRow) return null;
                    const rect = scopeRow.getBoundingClientRect();
                    return window.scrollY + rect.bottom;
                }
            """)
            self.logger.info(f"    [debug] clip_bottom={clip_bottom!r} for {sol_number}")

            if clip_bottom:
                target_height = int(clip_bottom) + 20  # small padding
                self._screenshot_page.set_viewport_size({"width": 1280, "height": target_height})
                self._screenshot_page.wait_for_timeout(200)
                screenshot_bytes = self._screenshot_page.screenshot(full_page=False)
            else:
                self.logger.warning(f"    [screenshot] Scope row not found, using full page for {sol_number}")
                screenshot_bytes = self._screenshot_page.screenshot(full_page=True)

            image = Image.open(io.BytesIO(screenshot_bytes))
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name
            image.save(tmp_path, "PDF")

            self.logger.info(f"    [screenshot] captured as PDF for {sol_number or url}")
            return tmp_path

        except Exception as e:
            self.logger.error(f"    Screenshot capture failed for {sol_number or url}: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None

    def upload_tender_notice(self, detail_url: str, teb_number: str,
                             mongo_id, sol_number: str = "") -> dict:
        if not detail_url:
            msg = "no detail_url available"
            self.logger.warning(f"    [notice] {msg} for TEB={teb_number}")
            return {"s3_path": None, "error": msg}

        tmp_path = None
        try:
            tmp_path = self._take_full_page_screenshot_as_pdf(detail_url, sol_number)
            if not tmp_path:
                return {"s3_path": None, "error": "screenshot capture failed"}

            folder = f"{teb_number.replace('/', '_')}_{mongo_id}"
            key    = f"{self.base_folder}/{folder}/tender_notice.pdf"

            try:
                self.s3.upload_file(tmp_path, self.bucket, key,
                                    ExtraArgs={"ContentType": "application/pdf"})
            except Exception as e:
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
        for attr, label, method in [
            ("_screenshot_page", "page",       "close"),
            ("_browser",         "browser",    "close"),
            ("_pw",              "playwright", "stop"),
        ]:
            obj = getattr(self, attr, None)
            if obj:
                try:
                    getattr(obj, method)()
                except Exception as e:
                    self.logger.debug(f"Error closing {label}: {e}")
                setattr(self, attr, None)
        self._browser_context = None
        self.logger.info("Playwright screenshot browser closed.")

    # ── TEB ID generator ──────────────────────────────────────────────────────

    def _generate_teb_id(self) -> str:
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
                     7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # ── Process one listing row ───────────────────────────────────────────────

    def _process_row(self, row: dict, row_num: int, total: int | str) -> None:
        sol_number = row.get("solicitation_number", f"sol_{row_num}")
        self.logger.info(f"  [{row_num}/{total}] {sol_number}")

        # Fetch detail page
        detail = self.fetch_detail(row)

        # Prefer detail-page values; fall back to listing values
        title      = detail.get("title")      or row.get("title")      or ""
        amendments = detail.get("amendments") or row.get("amendments")
        pre_conf   = (detail.get("pre_submission_conference")
                      or row.get("pre_submission_conference"))
        description = detail.get("description") or row.get("scope_text")
        contacts    = detail.get("contacts")

        # Merge document lists (listing docs first, then any extras from detail)
        listing_docs = row.get("documents", [])
        detail_docs  = detail.get("documents", [])
        all_doc_urls = {d["original_url"] for d in listing_docs}
        merged_docs  = listing_docs + [d for d in detail_docs if d["original_url"] not in all_doc_urls]

        hash_id = generate_hash(sol_number)

        # ── FIX: don't skip-and-return on duplicates. Previously, an
        # existing hash_id caused an immediate `return` here, which meant
        # upload_tender_notice() (and therefore the screenshot logic) was
        # NEVER called again for any solicitation already in MongoDB —
        # including across every code change made after the first scrape.
        # Now: reuse the existing record + TEB number, refresh its fields,
        # and ALWAYS regenerate the notice screenshot below.
        existing = self.raw_col.find_one({"hash_id": hash_id})

        bid_opening_date = parse_date(detail.get("bid_opening_date"), "detail_opening")
        rfp_closing_date = parse_date(detail.get("rfp_closing_date"), "detail_closing")
        closing_date      = parse_date(row.get("closing_date_raw"), "listing_close")

        if existing:
            mongo_id = existing["_id"]
            teb_no   = existing["teb_number"]
            self.logger.info(f"    ↻ Already exists (TEB: {teb_no}) — refreshing record & notice")

            self.raw_col.update_one(
                {"_id": mongo_id},
                {"$set": {
                    "title":                 title,
                    "amendments":            amendments,
                    "pre_submission_conference": pre_conf,
                    "description":           description,
                    "contacts":              contacts,
                    "closing_date_raw":      row.get("closing_date_raw"),
                    "closing_date":          closing_date,
                    "bid_opening_date":      bid_opening_date,
                    "rfp_closing_date":      rfp_closing_date,
                    "detail_url":            row.get("detail_url"),
                }},
            )

            if merged_docs:
                self.logger.info(f"    Uploading {len(merged_docs)} file(s) to S3 …")
                self._upload_to_s3({
                    "teb_number": teb_no,
                    "detail_url": row.get("detail_url"),
                    "documents":  merged_docs,
                }, mongo_id)

        else:
            teb_no = self._generate_teb_id()
            payload = {
                "hash_id":               hash_id,
                "teb_number":            teb_no,
                "solicitation_number":   sol_number,
                "row_id":                row.get("row_id"),
                "bid_type":              "RFP" if sol_number.startswith("RFP") else
                                         "IFB" if sol_number.startswith("IFB") else
                                         "REOI" if sol_number.startswith("REOI") else "Unknown",
                "title":                 title,
                "amendments":            amendments,
                "pre_submission_conference": pre_conf,
                "description":           description,
                "contacts":              contacts,
                "closing_date_raw":      row.get("closing_date_raw"),
                "closing_date":          closing_date,
                "bid_opening_date":      bid_opening_date,
                "rfp_closing_date":      rfp_closing_date,
                "documents":             merged_docs,
                "detail_url":            row.get("detail_url"),
                "source":                "Montgomery County MD Office of Procurement",
                "sol_type":              row.get("sol_type", "Formal"),
                "tender_notice_s3":      None,
                "etl_status":            "pending",
                "created_at":            datetime.now(timezone.utc),
            }

            try:
                result = self.raw_col.insert_one(payload)
                mongo_id = result.inserted_id
                self.logger.info(f"    ✓ Inserted (TEB: {teb_no})")
            except DuplicateKeyError:
                # Race condition: another process inserted it between our
                # find_one() and insert_one(). Re-fetch and treat as existing.
                existing = self.raw_col.find_one({"hash_id": hash_id})
                if not existing:
                    raise
                mongo_id = existing["_id"]
                teb_no = existing["teb_number"]
                self.logger.info(f"    ↻ Race on insert — using existing (TEB: {teb_no})")

            if payload.get("documents"):
                self.logger.info(f"    Uploading {len(payload['documents'])} file(s) to S3 …")
                self._upload_to_s3(payload, mongo_id)

        # ── Capture tender notice screenshot → PDF → S3.
        # This now runs on EVERY call to _process_row, new or existing,
        # so the screenshot logic is always actually exercised.
        notice_result = self.upload_tender_notice(
            row.get("detail_url"), teb_no, mongo_id, sol_number
        )
        notice_s3 = notice_result["s3_path"]

        notice_doc = {
            "type":         "tender_notice_screenshot",
            "title":        "Tender Notice (Page Screenshot)",
            "original_url": row.get("detail_url"),
            "s3_path":      notice_s3,
            "uploaded_at":  datetime.now(timezone.utc) if notice_s3 else None,
        }

        # Remove any previous notice screenshot entry, then push the fresh one.
        # (Two calls because Mongo disallows $pull and $push on the same
        # array path within a single update_one.)
        self.raw_col.update_one(
            {"_id": mongo_id},
            {
                "$set": {"tender_notice_s3": notice_s3},
                "$pull": {"documents": {"type": "tender_notice_screenshot"}},
            },
        )
        upd = self.raw_col.update_one(
            {"_id": mongo_id},
            {"$push": {"documents": notice_doc}},
        )
        self.logger.info(
            f"    Screenshot update → matched={upd.matched_count}, modified={upd.modified_count}"
        )
        if notice_s3:
            self.logger.info(f"    [notice] screenshot uploaded for TEB={teb_no}")
        else:
            self.logger.warning(
                f"    [notice] upload failed for TEB={teb_no} (error: {notice_result['error']})"
            )

    # ── Main entry point ──────────────────────────────────────────────────────

    def scrape(self, sol_type: str = "Formal") -> None:
        try:
            rows = self._fetch_listing(sol_type)
            total = len(rows)
            self.logger.info(f"Total solicitations to process: {total}")

            total_processed = 0
            for row_num, row in enumerate(rows, start=1):
                try:
                    self._process_row(row, row_num, total)
                    total_processed += 1
                except Exception as exc:
                    self.logger.error(f"  ✗ Row failed [{row.get('solicitation_number','?')}]: {exc}")
                self._polite_sleep()

            self.logger.info("═" * 60)
            self.logger.info(f"Scrape complete. Total records processed: {total_processed}")

        finally:
            self.close()


if __name__ == "__main__":
    scraper = MontgomeryCountyScraper()
    scraper.scrape()