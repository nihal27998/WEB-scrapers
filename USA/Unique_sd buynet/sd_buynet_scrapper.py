import hashlib
import logging
import os
import random
import re
import tempfile
import time

import boto3
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from pymongo import MongoClient, ReturnDocument
from requests.adapters import HTTPAdapter, Retry
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()


class Scraper:

    # ---------------- INIT ----------------
    def __init__(self):

        self.BASE_URL    = "https://sdbuynet.sandiegocounty.gov"
        self.LISTING_URL = "https://sdbuynet.sandiegocounty.gov/page.aspx/en/rfp/request_browse_public"
        self.AJAX_URL    = "https://sdbuynet.sandiegocounty.gov/ajax.aspx/en/rfp/request_browse_public"

        self.SESSION = requests.Session()

        retries = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.SESSION.mount("https://", adapter)
        self.SESSION.mount("http://",  adapter)

        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer":         self.LISTING_URL,
        })

        # ---------------- DB ----------------
        self.client          = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["sd_buynet_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        # ---------------- S3 ----------------
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/sd_buynet"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name           = os.getenv("AWS_REGION"),
        )

        # ---------------- LOG ----------------
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s"
        )
        self.logger = logging.getLogger("SD_BUYNET")

        self._common_doc_urls: set = set()
        self._page1_soup    = None
        self._hidden_fields = {}

    # ------------------------------------------------------------------ #
    #  TEB / HASH / HELPERS
    # ------------------------------------------------------------------ #

    def generate_teb_number(self):
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
                     7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    def generate_hash(self, rfx_number, rfx_name, lot_number):
        raw = "|".join([
            self.clean_text(rfx_number),
            self.clean_text(rfx_name),
            self.clean_text(str(lot_number)),
        ])
        return hashlib.md5(raw.encode()).hexdigest()

    def clean_text(self, text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", str(text)).strip()

    def parse_date(self, value):
        value = self.clean_text(value)
        if not value:
            return ""
        try:
            return dateutil_parser.parse(value, dayfirst=False)
        except Exception:
            return value

    # ------------------------------------------------------------------ #
    #  HIDDEN FIELD EXTRACTION
    # ------------------------------------------------------------------ #

    def _extract_hidden_fields(self, soup) -> dict:
        fields = {}
        for inp in soup.find_all("input", attrs={"type": "hidden"}):
            name  = inp.get("name", "")
            value = inp.get("value", "") or ""
            if name:
                fields[name] = value
        return fields

    # ------------------------------------------------------------------ #
    #  PAGINATION METADATA
    # ------------------------------------------------------------------ #

    def _detect_total_pages(self, soup) -> int:
        inp = soup.find("input", {"id": "maxpageindexbody_x_grid_grd"})
        if inp:
            try:
                return int(inp.get("value", "0")) + 1
            except ValueError:
                pass

        max_page = 1
        for btn in soup.select("ul.pager.buttons li button"):
            txt = self.clean_text(btn.get_text())
            if txt.isdigit():
                max_page = max(max_page, int(txt))
        return max_page

    # ------------------------------------------------------------------ #
    #  AJAX PAGINATION
    # ------------------------------------------------------------------ #

    def _fetch_ajax_page(self, page_index: int) -> BeautifulSoup | None:
        post_data = dict(self._hidden_fields)
        post_data["hdnCurrentPageIndexbody_x_grid_grd"] = str(page_index)
        post_data["body:x:grid:upgrid"] = "GoToPage"
        post_data["x_updpnl"]           = "body:x:grid:upgrid"
        post_data["__EVENTTARGET"]       = "body_x_grid_grd"
        post_data["__EVENTARGUMENT"]     = f"Page${page_index}"

        headers = {
            "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer":          self.LISTING_URL,
        }

        try:
            resp = self.SESSION.post(
                self.AJAX_URL, data=post_data, headers=headers, timeout=60
            )
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            self.logger.error(f"AJAX page {page_index} failed: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  LISTING – FULL PAGINATION
    # ------------------------------------------------------------------ #

    def fetch_all_solicitations(self):
        all_tenders = []
        seen_rfx    = set()

        self.logger.info("Fetching listing page 1 (GET)…")
        try:
            resp = self.SESSION.get(self.LISTING_URL, timeout=60)
            resp.raise_for_status()
        except Exception as e:
            self.logger.error(f"Failed to load listing page 1: {e}")
            return all_tenders

        soup = BeautifulSoup(resp.text, "lxml")
        self._page1_soup    = soup
        self._hidden_fields = self._extract_hidden_fields(soup)

        total_pages = self._detect_total_pages(soup)
        self.logger.info(f"Total pages detected: {total_pages}")

        rows = self.extract_listing_rows(soup)
        for r in rows:
            key = (r["rfx_number"], r["lot_number"])
            if key not in seen_rfx:
                seen_rfx.add(key)
                all_tenders.append(r)

        self.logger.info(f"Page 1 (GET): {len(rows)} rows")

        for page_index in range(1, total_pages):
            self.logger.info(f"Fetching page {page_index + 1} (AJAX, index={page_index})…")
            time.sleep(random.uniform(1.0, 2.5))

            frag_soup = self._fetch_ajax_page(page_index)
            if frag_soup is None:
                self.logger.warning(f"No response for page index {page_index} – stopping.")
                break

            new_hidden = self._extract_hidden_fields(frag_soup)
            if new_hidden:
                self._hidden_fields.update(new_hidden)

            rows = self.extract_listing_rows(frag_soup)
            if not rows:
                self.logger.warning(f"No rows in AJAX fragment for page index {page_index} – stopping.")
                break

            added = 0
            for r in rows:
                key = (r["rfx_number"], r["lot_number"])
                if key not in seen_rfx:
                    seen_rfx.add(key)
                    all_tenders.append(r)
                    added += 1

            self.logger.info(f"Page {page_index + 1}: {len(rows)} rows, {added} new unique")

            if added == 0:
                self.logger.warning("No new rows – possible loop, stopping.")
                break

        self.logger.info(f"Total unique solicitations: {len(all_tenders)}")
        return all_tenders

    # ------------------------------------------------------------------ #
    #  LISTING – ROW PARSE
    # ------------------------------------------------------------------ #
    # Confirmed column layout (16 cells):
    #   [00] detail link + rfx name snippet
    #   [01] rfx_number (e.g. BPM013149)
    #   [02] rfx_name
    #   [03] lot_number
    #   [04] round_number
    #   [05] commodities (combined)
    #   [06] commodities (first only)
    #   [07] begin_date
    #   [08] end_date
    #   [09] remaining_time
    #   [10] status
    #   [11] empty
    #   [12] empty
    #   [13] empty
    #   [14] bid_abstract link (filename as text, present only when available)
    #   [15] empty
    # ------------------------------------------------------------------ #

    def _cell(self, cells, idx):
        return self.clean_text(cells[idx].get_text(" ", strip=True)) if idx < len(cells) else ""

    def parse_listing_row(self, row):
        cells = row.find_all("td")
        if len(cells) < 3:
            return None

        first_link = cells[0].select_one("a[href]")
        if not first_link:
            return None
        href       = first_link.get("href", "").strip()
        detail_url = urljoin(self.BASE_URL, href) if href else ""

        m = re.search(r"/(\d+)$", detail_url)
        process_id = m.group(1) if m else ""

        rfx_number   = self._cell(cells, 1)
        rfx_name     = self._cell(cells, 2)
        lot_number   = self._cell(cells, 3)
        round_number = self._cell(cells, 4)
        commodities  = self._cell(cells, 5)
        begin_date   = self._cell(cells, 7)
        end_date     = self._cell(cells, 8)
        remaining    = self._cell(cells, 9)
        status       = self._cell(cells, 10)

        # FIX: bid_abstract_url is at index 14 (confirmed by diagnostics).
        # Also scan all cells as a fallback in case the column ever shifts.
        bid_abstract_url = ""
        if len(cells) > 14:
            link = cells[14].select_one("a[href]")
            if link:
                href14 = link.get("href", "").strip()
                bid_abstract_url = urljoin(self.BASE_URL, href14) if href14 else ""

        # Fallback: search every cell for any link that looks like a file download
        if not bid_abstract_url:
            for cell in cells[11:]:          # skip the known non-link cells
                link = cell.select_one("a[href]")
                if link:
                    lhref = link.get("href", "")
                    if "download" in lhref.lower() or "fil/" in lhref.lower():
                        bid_abstract_url = urljoin(self.BASE_URL, lhref.strip())
                        break

        if not rfx_number:
            return None

        return {
            "process_id":       process_id,
            "rfx_number":       rfx_number,
            "rfx_name":         rfx_name,
            "lot_number":       lot_number,
            "round_number":     round_number,
            "commodities":      commodities,
            "begin_date_raw":   begin_date,
            "begin_date":       self.parse_date(begin_date),
            "end_date_raw":     end_date,
            "end_date":         self.parse_date(end_date),
            "remaining_time":   remaining,
            "status":           status,
            "bid_abstract_url": bid_abstract_url,
            "detail_url":       detail_url,
        }

    def extract_listing_rows(self, soup):
        rows = []
        for table in soup.find_all("table"):
            headers = [self.clean_text(th.get_text()).lower()
                       for th in table.select("th")]
            if any(h in ("rfx #", "rfx name", "rfx#") for h in headers):
                for row in table.select("tbody tr"):
                    parsed = self.parse_listing_row(row)
                    if parsed:
                        rows.append(parsed)
                break

        if not rows:
            for row in soup.select("tr[id^='body_x_grid_grd_tr_']"):
                parsed = self.parse_listing_row(row)
                if parsed:
                    rows.append(parsed)

        self.logger.info(f"Rows found on this page: {len(rows)}")
        return rows

    # ------------------------------------------------------------------ #
    #  COMMON DOC DISCOVERY
    # ------------------------------------------------------------------ #

    def _discover_common_docs(self, solicitations: list):
        sample = [s for s in solicitations if s.get("detail_url")][:2]
        if len(sample) < 2:
            self.logger.info("Not enough detail pages to detect common docs – skipping.")
            return

        url_sets = []
        for s in sample:
            self.logger.info(f"Sampling for common docs: {s['detail_url']}")
            try:
                resp = self.SESSION.get(s["detail_url"], timeout=60)
                soup = BeautifulSoup(resp.text, "lxml")
                docs = self._extract_documents(soup, s["detail_url"])
                url_sets.append({d["original_url"] for d in docs if d.get("original_url")})
                time.sleep(random.uniform(0.8, 1.5))
            except Exception as e:
                self.logger.warning(f"Common-doc discovery failed for {s['detail_url']}: {e}")

        if len(url_sets) == 2:
            self._common_doc_urls = url_sets[0] & url_sets[1]
            if self._common_doc_urls:
                self.logger.info(
                    f"Common docs identified ({len(self._common_doc_urls)}): "
                    + ", ".join(self._common_doc_urls)
                )
            else:
                self.logger.info("No common docs found across sampled pages.")

    # ------------------------------------------------------------------ #
    #  DETAIL PAGE
    # ------------------------------------------------------------------ #

    def fetch_detail_page(self, detail_url: str):
        try:
            resp = self.SESSION.get(detail_url, timeout=60)
            if resp.status_code != 200:
                self.logger.error(f"Detail {detail_url} → {resp.status_code}")
                return {}
            soup = BeautifulSoup(resp.text, "lxml")
            return self._parse_detail_soup(soup, detail_url)
        except Exception as e:
            self.logger.error(f"Detail fetch failed [{detail_url}]: {e}")
            return {}

    # ------------------------------------------------------------------ #
    #  DETAIL PAGE PARSER  — rewritten for actual page structure
    #
    #  The page uses <td class="iv-phc-cell top aligned"> elements where
    #  each cell contains BOTH the label and value as inline text, e.g.:
    #
    #    <td class="iv-phc-cell top aligned">
    #      Contact Cassandra Jackson-Grijalva, Procurement Specialist
    #      Cell: 858-414-2500 | Email: ...
    #    </td>
    #
    #    <td class="iv-phc-cell top aligned">
    #      Summary                         ← empty summary, just the label
    #    </td>
    #
    #  Q&A dates are NOT in the visible cells — they live in hidden inputs:
    #    <input type="text" name="..._rfp_qa_start_date_...">  value="6/4/2026"
    #    <input type="text" name="..._rfp_qa_end_date_...">    value="7/6/2026"
    #  Times come from the first <select> inside the same <fieldset>/<legend>.
    # ------------------------------------------------------------------ #

    def _parse_detail_soup(self, soup, detail_url: str) -> dict:
        detail = {
            "summary":           "",
            "contact":           "",
            "qa_start_date_raw": "",
            "qa_start_date":     "",
            "qa_end_date_raw":   "",
            "qa_end_date":       "",
            "documents":         [],
        }

        # ── 1. summary & contact from iv-phc-cell tds ──────────────────
        #
        # Each relevant td looks like:
        #   "Summary"                    → label only, value is empty
        #   "Contact <name> Cell: ..."   → label + value concatenated
        #
        # We split on the first keyword and take whatever follows as value.
        # ────────────────────────────────────────────────────────────────
        for td in soup.find_all("td", class_=lambda c: c and "iv-phc-cell" in c):
            text = self.clean_text(td.get_text(" ", strip=True))

            # Summary — label may stand alone or prefix a value
            if not detail["summary"] and re.match(r"^summary\b", text, re.I):
                # Strip the leading "Summary" word; the rest is the value
                value = re.sub(r"^summary\s*[:\-]?\s*", "", text, flags=re.I).strip()
                detail["summary"] = value   # may be "" if no text follows

            # Contact — label always prefixes the contact text
            if not detail["contact"] and re.match(r"^contact\b", text, re.I):
                value = re.sub(r"^contact\s*[:\-]?\s*", "", text, flags=re.I).strip()
                detail["contact"] = value

        # ── 2. Q&A dates from hidden <input type="text"> ───────────────
        #
        # Input name patterns (confirmed):
        #   ..._rfp_qa_start_date_20230213220504001
        #   ..._rfp_qa_end_date_20230213220504002
        #
        # Time comes from the first <select> inside the same <fieldset>.
        # ────────────────────────────────────────────────────────────────
        for inp in soup.find_all("input", attrs={"type": "text"}):
            name  = inp.get("name", "")
            value = self.clean_text(inp.get("value", ""))
            if not value:
                continue

            is_start = "_rfp_qa_start_date_" in name
            is_end   = "_rfp_qa_end_date_"   in name

            if not is_start and not is_end:
                continue

            # Try to find the time from the nearest <select> sibling.
            # The input and its select share a <fieldset> or a parent div.
            time_str = ""
            parent = inp.find_parent(["fieldset", "div", "td"])
            if parent:
                sel = parent.find("select")
                if sel:
                    # First option with a non-empty value = default/selected time
                    for opt in sel.find_all("option"):
                        opt_val = self.clean_text(opt.get("value") or opt.get_text())
                        if opt_val and opt_val not in ("", "Delete all values."):
                            time_str = opt_val
                            break

            raw = f"{value} {time_str}".strip()

            if is_start and not detail["qa_start_date_raw"]:
                detail["qa_start_date_raw"] = raw
                detail["qa_start_date"]     = self.parse_date(raw)

            if is_end and not detail["qa_end_date_raw"]:
                detail["qa_end_date_raw"] = raw
                detail["qa_end_date"]     = self.parse_date(raw)

        # ── Debug log ──────────────────────────────────────────────────
        self.logger.info(
            f"[DETAIL] {detail_url} | "
            f"summary={'YES' if detail['summary'] else 'EMPTY'} , "
            f"contact={'YES' if detail['contact'] else 'NO'} , "
            f"qa_start={detail['qa_start_date_raw'] or 'NO'} , "
            f"qa_end={detail['qa_end_date_raw'] or 'NO'}"
        )

        detail["documents"] = self._extract_documents(soup, detail_url)
        return detail

    # ------------------------------------------------------------------ #
    #  DOCUMENTS EXTRACTION
    # ------------------------------------------------------------------ #

    _JUNK_TITLE_PATTERNS = [
        re.compile(r"rfx general information", re.I),
        re.compile(r"rfx documents selected title type att\.", re.I),
        re.compile(r"^selected title type att\.", re.I),
    ]

    def _is_junk_title(self, title: str) -> bool:
        return any(pat.search(title) for pat in self._JUNK_TITLE_PATTERNS)

    def _extract_documents(self, soup, detail_url: str) -> list:
        documents = []
        seen_urls = set()

        # Find the documents table — the one with Title / Type / Att. headers
        # and actual per-file rows (not the giant merged row).
        # Confirmed as Table [14] in diagnostics: 10 rows, proper per-file layout.
        doc_table = None
        for table in soup.find_all("table"):
            ths = [self.clean_text(th.get_text()).lower() for th in table.select("th")]
            if "att." in ths and "title" in ths and "type" in ths:
                # Pick the table whose first tbody row has a manageable cell count
                tbody_rows = table.select("tbody tr")
                if tbody_rows:
                    first_row_cells = tbody_rows[0].find_all("td")
                    # The correct table has ~7 cells per row, not 40+
                    if len(first_row_cells) <= 10:
                        doc_table = table
                        break

        if not doc_table:
            # Fallback: any table with att. + title headers
            for table in soup.find_all("table"):
                ths = [self.clean_text(th.get_text()).lower() for th in table.select("th")]
                if "att." in ths or ("title" in ths and "type" in ths):
                    doc_table = table
                    break

        if not doc_table:
            return documents

        # Build column index map from headers
        header_row = doc_table.select_one("thead tr") or doc_table.find("tr")
        col_map    = {}
        if header_row:
            for idx, th in enumerate(header_row.find_all(["th", "td"])):
                col_map[self.clean_text(th.get_text()).lower()] = idx

        title_idx = col_map.get("title", 0)
        att_idx   = col_map.get("att.",  2)

        for row in doc_table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            title = self.clean_text(
                cells[title_idx].get_text(" ", strip=True)
            ) if title_idx < len(cells) else ""

            if self._is_junk_title(title):
                continue

            doc_url = ""
            # Try att. column first
            if att_idx < len(cells):
                link = cells[att_idx].select_one("a[href]")
                if link:
                    href = link.get("href", "").strip()
                    doc_url = urljoin(self.BASE_URL, href) if href else ""

            # Fall back to title column
            if not doc_url and title_idx < len(cells):
                link = cells[title_idx].select_one("a[href]")
                if link:
                    href = link.get("href", "").strip()
                    doc_url = urljoin(self.BASE_URL, href) if href else ""

            # Last resort: any link in the row that looks like a download
            if not doc_url:
                for cell in cells:
                    link = cell.select_one("a[href]")
                    if link:
                        href = link.get("href", "")
                        if "download" in href.lower() or "fil/" in href.lower():
                            doc_url = urljoin(self.BASE_URL, href.strip())
                            break

            if not doc_url or doc_url in seen_urls:
                continue
            seen_urls.add(doc_url)

            documents.append({
                "type":         "tender_document",
                "title":        title,
                "original_url": doc_url,
                "s3_path":      None,
                "uploaded_at":  None,
            })

        if self._common_doc_urls:
            before    = len(documents)
            documents = [d for d in documents
                         if d.get("original_url") not in self._common_doc_urls]
            filtered  = before - len(documents)
            if filtered:
                self.logger.debug(f"Filtered {filtered} common doc(s) from {detail_url}")

        return documents

    # ------------------------------------------------------------------ #
    #  S3 UPLOAD
    # ------------------------------------------------------------------ #

    def upload_to_s3(self, doc: dict, mongo_id):
        if not self.bucket:
            self.logger.warning("S3_BUCKET_NAME not configured – skipping")
            return

        folder       = f"{doc['teb_number'].replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in doc.get("documents", []):
            try:
                url = d.get("original_url")
                if not url:
                    updated_docs.append(d)
                    continue

                with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
                    tmp_path = tmp.name

                try:
                    resp = self.SESSION.get(url, timeout=120, stream=True)
                    if resp.status_code != 200:
                        self.logger.warning(f"Doc download [{resp.status_code}]: {url}")
                        updated_docs.append(d)
                        continue

                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    title = d.get("title") or os.path.basename(urlparse(url).path)
                    title = re.sub(r"[^\w\-. ]", "_", title)
                    ext   = os.path.splitext(urlparse(url).path)[1].lower()
                    if ext and not title.lower().endswith(ext):
                        title += ext

                    key = f"{self.base_folder}/{folder}/{title}"

                    content_type_map = {
                        ".pdf":  "application/pdf",
                        ".xls":  "application/vnd.ms-excel",
                        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ".doc":  "application/msword",
                        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ".zip":  "application/zip",
                    }
                    content_type = content_type_map.get(ext, "application/octet-stream")

                    with open(tmp_path, "rb") as f:
                        self.s3.put_object(
                            Bucket=self.bucket,
                            Key=key,
                            Body=f,
                            ContentType=content_type,
                        )

                    d["s3_path"]     = f"s3://{self.bucket}/{key}"
                    d["uploaded_at"] = datetime.now(timezone.utc)
                    self.logger.info(f"Uploaded: {key}")

                finally:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)

            except Exception as e:
                self.logger.error(f"S3 upload failed [{d.get('original_url')}]: {e}")
                d["s3_path"]     = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}}
        )

    # ------------------------------------------------------------------ #
    #  MAIN SCRAPE
    # ------------------------------------------------------------------ #

    def scrape(self):
        total = 0
        try:
            self.logger.info(f"Starting: {self.LISTING_URL}")
            self.SESSION.get(self.LISTING_URL, timeout=30)

            solicitations = self.fetch_all_solicitations()
            if not solicitations:
                self.logger.warning("No solicitations found.")
                return

            self._discover_common_docs(solicitations)

            for data in solicitations:
                try:
                    hash_id = self.generate_hash(
                        data["rfx_number"],
                        data["rfx_name"],
                        data["lot_number"],
                    )

                    if self.raw_collection.find_one({"hash_id": hash_id}):
                        self.logger.info(f"Skipped duplicate: {data['rfx_number']}")
                        continue

                    detail = {}
                    if data.get("detail_url"):
                        self.logger.info(f"Detail → {data['detail_url']}")
                        detail = self.fetch_detail_page(data["detail_url"])
                        time.sleep(random.uniform(0.8, 2.0))

                    teb_no = self.generate_teb_number()

                    tender = {
                        "hash_id":           hash_id,
                        "teb_number":        teb_no,
                        "source":            "SD_BUYNET",
                        "process_id":        data["process_id"],
                        "rfx_number":        data["rfx_number"],
                        "rfx_name":          data["rfx_name"],
                        "tender_subject":    data["rfx_name"],
                        "lot_number":        data["lot_number"],
                        "round_number":      data["round_number"],
                        "commodities":       data["commodities"],
                        "begin_date_raw":    data["begin_date_raw"],
                        "begin_date":        data["begin_date"],
                        "end_date_raw":      data["end_date_raw"],
                        "end_date":          data["end_date"],
                        "remaining_time":    data["remaining_time"],
                        "status":            data["status"],
                        "bid_abstract_url":  data["bid_abstract_url"],
                        "detail_url":        data["detail_url"],
                        "summary":           detail.get("summary",           ""),
                        "contact":           detail.get("contact",           ""),
                        "qa_start_date_raw": detail.get("qa_start_date_raw", ""),
                        "qa_start_date":     detail.get("qa_start_date",     ""),
                        "qa_end_date_raw":   detail.get("qa_end_date_raw",   ""),
                        "qa_end_date":       detail.get("qa_end_date",       ""),
                        "documents":         detail.get("documents",         []),
                        "etl_status":        "pending",
                        "created_at":        datetime.now(timezone.utc),
                    }

                    res = self.raw_collection.insert_one(tender)

                    if tender.get("documents"):
                        self.upload_to_s3(tender, res.inserted_id)

                    total += 1
                    self.logger.info(
                        f"Inserted: {data['rfx_number']} | {data['rfx_name'][:80]}"
                    )

                except Exception as e:
                    self.logger.error(
                        f"Tender processing failed [{data.get('rfx_number')}]: {e}"
                    )

            self.logger.info(f"Total inserted: {total}")

        except Exception as e:
            self.logger.error(f"Scraper failed: {e}")
        finally:
            self.client.close()


# ------------------------------------------------------------------ #
#  RUN
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    Scraper().scrape()