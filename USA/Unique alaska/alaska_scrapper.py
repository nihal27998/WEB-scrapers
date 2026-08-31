

import os
import re
import time
import random
import logging
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import boto3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from pymongo import MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

try:
    from dateutil import parser as dateparser
except ImportError:
    dateparser = None

from dotenv import load_dotenv
load_dotenv()


DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rtf")

# ASP.NET attachment-handler URL patterns that DON'T carry a file
# extension in the href itself (extension only shows up in the
# response's Content-Disposition header when actually downloaded).
DOC_HANDLER_PATTERNS = re.compile(
    r"(Download\.aspx|GetFile\.aspx|DownloadAttachment\.aspx|"
    r"FileHandler\.ashx|GetDocument\.aspx|ViewAttachment\.aspx)",
    re.I,
)
DOC_QUERY_PARAM_HINTS = ("attachmentid", "fileid", "docid", "documentid")

# If you've confirmed the real column order from --debug-rows output,
# set it here to bypass all heuristics, e.g.:
#   FORCE_COLUMN_ORDER = ["summary", "posted_date_raw", "expires_date_raw"]
FORCE_COLUMN_ORDER = None


class AlaskaOnlinePublicNoticesScraper:

    BASE_URL = "https://aws.state.ak.us/OnlinePublicNotices"
    SEARCH_URL = f"{BASE_URL}/Notices/Search.aspx"
    VIEW_URL = f"{BASE_URL}/Notices/View.aspx"

    DEFAULT_SEARCH_BUTTON_NAME = "ctl00$contentMain$btnSearch"
    STATUS_OPTIONS = ("Active", "Archived")

    # Site's raw status -> your internal bid_status vocabulary.
    # notice_status keeps the raw site value untouched; bid_status is
    # the normalized one used across your pipelines.
    STATUS_MAP = {
        "Active": "Open",
        "Archived": "Closed",
    }

    def __init__(self, min_delay: float = 1.5, max_delay: float = 3.5,
                 timeout: int = 30, debug_rows: bool = False):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.debug_rows = debug_rows

        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        })

        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db = self.client["tender_bharo"]
        self.raw_collection = self.db["alaska_opn_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        self.bucket = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/alaska_opn_bids"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION"),
        )

        logging.basicConfig(
            level=logging.DEBUG if debug_rows else logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("Alaska_OPN_Scraper")

    def _sleep(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def generate_teb_id(self):
        counter = self.meta_collection.find_one_and_update(
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

    def generate_hash(self, notice_id) -> str:
        return hashlib.md5(str(notice_id).encode()).hexdigest()

    def parse_date(self, date_val):
        try:
            if not date_val or not dateparser:
                return None
            dt = dateparser.parse(str(date_val).strip(), dayfirst=False)
            return dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            self.logger.error(f"Date parse failed: {date_val!r} | {e}")
            return None

    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        folder = f"{teb_number.replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in documents:
            try:
                url = d.get("original_url")
                if not url:
                    updated_docs.append(d)
                    continue

                response = self.session.get(url, timeout=60, allow_redirects=True)
                if response.status_code != 200:
                    self.logger.warning(f"Could not download document: {url} (HTTP {response.status_code})")
                    updated_docs.append(d)
                    continue

                # Prefer the filename from Content-Disposition (handles
                # Download.aspx?id=... style URLs that have no extension).
                cd = response.headers.get("Content-Disposition", "")
                cd_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
                if cd_match:
                    title = cd_match.group(1).strip()
                else:
                    title = d.get("title") or os.path.basename(urlparse(url).path) or "document"

                title = re.sub(r"[^\w\-. ]", "_", title)
                if not any(title.lower().endswith(ext) for ext in
                           [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rtf"]):
                    # Guess extension from Content-Type if still missing.
                    ctype = response.headers.get("Content-Type", "").lower()
                    ext_map = {
                        "pdf": ".pdf", "msword": ".doc",
                        "wordprocessingml": ".docx", "ms-excel": ".xls",
                        "spreadsheetml": ".xlsx", "zip": ".zip",
                    }
                    matched_ext = next((v for k, v in ext_map.items() if k in ctype), ".pdf")
                    title += matched_ext

                key = f"{self.base_folder}/{folder}/{title}"
                content_type = response.headers.get("Content-Type", "application/pdf")

                self.s3.put_object(
                    Bucket=self.bucket, Key=key,
                    Body=response.content, ContentType=content_type,
                )
                d["s3_path"] = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"Uploaded to S3: {key}")

            except Exception as e:
                self.logger.error(f"S3 upload failed for {d.get('original_url')}: {e}")
                d["s3_path"] = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        self.raw_collection.update_one({"_id": mongo_id}, {"$set": {"documents": updated_docs}})
        return updated_docs

    # ------------------------------------------------------------------
    # ASP.NET WebForms helpers
    # ------------------------------------------------------------------
    def _collect_form_state(self, soup: BeautifulSoup, form_id: str = "formMain") -> dict:
        form = soup.find("form", id=form_id) or soup.find("form")
        if not form:
            raise RuntimeError("Could not find the ASP.NET form on the page.")

        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in ("submit", "button", "image", "file"):
                continue
            if itype in ("checkbox", "radio"):
                if inp.has_attr("checked"):
                    data[name] = inp.get("value", "on")
                continue
            data[name] = inp.get("value", "")

        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            chosen = sel.find("option", selected=True) or sel.find("option")
            data[name] = chosen.get("value", chosen.get_text(strip=True)) if chosen else ""

        for ta in form.find_all("textarea"):
            name = ta.get("name")
            if name:
                data[name] = ta.get_text()

        return data

    def _find_search_button_name(self, soup: BeautifulSoup) -> str:
        for inp in soup.find_all("input", type=re.compile("submit", re.I)):
            value = (inp.get("value") or "").strip().lower()
            if value == "search" and inp.get("name"):
                return inp["name"]
        for btn in soup.find_all("button"):
            if btn.get_text(strip=True).lower() == "search" and btn.get("name"):
                return btn["name"]
        self.logger.warning(
            "Could not auto-detect the Search button's form field name; "
            "falling back to %s.", self.DEFAULT_SEARCH_BUTTON_NAME,
        )
        return self.DEFAULT_SEARCH_BUTTON_NAME

    def _find_status_field_name(self, soup: BeautifulSoup) -> str:
        select = soup.find("select", id="contentMain_ddlStatus")
        if select and select.get("name"):
            return select["name"]
        return "ctl00$contentMain$ddlStatus"

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    def search(self, status: str) -> list:
        self.logger.info("Loading search page for status=%s ...", status)
        resp = self.session.get(self.SEARCH_URL, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        payload = self._collect_form_state(soup)
        payload[self._find_status_field_name(soup)] = status
        payload["__EVENTTARGET"] = ""
        payload["__EVENTARGUMENT"] = ""

        search_button_name = self._find_search_button_name(soup)
        payload[search_button_name] = "Search"

        self._sleep()
        self.logger.info("Submitting search (status=%s) ...", status)
        resp = self.session.post(
            self.SEARCH_URL,
            data=payload,
            timeout=self.timeout,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.SEARCH_URL,
                "Origin": "https://aws.state.ak.us",
            },
        )
        resp.raise_for_status()

        results_soup = BeautifulSoup(resp.text, "lxml")

        if re.search(r"more than\s+\d+\s+results were found", resp.text, re.I):
            self.logger.warning(
                "[%s] Result set was capped by the site. Narrow with a date "
                "range or department filter for full coverage.", status,
            )

        results = self.parse_results(results_soup, status)
        self.logger.info("[%s] Parsed %d notice row(s).", status, len(results))
        return results

    @staticmethod
    def _clean_cell(text):
        if text is None:
            return None
        text = text.replace("\xa0", " ").strip()
        return text or None

    def _get_grid_headers(self, soup: BeautifulSoup, sample_row) -> list:
        """
        Try to read the actual <th> header labels from the grid this row
        belongs to, so column mapping is based on real labels
        ("Posted", "Expires", ...) instead of position guessing.
        Returns a lowercased list of header labels, or [] if not found.
        """
        table = sample_row.find_parent("table") if sample_row else None
        if table is None:
            return []
        header_row = table.find("tr")
        if header_row is None:
            return []
        ths = header_row.find_all(["th"])
        if not ths:
            # Some RadGrids use a styled <td> header row instead of <th>.
            return []
        return [self._clean_cell(th.get_text()) or "" for th in ths]

    def _split_row_cells(self, row, title: str) -> dict:
        """
        Maps a result row's <td> cells onto named fields.

        Strategy, in order of preference:
          1. FORCE_COLUMN_ORDER, if you've hard-set it after inspecting
             real --debug-rows output.
          2. Header-label matching (looks for "Summary"/"Description",
             "Posted"/"Publish", "Expire"/"Deadline" in <th> text).
          3. Structural fallback: find the <td> that actually CONTAINS
             the title <a> (not a string-equality guess) and drop
             everything up to and including it; treat the remaining
             cells positionally as [summary, posted, expires].
        """
        tds = row.find_all("td")
        cell_texts = [self._clean_cell(td.get_text()) for td in tds]

        result = {"summary": None, "posted_date_raw": None, "expires_date_raw": None}

        if FORCE_COLUMN_ORDER:
            for field, val in zip(FORCE_COLUMN_ORDER, cell_texts):
                if field in result:
                    result[field] = val
            if self.debug_rows:
                self.logger.debug("FORCE_COLUMN_ORDER mapping -> %s | raw=%s", result, cell_texts)
            return result

        headers = self._get_grid_headers(BeautifulSoup(""), row)  # parent table lookup
        if headers:
            header_map = {}
            for idx, h in enumerate(headers):
                hl = h.lower()
                if "summ" in hl or "descrip" in hl:
                    header_map["summary"] = idx
                elif "post" in hl or "publish" in hl:
                    header_map["posted_date_raw"] = idx
                elif "expir" in hl or "deadline" in hl or "archive" in hl:
                    header_map["expires_date_raw"] = idx
            if header_map:
                for field, idx in header_map.items():
                    if idx < len(cell_texts):
                        result[field] = cell_texts[idx]
                if self.debug_rows:
                    self.logger.debug(
                        "Header-based mapping -> headers=%s map=%s result=%s raw=%s",
                        headers, header_map, result, cell_texts,
                    )
                return result

        # --- structural fallback ---
        title_td_index = None
        for i, td in enumerate(tds):
            if td.find("a", href=re.compile(r"View\.aspx\?id=\d+", re.I)):
                title_td_index = i
                break

        if title_td_index is None:
            # Couldn't even find the title cell structurally; bail out
            # rather than silently mis-assigning fields.
            self.logger.warning("Could not locate title cell in row structurally; row_cells=%s", cell_texts)
            return result

        remaining = cell_texts[title_td_index + 1:]
        if len(remaining) >= 1:
            result["summary"] = remaining[0]
        if len(remaining) >= 2:
            result["posted_date_raw"] = remaining[1]
        if len(remaining) >= 3:
            result["expires_date_raw"] = remaining[2]

        if self.debug_rows:
            self.logger.debug(
                "Structural fallback mapping -> title_td_index=%d result=%s raw=%s",
                title_td_index, result, cell_texts,
            )
        return result

    def parse_results(self, soup: BeautifulSoup, status: str) -> list:
        seen_ids = set()
        results = []

        for a in soup.find_all("a", href=re.compile(r"View\.aspx\?id=\d+", re.I)):
            href = a["href"]
            qs = parse_qs(urlparse(href).query)
            notice_id = qs.get("id", [None])[0]
            if not notice_id or notice_id in seen_ids:
                continue
            seen_ids.add(notice_id)

            title = a.get_text(strip=True)
            row = a.find_parent("tr")
            if row is None:
                self.logger.warning("Notice %s link has no parent <tr>; skipping row-field extraction.", notice_id)
                results.append({
                    "notice_id": notice_id, "title": title, "status": status,
                    "view_url": urljoin(self.BASE_URL + "/Notices/", href),
                    "row_cells": [], "summary": None,
                    "posted_date_raw": None, "expires_date_raw": None,
                })
                continue

            cell_texts = [td.get_text(strip=True) for td in row.find_all("td")]
            mapped = self._split_row_cells(row, title)

            results.append({
                "notice_id": notice_id,
                "title": title,
                "status": status,
                "view_url": urljoin(self.BASE_URL + "/Notices/", href),
                "row_cells": cell_texts,
                **mapped,
            })

        return results

    # ------------------------------------------------------------------
    # detail page
    # ------------------------------------------------------------------
    def fetch_detail(self, notice_id: str) -> dict:
        url = f"{self.VIEW_URL}?id={notice_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout, headers={"Referer": self.SEARCH_URL})
            resp.raise_for_status()
        except requests.RequestException as e:
            self.logger.error("Failed to fetch detail for notice %s: %s", notice_id, e)
            return {}

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_detail_html(soup, notice_id, url)

    def _is_document_link(self, href: str) -> bool:
        if href.lower().endswith(DOC_EXTENSIONS):
            return True
        if DOC_HANDLER_PATTERNS.search(href):
            return True
        qs = parse_qs(urlparse(href).query)
        if any(p in qs for p in DOC_QUERY_PARAM_HINTS):
            return True
        return False

    @staticmethod
    def _text_or_none(soup, elem_id):
        el = soup.find(id=elem_id)
        if el is None:
            return None
        txt = el.get_text(" ", strip=True)
        return txt or None

    def _parse_detail_html(self, soup: BeautifulSoup, notice_id: str, url: str) -> dict:
        """
        Confirmed against a real View.aspx response (notice 224171):
        every field we need is server-rendered with a stable element id,
        so we read those directly instead of guessing containers.

            #contentMain_lblTitle          -> notice title
            #contentMain_lblBody           -> notice body (the actual
                                               content, no sidebar/menu
                                               noise mixed in)
            #notice_attachments a[href]    -> attachment links, e.g.
                                               "Attachment.aspx?id=162494"
                                               (NOTE: no file extension in
                                               the href - the real
                                               filename only appears in
                                               the link text and in the
                                               Content-Disposition header
                                               on download)
            #contentMain_lblDepartment     -> Department
            #contentMain_lblCategory       -> Category
            #contentMain_lblSubCategory    -> Sub-Category
            #contentMain_lblLocations      -> Location(s)
            #contentMain_lblProjectNumber  -> Project/Regulation #
            #contentMain_lblPublishDate    -> Publish Date
            #contentMain_lblArchiveDate    -> Archive Date
            .noticeEvents li               -> Events/Deadlines (free text
                                               per <li>, includes a map
                                               link we strip out)
            #notice_revisions table        -> Created/Modified history
        """
        title = self._text_or_none(soup, "contentMain_lblTitle")
        if not title and soup.title and soup.title.string:
            title = re.sub(r"\s*-\s*Alaska Online Public Notices\s*$", "", soup.title.string.strip())
        if not title:
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content", "").strip()

        body_el = soup.find(id="contentMain_lblBody")
        body_text = body_el.get_text("\n", strip=True) if body_el else None
        if body_text is None:
            # Fall back to the older heuristic only if the exact id is
            # ever absent (e.g. a differently-templated notice type).
            fallback = soup.find(id="opn_viewnotice") or soup.find(id="contentMain_pnlNotice") or soup.body
            body_text = fallback.get_text("\n", strip=True) if fallback else None
            if self.debug_rows:
                self.logger.debug("notice %s: #contentMain_lblBody not found, used fallback container", notice_id)

        # --- Attachments: scope to #notice_attachments, not the whole
        # page, so we don't accidentally pick up unrelated nav/footer
        # links. Hrefs look like "Attachment.aspx?id=162494" - no file
        # extension, so the saved filename comes from the link text
        # (and is re-confirmed from Content-Disposition at download
        # time in upload_to_s3).
        documents = []
        seen_urls = set()
        attach_container = soup.find(id="notice_attachments")
        link_source = attach_container.find_all("a", href=True) if attach_container else []
        if not link_source:
            # Defensive fallback: scan the whole page for anything that
            # looks like a document link, in case the id ever changes.
            link_source = [a for a in soup.find_all("a", href=True) if self._is_document_link(a["href"])]

        for a in link_source:
            href = a["href"].strip()
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            full_url = urljoin(url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            documents.append({
                "type": "Tender_document",
                "title": a.get_text(strip=True) or href.rsplit("/", 1)[-1],
                "original_url": full_url,
                "s3_path": None,
                "uploaded_at": None,
            })

        # --- Structured Details panel ---
        details = {
            "department": self._text_or_none(soup, "contentMain_lblDepartment"),
            "category": self._text_or_none(soup, "contentMain_lblCategory"),
            "sub_category": self._text_or_none(soup, "contentMain_lblSubCategory"),
            "locations": self._text_or_none(soup, "contentMain_lblLocations"),
            "project_number": self._text_or_none(soup, "contentMain_lblProjectNumber"),
            "publish_date_raw": self._text_or_none(soup, "contentMain_lblPublishDate"),
            "archive_date_raw": self._text_or_none(soup, "contentMain_lblArchiveDate"),
        }

        # --- Events/Deadlines: one entry per <li>, drop the embedded
        # "View on Map" link, keep the label + datetime text.
        events = []
        events_cell = soup.find("td", class_=re.compile(r"noticeEvents"))
        if events_cell:
            for li in events_cell.find_all("li"):
                li_copy = BeautifulSoup(str(li), "lxml")
                map_link = li_copy.find("a", class_="map")
                if map_link:
                    map_link.decompose()
                text = li_copy.get_text(" ", strip=True)
                events.append(text)

        # --- Revision history ---
        created_by_raw = self._text_or_none(soup, "contentMain_lblCreatedBy")
        revision_history = []
        rev_table = soup.find(id="notice_revisions")
        if rev_table:
            for row in rev_table.find_all("tr"):
                txt = row.get_text(" ", strip=True)
                if txt:
                    revision_history.append(txt)

        if self.debug_rows:
            self.logger.debug(
                "Detail page %s -> title=%r body_chars=%s found_docs=%d details=%s events=%d",
                notice_id, title,
                len(body_text) if body_text else 0,
                len(documents), details, len(events),
            )
            if not documents:
                all_attach_links = (
                    [a["href"] for a in attach_container.find_all("a", href=True)]
                    if attach_container else "NO #notice_attachments CONTAINER FOUND"
                )
                self.logger.debug("No document links matched. Raw attachment links seen: %s", all_attach_links)

        return {
            "detail_title": title,
            "detail_page_url": url,
            "body_text": body_text,
            "documents": documents,
            "details": details,
            "events": events,
            "created_by_raw": created_by_raw,
            "revision_history": revision_history,
        }

    # ------------------------------------------------------------------
    # duplicate handling
    # ------------------------------------------------------------------
    def _retry_missing_uploads(self, hash_id: str, notice_id: str):
        existing = self.raw_collection.find_one({"hash_id": hash_id})
        if not existing:
            return

        # Backfill bid_status on older records inserted before this
        # field existed, or where it's out of sync with notice_status.
        existing_status = existing.get("notice_status")
        expected_bid_status = self.STATUS_MAP.get(existing_status, existing_status)
        if existing.get("bid_status") != expected_bid_status:
            self.raw_collection.update_one(
                {"_id": existing["_id"]}, {"$set": {"bid_status": expected_bid_status}},
            )
            self.logger.info(
                "Notice %s - backfilled bid_status -> %s", notice_id, expected_bid_status,
            )

        docs = existing.get("documents", [])

        if not docs:
            self.logger.info("Duplicate notice %s has no documents stored - re-fetching detail page.", notice_id)
            detail = self.fetch_detail(notice_id)
            docs = detail.get("documents", [])
            if docs:
                self.raw_collection.update_one({"_id": existing["_id"]}, {"$set": {"documents": docs}})

        pending = [d for d in docs if not d.get("s3_path")]
        if pending:
            self.logger.info("Duplicate notice %s has %d un-uploaded doc(s) - uploading now.", notice_id, len(pending))
            uploaded = self.upload_to_s3(
                pending,
                teb_number=existing.get("teb_number", f"UNKNOWN_{notice_id}"),
                mongo_id=existing["_id"],
            )
            uploaded_map = {d["original_url"]: d for d in uploaded if d.get("original_url")}
            merged = [uploaded_map.get(d.get("original_url"), d) if d.get("original_url") else d for d in docs]
            self.raw_collection.update_one({"_id": existing["_id"]}, {"$set": {"documents": merged}})
        else:
            self.logger.info("Duplicate notice %s - all documents already uploaded, skipping.", notice_id)

    # ------------------------------------------------------------------
    # CORE ENGINE
    # ------------------------------------------------------------------
    def scrape(self, statuses=None, limit=None):
        statuses = statuses or list(self.STATUS_OPTIONS)
        global_total = 0

        for status in statuses:
            self.logger.info("=" * 60)
            self.logger.info("Starting scrape for status: [%s]", status)
            self.logger.info("=" * 60)

            rows = self.search(status)
            if limit:
                rows = rows[:limit]

            status_total = 0

            for row in rows:
                notice_id = row["notice_id"]
                hash_id = self.generate_hash(notice_id)

                if self.raw_collection.find_one({"hash_id": hash_id}):
                    self.logger.info("[%s] Duplicate - checking uploads: %s", status, notice_id)
                    self._retry_missing_uploads(hash_id, notice_id)
                    continue

                try:
                    self._sleep()
                    detail = self.fetch_detail(notice_id)
                    teb_no = self.generate_teb_id()

                    detail_dates = detail.get("details", {}) or {}
                    # Prefer the detail page's own Publish/Archive Date
                    # spans (#contentMain_lblPublishDate /
                    # #contentMain_lblArchiveDate) over the search-row
                    # guess, since the detail page ids are confirmed
                    # exact and the row-cell mapping is still a
                    # heuristic for grids that don't expose headers.
                    posted_raw = detail_dates.get("publish_date_raw") or row.get("posted_date_raw")
                    expires_raw = detail_dates.get("archive_date_raw") or row.get("expires_date_raw")

                    notice_payload = {
                        "hash_id": hash_id,
                        "teb_number": teb_no,
                        "notice_id": notice_id,
                        "title": detail.get("detail_title") or row.get("title"),
                        "notice_status": status,
                        "bid_status": self.STATUS_MAP.get(status, status),
                        "summary": row.get("summary"),
                        "posted_date": self.parse_date(posted_raw),
                        "posted_date_raw": posted_raw,
                        "expires_date": self.parse_date(expires_raw),
                        "expires_date_raw": expires_raw,
                        "body_text": detail.get("body_text"),
                        "view_url": row.get("view_url"),
                        "detail_page_url": detail.get("detail_page_url"),
                        "documents": detail.get("documents", []),
                        "department": detail_dates.get("department"),
                        "category": detail_dates.get("category"),
                        "sub_category": detail_dates.get("sub_category"),
                        "locations": detail_dates.get("locations"),
                        "project_number": detail_dates.get("project_number"),
                        "events": detail.get("events", []),
                        "created_by_raw": detail.get("created_by_raw"),
                        "revision_history": detail.get("revision_history", []),
                        "source": "State of Alaska - Online Public Notices",
                        "etl_status": "pending",
                        "created_at": datetime.now(timezone.utc),
                    }

                    try:
                        res = self.raw_collection.insert_one(notice_payload)
                        self.logger.info("[%s] Stored: %s (TEB: %s)", status, notice_id, teb_no)
                    except DuplicateKeyError:
                        self.logger.info("[%s] Race-condition duplicate - checking uploads: %s", status, notice_id)
                        self._retry_missing_uploads(hash_id, notice_id)
                        continue

                    if notice_payload.get("documents"):
                        self.upload_to_s3(notice_payload["documents"], teb_number=teb_no, mongo_id=res.inserted_id)

                    status_total += 1
                    global_total += 1

                except Exception as e:
                    self.logger.error("[%s] Failed processing notice %s: %s", status, notice_id, e)

            self.logger.info("[%s] Completed. Records inserted: %d", status, status_total)

        self.logger.info("All runs complete. Total records inserted: %d", global_total)
        return global_total

    # ------------------------------------------------------------------
    # ONE-TIME MIGRATION: backfill bid_status on records inserted
    # before this field existed. Pure Mongo update, no scraping/network
    # calls to the site needed.
    # ------------------------------------------------------------------
    def backfill_bid_status(self):
        self.logger.info("Backfilling bid_status on existing records...")
        ops = []
        cursor = self.raw_collection.find(
            {"bid_status": {"$exists": False}},
            {"_id": 1, "notice_status": 1},
        )
        for doc in cursor:
            new_status = self.STATUS_MAP.get(doc.get("notice_status"), doc.get("notice_status"))
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"bid_status": new_status}}))

        if not ops:
            self.logger.info("Nothing to backfill - all records already have bid_status.")
            return 0

        result = self.raw_collection.bulk_write(ops)
        self.logger.info("Backfilled bid_status on %d record(s).", result.modified_count)
        return result.modified_count


def main():
    parser = argparse.ArgumentParser(description="Scrape Alaska Online Public Notices into Mongo/S3.")
    parser.add_argument("--status", choices=["Active", "Archived", "All"], action="append",
                         help="Status to search (repeatable). Defaults to both Active and Archived.")
    parser.add_argument("--limit", type=int, default=None, help="Max notices per status (for testing).")
    parser.add_argument("--min-delay", type=float, default=1.5)
    parser.add_argument("--max-delay", type=float, default=3.5)
    parser.add_argument("--debug-rows", action="store_true",
                         help="Verbose per-row/per-detail-page debug logging to diagnose field mapping.")
    parser.add_argument("--backfill-only", action="store_true",
                         help="Skip scraping entirely - just backfill bid_status on existing Mongo "
                              "records that predate this field, then exit.")
    args = parser.parse_args()

    scraper = AlaskaOnlinePublicNoticesScraper(
        min_delay=args.min_delay, max_delay=args.max_delay, debug_rows=args.debug_rows,
    )

    if args.backfill_only:
        scraper.backfill_bid_status()
        return

    statuses = args.status or ["Active", "Archived"]
    scraper.scrape(statuses=statuses, limit=args.limit)


if __name__ == "__main__":
    main()