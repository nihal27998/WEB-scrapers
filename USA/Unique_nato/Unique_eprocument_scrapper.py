import os
import re
import json
import math
import time
import random
import hashlib
import logging
import html as html_module


import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from dateutil import parser as dateutil_parser

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

load_dotenv()


# ══════════════════════════════════════════════════════════════════
# EUROPEAN COUNTRIES  —  used for country detection in product/title
# ══════════════════════════════════════════════════════════════════
EUROPEAN_COUNTRIES = [
    "Albania", "Andorra", "Austria", "Belarus", "Belgium",
    "Bosnia and Herzegovina", "Bulgaria", "Croatia", "Cyprus",
    "Czech Republic", "Denmark", "Estonia", "Finland", "France",
    "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy",
    "Kosovo", "Latvia", "Liechtenstein", "Lithuania", "Malta",
    "Moldova", "Monaco", "Montenegro", "Netherlands", "North Macedonia",
    "Norway", "Poland", "Portugal", "Romania", "Russia", "San Marino",
    "Serbia", "Slovakia", "Slovenia", "Spain", "Sweden", "Switzerland",
    "Ukraine", "United Kingdom", "Vatican City",
]

# Pre-compile a case-insensitive pattern for each country.
# Sorted longest-first so "Bosnia and Herzegovina" matches before "Bosnia".
_COUNTRY_PATTERNS = [
    (country, re.compile(r'\b' + re.escape(country) + r'\b', re.IGNORECASE))
    for country in sorted(EUROPEAN_COUNTRIES, key=len, reverse=True)
]

DEFAULT_COUNTRY = "LUXEMBOURG"


def detect_country(text: str) -> str:
    """
    Return the LAST European country found in `text`, or DEFAULT_COUNTRY.

    'Last' is defined as the rightmost match by start-position, which
    typically corresponds to the destination / delivery country when
    multiple countries are mentioned (e.g. "SUPPLY OF SPARE PARTS –
    GERMANY AND FRANCE" → "France").
    """
    if not text:
        return DEFAULT_COUNTRY

    last_country = None
    last_pos     = -1

    for country, pattern in _COUNTRY_PATTERNS:
        for m in pattern.finditer(text):
            if m.start() > last_pos:
                last_pos     = m.start()
                last_country = country

    return last_country.upper() if last_country else DEFAULT_COUNTRY


# ══════════════════════════════════════════════════════════════════
class NSPANATOScraper:
# ══════════════════════════════════════════════════════════════════

    BASE_URL      = "https://eportal.nspa.nato.int/eProcurement5G"
    LIST_URL      = f"{BASE_URL}/Opportunities/OpportunitiesList"
    DATA_FEED_URL = f"{BASE_URL}/Opportunities/OpportunitiesList/OpportunitiesList"
    PAGER_URL     = f"{BASE_URL}/Opportunities/OpportunitiesList/OpportunitiesListPager"
    DETAIL_URL    = f"{BASE_URL}/Opportunities/Opportunities/DetailsOpportunity"

    PAGE_SIZE = 10   # keep identical to what the browser sends

    # ──────────────────────────────────────────────────────────────
    def __init__(self):
        # ── requests session ──────────────────────────────────────
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2,
                        status_forcelist=[502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept-Language":  "en-MM,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding":  "gzip, deflate, br, zstd",
            "Connection":       "keep-alive",
        })
        self._csrf_token: str = ""

        # ── MongoDB ───────────────────────────────────────────────
        self.client   = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db       = self.client["tender_bharo"]
        self.raw_col  = self.db["nspa_nato_tenders"]
        self.meta_col = self.db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

       

        # ── Logger ────────────────────────────────────────────────
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("NSPA_NATO")

    # ══════════════════════════════════════════════════════════════
    # 1. PRIME SESSION  —  GET list page, grab CSRF token + cookies
    # ══════════════════════════════════════════════════════════════
    def prime_session(self) -> None:
        self.logger.info("═" * 60)
        self.logger.info("Priming session via GET …")
        resp = self.session.get(
            self.LIST_URL,
            params={"PreFilter": "RFP"},
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Referer":                   "https://www.google.com/",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "cross-site",
                "Sec-Fetch-User":            "?1",
            },
            timeout=30,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        token_inp = soup.find("input", {"name": "__RequestVerificationToken"})
        if not token_inp:
            raise RuntimeError(
                "Cannot find __RequestVerificationToken — site structure changed?"
            )
        self._csrf_token = token_inp["value"]
        self.logger.info(f"CSRF token captured ({len(self._csrf_token)} chars)")
        self.logger.info(f"Cookies: {list(self.session.cookies.keys())}")

    # ══════════════════════════════════════════════════════════════
    # SHARED HELPERS
    # ══════════════════════════════════════════════════════════════
    def _feed_params(self, page_index: int) -> dict:
        """Query-string params sent to both the data-feed and pager endpoints."""
        return {
            "DisplayMode":                           "Default",
            "QueryMode":                             "AvailableForActionOnly",
            "NavigationContext":                     "None",
            "CurrentPageIndex":                      page_index,
            "SkippedPages":                          0,
            "PageSize":                              self.PAGE_SIZE,
            "FilterKeywords": (
                "System.Collections.Generic.List`1"
                "[Contracts.DTO.Common.ValueTypeDTO]"
            ),
            "ColumnsToGet":                          "*",
            "PreFilter":                             "RFP",
            "IsFavorite":                            "False",
            "IsImportant":                           "False",
            "IsTodo":                                "False",
            "IsDone":                                "False",
            "UseDelegation":                         "False",
            "IsDelegationFilterVisible":             "True",
            "isFlagFilterVisible":                   "True",
            "IsContractPointOfContactFilterVisible": "True",
            "IsAssignToMeFilterVisible":             "False",
        }

    def _xhr_headers(self) -> dict:
        return {
            "Accept":                       "*/*",
            "Content-Type":                 "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With":             "XMLHttpRequest",
            "__RequestVerificationToken":   self._csrf_token,
            "Referer":  f"{self.LIST_URL}?PreFilter=RFP",
            "Origin":   "https://eportal.nspa.nato.int",
            "Sec-Fetch-Dest":  "empty",
            "Sec-Fetch-Mode":  "cors",
            "Sec-Fetch-Site":  "same-origin",
        }

    # ══════════════════════════════════════════════════════════════
    # 2. TOTAL RECORD COUNT  —  POST pager endpoint
    # ══════════════════════════════════════════════════════════════
    def _fetch_total_records(self) -> int:
        """
        POST OpportunitiesListPager → read hidden input GridPagerTotalRowCount.
        Falls back to page-widget counting, then to a large sentinel.
        """
        self.logger.info("Fetching total record count …")
        try:
            resp = self.session.post(
                self.PAGER_URL,
                params=self._feed_params(1),
                data="",
                headers=self._xhr_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # primary: hidden input
            inp = soup.find("input", {"id": "GridPagerTotalRowCount"})
            if inp and str(inp.get("value", "")).isdigit():
                total = int(inp["value"])
                self.logger.info(f"Total records: {total}")
                return total

            # fallback A: regex in raw HTML
            m = re.search(r'TotalRowCount["\s:]+(\d+)', resp.text)
            if m:
                total = int(m.group(1))
                self.logger.info(f"Total records (regex): {total}")
                return total

            # fallback B: highest page number in pagination widget
            page_nums = [
                int(li.get_text(strip=True))
                for li in soup.select("ul.pagination li.page-item a")
                if li.get_text(strip=True).isdigit()
            ]
            if page_nums:
                total = max(page_nums) * self.PAGE_SIZE
                self.logger.info(f"Total records (widget estimate): ~{total}")
                return total

        except Exception as e:
            self.logger.error(f"Could not fetch total records: {e}")

        self.logger.warning("Using sentinel total=9999; will stop on empty page.")
        return 9999

    # ══════════════════════════════════════════════════════════════
    # 3a. FETCH ONE LIST PAGE  →  HTML fragment
    # ══════════════════════════════════════════════════════════════
    def _fetch_list_page(self, page_index: int) -> list[dict]:
        """POST data-feed, parse <tbody> rows, return list of row dicts."""
        resp = self.session.post(
            self.DATA_FEED_URL,
            params=self._feed_params(page_index),
            data="",
            headers=self._xhr_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_table_rows(BeautifulSoup(resp.text, "lxml"))

    # ══════════════════════════════════════════════════════════════
    # 3b. PARSE <tbody> ROWS  —  FIXED: separate title / product,
    #     and detect country from combined text
    # ══════════════════════════════════════════════════════════════
    def _parse_table_rows(self, soup: BeautifulSoup) -> list[dict]:
        rows = []
        for tr in soup.select("tbody tr[row-id]"):
            row_id = tr.get("row-id", "").strip()
            cells  = tr.find_all("td")
            if len(cells) < 6:
                continue

            # ── cell 0 — reference + title + product ─────────────
            #
            # Cell 0 can contain up to three distinct pieces of text:
            #
            #   1. Reference code  — short, ALL-CAPS + digits (e.g. LNA26002)
            #   2. Product name    — ALL-CAPS description, medium length,
            #                        e.g. "AIRCRAFT SPARE PARTS – SPAIN"
            #   3. Title           — mixed-case sentence / phrase
            #
            # We classify every non-empty line from the cell and store
            # each in its own variable.

            ref_a = cells[0].find("a", href=True)

            ref_text = ""
            title    = ""
            product  = ""

            if ref_a:
                # ── Strategy 1: find reference code in a child element ──
                for tag in ref_a.find_all(["span", "small", "strong", "div", "p"]):
                    candidate = tag.get_text(strip=True)
                    if re.match(r'^[A-Z]{2,6}[\-\s]?\d{2,}', candidate) or \
                       re.match(r'^[A-Z0-9]{4,20}$', candidate):
                        ref_text = candidate
                        break

                # ── Strategy 2: classify every text line in the cell ────
                lines = [
                    t.strip()
                    for t in cells[0].get_text("\n").split("\n")
                    if t.strip()
                ]
                remaining = [l for l in lines if l != ref_text]

                for line in remaining:
                    # ── Title: contains lowercase words → a proper sentence ──
                    if re.search(r'[a-z]{3,}', line):
                        if not title:
                            title = line
                        # additional title fragments → append
                        elif len(line) > 10:
                            title = f"{title} {line}"

                    # ── Reference code: short, all-caps/digits, no spaces ──
                    elif (
                        not ref_text
                        and len(line) <= 25
                        and re.match(r'^[A-Z0-9\-/]+$', line)
                    ):
                        ref_text = line

                    # ── Product name: ALL-CAPS, medium length ────────────
                    elif re.match(r'^[A-Z0-9 _/\-\(\),\.&]+$', line) and len(line) > 4:
                        if not product:
                            product = line
                        elif len(line) > 5:
                            # multiple ALL-CAPS lines → concatenate
                            product = f"{product} {line}"

                # Edge-case: only ALL-CAPS text found, nothing mixed-case
                # → treat it as the title (better than leaving title blank)
                if not title and product:
                    title   = product
                    product = ""

            # ── cell 1 — opportunity type / sub-type ─────────────
            type_lines = [
                t.strip()
                for t in cells[1].get_text("\n").split("\n")
                if t.strip()
            ]
            opp_type = type_lines[0] if type_lines else ""
            sub_type = type_lines[1] if len(type_lines) > 1 else ""

            # ── cell 2 — purchasing organisation ─────────────────
            org_lines = [
                t.strip()
                for t in cells[2].get_text("\n").split("\n")
                if t.strip()
            ]
            org_code = org_lines[0] if org_lines else ""
            org_name = org_lines[1] if len(org_lines) > 1 else ""

            # ── cell 3 — status ───────────────────────────────────
            status_text = re.sub(
                r'\s+', ' ',
                cells[3].get_text(separator=" ", strip=True),
            ).strip()

            # ── shared date-cell parser ───────────────────────────
            def _parse_date_cell(cell) -> dict[str, str]:
                """Return {label_lower: value_string} for every <small> in cell."""
                result: dict[str, str] = {}
                for small in cell.find_all("small"):
                    label = small.get_text(strip=True).lower().rstrip(":")
                    value = ""
                    for sib in small.next_siblings:
                        if hasattr(sib, "name") and sib.name == "br":
                            continue
                        if hasattr(sib, "name") and sib.name == "small":
                            break
                        if not hasattr(sib, "name"):
                            candidate = str(sib).strip()
                            if candidate:
                                value = candidate
                                break
                        else:
                            candidate = sib.get_text(strip=True)
                            if candidate:
                                value = candidate
                                break
                    if label and value:
                        result[label] = value
                return result

            # ── cell 4 — publication / modified date ──────────────
            pub_date = mod_date = None
            c4 = _parse_date_cell(cells[4])
            self.logger.debug(f"    cell[4] labels: {list(c4.keys())}")
            for lbl, val in c4.items():
                if "publication" in lbl:
                    pub_date = val
                elif "modified" in lbl or "update" in lbl:
                    mod_date = val

            # ── cell 5 — closing / tentative date ────────────────
            closing_date = tentative_date = None
            c5 = _parse_date_cell(cells[5])
            self.logger.debug(f"    cell[5] labels: {list(c5.keys())}")
            for lbl, val in c5.items():
                if "closing" in lbl:
                    closing_date = val
                elif (
                    "tentative" in lbl
                    or "award"    in lbl
                    or "contract" in lbl
                    or "estimated" in lbl
                ):
                    tentative_date = val

            rows.append({
                "row_id":           row_id,
                "reference":        ref_text,
                "title":            title,
                "product_name":     product,   # ← NEW: ALL-CAPS product name
                "opportunity_type": opp_type,
                "sub_type":         sub_type,
                "org_code":         org_code,
                "org_name":         org_name,
                "status":           status_text,
                "publication_date": pub_date,
                "modified_date":    mod_date,
                "closing_date":     closing_date,
                "tentative_date":   tentative_date,
            })
        return rows

    # ══════════════════════════════════════════════════════════════
    # 4. FETCH DETAIL  —  POST with JSON body
    # ══════════════════════════════════════════════════════════════
    def fetch_detail(self, row_id_encrypted: str) -> dict:
        try:
            resp = self.session.post(
                self.DETAIL_URL,
                params={"RowIDEncrypted": row_id_encrypted},
                json={"RowIDEncrypted": row_id_encrypted},
                headers={
                    "Accept":                       "*/*",
                    "Content-Type":                 "application/json",
                    "X-Requested-With":             "XMLHttpRequest",
                    "__requestverificationtoken":   self._csrf_token,
                    "Referer": f"{self.LIST_URL}?PreFilter=RFP",
                    "Origin":  "https://eportal.nspa.nato.int",
                    "Sec-Fetch-Dest":  "empty",
                    "Sec-Fetch-Mode":  "cors",
                    "Sec-Fetch-Site":  "same-origin",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return self._parse_detail(resp.text, row_id_encrypted)
        except Exception as e:
            self.logger.error(f"Detail fetch failed [{row_id_encrypted}]: {e}")
            return {}

    def _parse_detail(self, html: str, row_id: str) -> dict:
        soup = BeautifulSoup(html, "lxml")

        def get_field(label_text: str) -> str | None:
            for lbl in soup.find_all("label"):
                if label_text.lower() in lbl.get_text(strip=True).lower():
                    parent = lbl.find_parent("div", class_="form-group")
                    if parent:
                        texts = [
                            t.strip()
                            for t in parent.get_text("\n").split("\n")
                            if t.strip()
                            and t.strip() != lbl.get_text(strip=True)
                        ]
                        return texts[0] if texts else None
            return None

        detail: dict = {
            "collective_number":  get_field("Collective Number"),
            "title":              get_field("Title"),
            "type":               get_field("Type"),
            "purchasing_org":     get_field("Purchasing Organisation"),
            "closing_time":       get_field("Closing Time"),
            "publication_date":   get_field("Publication Date"),
            "level_distribution": get_field("Level Distribution"),
            "buyer_email":        None,
            "buyer_backup_email": None,
            "attachments":        [],
            "detail_url":         f"{self.DETAIL_URL}?RowIDEncrypted={row_id}",
        }

        for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
            email  = a.get_text(strip=True)
            parent = a.find_parent("div", class_="form-group")
            label  = ""
            if parent:
                lbl_tag = parent.find("label")
                label   = lbl_tag.get_text(strip=True).lower() if lbl_tag else ""
            if "backup" in label:
                detail["buyer_backup_email"] = email
            elif not detail["buyer_email"]:
                detail["buyer_email"] = email

        detail["attachments"] = self._extract_attachments(html)
        return detail

    # ══════════════════════════════════════════════════════════════
    # ATTACHMENT EXTRACTION  (3 strategies, deduped)
    # ══════════════════════════════════════════════════════════════
    def _extract_attachments(self, html: str) -> list[dict]:
        attachments: list[dict] = []
        soup = BeautifulSoup(html, "lxml")

        # Strategy A — parse `component documents='...'` JSON blob
        for tag in soup.find_all(True):
            for attr_name, attr_val in tag.attrs.items():
                if not isinstance(attr_val, str):
                    continue
                if "documents" not in str(attr_name).lower():
                    continue
                try:
                    data  = json.loads(attr_val)
                    files = data.get("Files", [])
                    for f in files:
                        url_path = f.get("DownloadFileActionUrl", "")
                        if url_path:
                            attachments.append({
                                "type":         "Tender_document",
                                "title":        f.get("FileName", ""),
                                "original_url": urljoin(
                                    "https://eportal.nspa.nato.int",
                                    url_path,
                                ),
                                "s3_path":      None,
                                "uploaded_at":  None,
                            })
                except Exception:
                    pass

        # Strategy B — regex over HTML-unescaped raw text
        if not attachments:
            unescaped = html_module.unescape(html)
            pattern = re.compile(
                r'"FileName"\s*:\s*"(?P<fname>[^"]+)"'
                r'.*?'
                r'"DownloadFileActionUrl"\s*:\s*"(?P<url>[^"]+)"',
                re.DOTALL,
            )
            seen_urls: set[str] = set()
            for m in pattern.finditer(unescaped):
                fname    = m.group("fname")
                url_path = m.group("url").replace("\\/", "/")
                full_url = urljoin("https://eportal.nspa.nato.int", url_path)
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    attachments.append({
                        "type":         "Tender_document",
                        "title":        fname,
                        "original_url": full_url,
                        "s3_path":      None,
                        "uploaded_at":  None,
                    })

        # Strategy C — direct <a href="...DownloadAttachments..."> links
        if not attachments:
            for a in soup.find_all(
                "a", href=re.compile(r"DownloadAttachments", re.I)
            ):
                url_path = a["href"]
                full_url = urljoin("https://eportal.nspa.nato.int", url_path)
                fname = (
                    a.get_text(strip=True)
                    or os.path.basename(urlparse(full_url).path)
                )
                attachments.append({
                    "type":         "Tender_document",
                    "title":        fname,
                    "original_url": full_url,
                    "s3_path":      None,
                    "uploaded_at":  None,
                })

        # deduplicate
        seen: set[str] = set()
        unique: list[dict] = []
        for att in attachments:
            if att["original_url"] not in seen:
                seen.add(att["original_url"])
                unique.append(att)

        self.logger.info(f"    Attachments: {len(unique)}")
        return unique

    # ══════════════════════════════════════════════════════════════
    # S3 UPLOAD
    # ══════════════════════════════════════════════════════════════
    

    # ══════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════
    def _parse_date(self, raw: str | None):
        if not raw:
            return None
        try:
            dt = dateutil_parser.parse(raw.strip(), dayfirst=True)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _generate_hash(self, row_id: str) -> str:
        return hashlib.md5(row_id.encode()).hexdigest()

    def _generate_teb_id(self) -> str:
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1:"A", 2:"B", 3:"C",  4:"D",  5:"E",  6:"F",
            7:"G", 8:"H", 9:"I", 10:"J", 11:"K", 12:"L",
        }
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # ══════════════════════════════════════════════════════════════
    # PROCESS ONE ROW  (detail + insert + S3)
    # ══════════════════════════════════════════════════════════════
    def _process_row(self, row: dict, row_num: int, total: int) -> None:
        row_id = row.get("row_id", "")
        ref    = row.get("reference") or f"row_{row_num}"

        self.logger.info(f"  [{row_num}/{total}] {ref} (row_id={row_id})")

        # ── fetch detail ──────────────────────────────────────────
        detail = self.fetch_detail(row_id) if row_id else {}

        # ── resolve reference number ──────────────────────────────
        # Priority: list-table reference → collective_number → fallback label
        collective_number = detail.get("collective_number") or ""
        reference = (
            row.get("reference")
            or collective_number
            or ref
        )

        # ── resolve title ─────────────────────────────────────────
        # Prefer detail page title (more reliable) over list-parsed title
        title = detail.get("title") or row.get("title") or ""

        # ── resolve product name ──────────────────────────────────
        # Use whatever was parsed from the list table; may be empty string
        product = row.get("product_name") or ""

        # ── detect country ────────────────────────────────────────
        # Search product name first; if no hit, fall back to title.
        # Default is Luxembourg when neither contains a country.
        combined_text = f"{product} {title}".strip()
        country = detect_country(combined_text)

        self.logger.info(
            f"    Reference → {reference!r} | "
            f"Product Name → {product!r} | "
            f"Country → {country!r}"
        )

        hash_id = self._generate_hash(row_id or ref)
        teb_no  = self._generate_teb_id()

        payload = {
            "hash_id":             hash_id,
            "teb_number":          teb_no,
            "row_id_encrypted":    row_id,
            "reference":           reference,
            "title":               title,
            "product_name":        product or None,   # ← NEW field
            "country":             country,            # ← NEW field
            "opportunity_type":    row.get("opportunity_type"),
            "sub_type":            row.get("sub_type"),
            "org_code":            row.get("org_code"),
            "org_name":            row.get("org_name"),
            "status":              row.get("status"),
            "publication_date":    self._parse_date(row.get("publication_date")),
            "modified_date":       self._parse_date(row.get("modified_date")),
            "closing_date":        self._parse_date(row.get("closing_date")),
            "tentative_date":      (
                self._parse_date(row.get("tentative_date"))
                or self._parse_date(detail.get("closing_time"))
                or self._parse_date(row.get("closing_date"))
            ),
            "collective_number":   collective_number or None,
            "purchasing_org":      detail.get("purchasing_org"),
            "closing_time":        detail.get("closing_time"),
            "level_distribution":  detail.get("level_distribution"),
            "buyer_email":         detail.get("buyer_email"),
            "buyer_backup_email":  detail.get("buyer_backup_email"),
            "detail_url":          detail.get("detail_url"),
            "documents":           detail.get("attachments", []),
            "source":              "NSPA NATO eProcurement",
            "etl_status":          "pending",
            "created_at":          datetime.now(timezone.utc),
        }

        # ── insert into MongoDB ───────────────────────────────────
        try:
            result = self.raw_col.insert_one(payload)
            self.logger.info(f"    ✓ Inserted (TEB: {teb_no})")
        except DuplicateKeyError:
            self.logger.info(f"    ⚠ Duplicate — skipping: {ref}")
            return

        # ── upload attachments to S3 ──────────────────────────────
        if payload.get("documents"):
            self.logger.info(
                f"    Uploading {len(payload['documents'])} file(s) to S3 …"
            )
            self._upload_to_s3(payload, result.inserted_id)

        time.sleep(random.uniform(0.8, 1.5))

    # ══════════════════════════════════════════════════════════════
    # MAIN ENTRY POINT
    # ══════════════════════════════════════════════════════════════
    def scrape(self) -> None:
        """
        FETCH page  →  INSERT every row on that page  →  FETCH next page …
        """
        total_inserted = 0

        # ── Step 1: prime session ─────────────────────────────────
        self.prime_session()

        # ── Step 2: find out how many pages exist ─────────────────
        total_records = self._fetch_total_records()
        total_pages   = math.ceil(total_records / self.PAGE_SIZE)
        self.logger.info(
            f"Plan: {total_records} records across {total_pages} pages"
        )

        # ── Step 3: page-by-page loop ─────────────────────────────
        global_row_num = 0

        for page in range(1, total_pages + 1):
            self.logger.info("═" * 60)
            self.logger.info(
                f"PAGE {page}/{total_pages}  —  fetching list …"
            )

            # ── fetch this page ───────────────────────────────────
            try:
                rows = self._fetch_list_page(page)
            except Exception as e:
                self.logger.error(f"Page {page} fetch failed: {e}. Retrying …")
                time.sleep(3)
                try:
                    rows = self._fetch_list_page(page)
                except Exception as e2:
                    self.logger.error(f"Page {page} retry failed: {e2}. Skipping page.")
                    continue

            if not rows:
                self.logger.info(
                    f"Page {page} returned 0 rows — end of data, stopping."
                )
                break

            self.logger.info(
                f"Page {page}: got {len(rows)} row(s). "
                "Inserting now …"
            )

            # ── insert every row on this page immediately ─────────
            for row in rows:
                global_row_num += 1
                try:
                    self._process_row(row, global_row_num, total_records)
                    total_inserted += 1
                except Exception as e:
                    ref = row.get("reference", "?")
                    self.logger.error(f"  ✗ Row failed [{ref}]: {e}")

            self.logger.info(
                f"Page {page} done. "
                f"Rows inserted this page: {len(rows)} | "
                f"Grand total so far: {total_inserted}"
            )

            # ── polite delay before next page ─────────────────────
            if page < total_pages:
                delay = random.uniform(1.5, 3.0)
                self.logger.info(f"Sleeping {delay:.1f}s before page {page+1} …")
                time.sleep(delay)

        self.logger.info("═" * 60)
        self.logger.info(f"All pages done. Total records inserted: {total_inserted}")


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    NSPANATOScraper().scrape()