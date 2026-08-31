import os
import re
import json
import time
import random
import logging
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter, Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
    from pymongo import MongoClient, ReturnDocument
    from pymongo.errors import DuplicateKeyError
    STORAGE_LIBS = True
except ImportError:
    STORAGE_LIBS = False
    class DuplicateKeyError(Exception):
        pass


# ── STATUS FILTER ─────────────────────────────────────────────────────────────
AWARDED_STATUSES = {"awarded"}


def _is_awarded_status(status_text: str) -> bool:
    return "awarded" in (status_text or "").strip().lower()


# ── TEXT BLACKLIST ────────────────────────────────────────────────────────────
# Any extracted field that contains one of these phrases is junk boilerplate,
# not real content, and must be discarded regardless of length.
BOILERPLATE_PHRASES = [
    "sign up to receive",
    "create a website account",
    "manage notification subscriptions",
    "click on any of the titles",
    "the following is a listing of various bid postings",
]


def _is_boilerplate(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(phrase in low for phrase in BOILERPLATE_PHRASES)


# ══════════════════════════════════════════════════════════════════════════════
class AlleganyAwardScraper:

    BASE_URL = "https://www.alleganygov.org"
    BIDS_URL = f"{BASE_URL}/Bids.aspx"

    DOC_PATTERN = re.compile(
        r"(DocumentCenter|AttachmentCenter"
        r"|\.pdf($|\?)|\.docx?($|\?)|\.xlsx?($|\?)"
        r"|\.zip($|\?)|\.rtf($|\?)|\.pptx?($|\?))",
        re.I,
    )
    NON_DOC_PATTERN = re.compile(r"ImageRepository/Document", re.I)

    MIME_MAP = {
        ".pdf":  "application/pdf",
        ".doc":  "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls":  "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt":  "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip":  "application/zip",
        ".rtf":  "application/rtf",
    }

    WEEKDAY_RE = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    DATE_RE    = r"[A-Za-z]+\.?\s+\d{1,2},?\s*\d{4}"
    TIME_RE    = r"\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?"

    # ──────────────────────────────────────────────────────────────────────────
    def __init__(self, use_storage=True, debug=False, output_dir="output", max_bids=None):
        self.debug      = debug
        self.output_dir = output_dir
        self.max_bids   = max_bids

        self.session = requests.Session()
        retry = Retry(
            total=4, backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         self.BIDS_URL,
        })

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("AlleganyAwards")

        if self.debug:
            os.makedirs("debug_html", exist_ok=True)

        self.use_storage = (
            use_storage
            and STORAGE_LIBS
            and bool(os.getenv("LOCAL_MONGO_URI"))
        )
        if use_storage and not self.use_storage:
            self.logger.warning(
                "Storage requested but pymongo/boto3 or LOCAL_MONGO_URI missing — "
                "falling back to local JSON."
            )
        if self.use_storage:
            self._init_storage()

    # ──────────────────────────────────────────────────────────────────────────
    def _init_storage(self):
        self.client         = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        db                  = self.client["tender_bharo"]
        self.raw_col        = db["allegany_county_awards"]
        self.meta_col       = db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        self.s3_bucket      = os.getenv("S3_BUCKET_NAME", "")
        self.s3_base_folder = "tender_documents/allegany_county_awards"
        if self.s3_bucket:
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name           = os.getenv("AWS_REGION", "us-east-1"),
            )
            self.logger.info("S3 configured.")
        else:
            self.s3 = None
        self.logger.info("MongoDB connected.")

    # ══════════════════════════════════════════════════════════════════════════
  

    @staticmethod
    def _clean(text):
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _bid_id_from_href(href):
        m = re.search(r"bid[Ii][Dd]=(\d+)", href or "")
        return int(m.group(1)) if m else None

    def _hash(self, bid_id):
        return hashlib.md5(f"allegany_county_awards_{bid_id}".encode()).hexdigest()

    def _teb(self):
        if not self.use_storage:
            return ""
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        mm  = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{mm[now.month]}/{seq:08d}"

    def _parse_date(self, raw):
       
        if not raw:
            return None
        raw = self._clean(raw)

        sentinel_re = re.compile(
            r"^\s*(open(\s+until\s+contract(ed)?)?|upon\b.*|n/?a|tbd|"
            r"until\s+further(\s+notice)?|cancell?ed)\s*$",
            re.I,
        )
        # Strip a trailing parenthetical like "(Awarded)" before sentinel-checking,
        # so "April 25, 2017 (Awarded)" is treated as a real date, while a lone
        # "Awarded" or "Cancelled" string is still correctly treated as a sentinel.
        stripped = re.sub(r"\(\s*awarded[^)]*\)\s*$", "", raw, flags=re.I).strip()
        check_value = stripped or raw
        if sentinel_re.match(check_value) or check_value.lower() == "awarded":
            return None

        cleaned = re.sub(r"@", " ", stripped or raw)
        cleaned = re.sub(r"([ap])\.m\.", r"\1m", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        try:
            return date_parser.parse(cleaned)
        except Exception:
            self.logger.warning(f"Cannot parse date: {cleaned!r}")
            return None

    def _debug_save(self, html, filename):
        path = os.path.join("debug_html", filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        self.logger.info(f"[debug] saved → {path}")

    def _mime_from_url_or_header(self, url: str, content_type_header: str) -> str:
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in self.MIME_MAP:
            return self.MIME_MAP[ext]
        if content_type_header:
            return content_type_header.split(";")[0].strip()
        return "application/octet-stream"

    def _safe_filename(self, url: str, title: str) -> str:
        url_path  = urlparse(url).path
        url_ext   = os.path.splitext(url_path)[1].lower()
        base = re.sub(r"[^\w\-. ]", "_", (title or "attachment")).strip()
        base = re.sub(r"_+", "_", base)
        if not base:
            base = "attachment"
        title_ext = os.path.splitext(base)[1].lower()
        if not title_ext:
            base += url_ext if url_ext else ".pdf"
        return base

    # ══════════════════════════════════════════════════════════════════════════
    

    def prime_session(self):
        self.logger.info("Priming session …")
        r = self.session.get(self.BIDS_URL, timeout=30)
        r.raise_for_status()
        self.logger.info(f"Session primed. Cookies: {list(self.session.cookies.keys())}")
        return r.text

    # ══════════════════════════════════════════════════════════════════════════
    

    def fetch_all_awarded(self) -> list:
        self.logger.info("Trying Strategy A: GET numPerPage=999 …")
        rows_a = self._fetch_listing_get(
            params={"showAllBids": "on", "numPerPage": "999"},
            label="Awarded-A",
        )
        awarded_a = self._filter_awarded(rows_a)
        self.logger.info(f"  Strategy A → {len(awarded_a)} awarded row(s).")

        self.logger.info("Trying Strategy B: ASP.NET postback pagination …")
        rows_b = self._fetch_listing_postback()
        awarded_b = self._filter_awarded(rows_b)
        self.logger.info(f"  Strategy B → {len(awarded_b)} awarded row(s).")

        self.logger.info("Trying Strategy C: GET page=N …")
        rows_c = self._fetch_listing_get_paged()
        awarded_c = self._filter_awarded(rows_c)
        self.logger.info(f"  Strategy C → {len(awarded_c)} awarded row(s).")

        self.logger.info("Trying Strategy D: year-by-year loop …")
        awarded_d = self._fetch_listing_by_year()
        self.logger.info(f"  Strategy D → {len(awarded_d)} awarded row(s).")

        merged = {}
        for row in awarded_a + awarded_b + awarded_c + awarded_d:
            merged.setdefault(row["bid_id"], row)

        result = list(merged.values())
        self.logger.info(f"  Merged unique awarded bids: {len(result)}")
        return result

    def _filter_awarded(self, rows: list) -> list:
        seen_statuses = sorted({r["status"] for r in rows})
        if seen_statuses:
            self.logger.info(f"  All statuses on page: {seen_statuses}")
        kept    = [r for r in rows if "awarded" in r["status"].strip().lower()]
        dropped = sorted({r["status"] for r in rows if "awarded" not in r["status"].strip().lower()})
        if dropped:
            self.logger.info(f"  Filtered OUT statuses: {dropped}")
        return kept

    def _fetch_listing_get(self, params: dict, label: str) -> list:
        try:
            r = self.session.get(self.BIDS_URL, params=params, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.warning(f"  [{label}] GET failed: {e}")
            return []
        if self.debug:
            self._debug_save(r.text, f"listing_{label}.html")
        return self._parse_listing(r.text, label)

    def _fetch_listing_get_paged(self) -> list:
        all_rows = {}
        page = 1
        consecutive_empty = 0

        while True:
            params = {"showAllBids": "on", "page": str(page)}
            label  = f"Awarded-C-p{page}"
            rows   = self._fetch_listing_get(params, label)
            new    = [r for r in rows if r["bid_id"] not in all_rows]

            if not new:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                for r in new:
                    all_rows[r["bid_id"]] = r

            page += 1
            if page > 50:
                self.logger.warning("  GET paged: hit 50-page cap.")
                break
            time.sleep(random.uniform(0.5, 1.0))

        return list(all_rows.values())

    def _fetch_listing_by_year(self) -> list:
        YEAR_PARAMS = ["year", "fiscalYear", "archiveYear", "bidYear"]
        current_year = datetime.now(timezone.utc).year
        all_rows = {}

        for year in range(2019, current_year + 1):
            for param_name in YEAR_PARAMS:
                params = {"showAllBids": "on", param_name: str(year)}
                label = f"Awarded-D-{param_name}-{year}"
                rows  = self._fetch_listing_get(params, label)
                awarded = self._filter_awarded(rows)

                new = [r for r in awarded if r["bid_id"] not in all_rows]
                if new:
                    self.logger.info(
                        f"  [Year loop] param={param_name} year={year} "
                        f"→ {len(new)} new awarded bid(s)"
                    )
                    for r in new:
                        all_rows[r["bid_id"]] = r

                time.sleep(random.uniform(0.3, 0.7))

        return list(all_rows.values())

    def _fetch_listing_postback(self) -> list:
        all_rows = {}

        try:
            r = self.session.get(self.BIDS_URL, params={"showAllBids": "on"}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.warning(f"  Postback page-1 GET failed: {e}")
            return []

        if self.debug:
            self._debug_save(r.text, "listing_postback_p1.html")

        rows = self._parse_listing(r.text, "Postback-p1")
        for row in rows:
            all_rows.setdefault(row["bid_id"], row)

        soup = BeautifulSoup(r.text, "lxml")

        def _hidden(name):
            tag = soup.find("input", {"type": "hidden", "name": name})
            return tag["value"] if tag else ""

        viewstate        = _hidden("__VIEWSTATE")
        viewstate_gen    = _hidden("__VIEWSTATEGENERATOR")
        event_validation = _hidden("__EVENTVALIDATION")

        if not viewstate:
            self.logger.info("  No __VIEWSTATE found — postback pagination not applicable.")
            return list(all_rows.values())

        pager_target = self._detect_pager_target(soup)
        if not pager_target:
            self.logger.info("  No pager control detected — postback pagination skipped.")
            return list(all_rows.values())

        self.logger.info(f"  Pager control: {pager_target}")

        page_num = 2
        consecutive_empty = 0

        while True:
            post_data = {
                "__VIEWSTATE":          viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                "__EVENTVALIDATION":    event_validation,
                "__EVENTTARGET":        pager_target,
                "__EVENTARGUMENT":      str(page_num),
                "showAllBids":          "on",
            }
            try:
                pr = self.session.post(
                    self.BIDS_URL, data=post_data, timeout=30,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                pr.raise_for_status()
            except Exception as e:
                self.logger.warning(f"  Postback p{page_num} failed: {e}")
                break

            if self.debug:
                self._debug_save(pr.text, f"listing_postback_p{page_num}.html")

            new_rows = self._parse_listing(pr.text, f"Postback-p{page_num}")
            new      = [r for r in new_rows if r["bid_id"] not in all_rows]

            if not new:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                for row in new:
                    all_rows[row["bid_id"]] = row

            ps = BeautifulSoup(pr.text, "lxml")
            def _h(n):
                t = ps.find("input", {"type": "hidden", "name": n})
                return t["value"] if t else ""
            viewstate        = _h("__VIEWSTATE") or viewstate
            event_validation = _h("__EVENTVALIDATION") or event_validation

            page_num += 1
            if page_num > 100:
                self.logger.warning("  Postback: hit 100-page cap.")
                break

            time.sleep(random.uniform(0.5, 1.0))

        return list(all_rows.values())

    def _detect_pager_target(self, soup: BeautifulSoup) -> str:
        postback_re = re.compile(r"__doPostBack\('([^']+)','([^']*)'\)", re.I)
        for a in soup.find_all("a", href=re.compile(r"__doPostBack", re.I)):
            m = postback_re.search(a.get("href", ""))
            if m and re.search(r"pag|next|page", m.group(1), re.I):
                return m.group(1)

        for a in soup.find_all("a", onclick=True):
            m = postback_re.search(a["onclick"])
            if m and re.search(r"pag|next|page", m.group(1), re.I):
                return m.group(1)

        for inp in soup.find_all("input", {"type": "submit"}):
            name = inp.get("name", "")
            if re.search(r"pag|next|page", name, re.I):
                return name

        for script_tag in soup.find_all(string=postback_re):
            for m in postback_re.finditer(script_tag):
                if re.search(r"pag", m.group(1), re.I):
                    return m.group(1)

        return ""

    # ══════════════════════════════════════════════════════════════════════════


    def _parse_listing(self, html: str, status_label: str) -> list:
        soup = BeautifulSoup(html, "lxml")

        bid_items = soup.find("div", class_="bidItems")
        if not bid_items:
            bid_items = soup.find("div", id="contentarea") or soup.body
            self.logger.warning(
                f"[{status_label}] div.bidItems not found — using full contentarea."
            )

        results     = []
        seen_ids    = set()
        current_cat = ""

        for tag in bid_items.children:
            if not hasattr(tag, "get"):
                continue
            classes = tag.get("class", [])

            if "bidsHeader" in classes or "listHeader" in classes:
                spans = tag.find_all("span")
                current_cat = self._clean(spans[0].get_text()) if spans else ""
                continue

            if "listItemsRow" not in classes:
                continue

            bid_title_div  = tag.find("div", class_="bidTitle")
            bid_status_div = tag.find("div", class_="bidStatus")
            if not bid_title_div:
                continue

            link = bid_title_div.find("a", href=True)
            if not link:
                continue

            href   = link["href"].strip()
            bid_id = self._bid_id_from_href(href)
            if bid_id is None:
                continue
            if bid_id in seen_ids:
                self.logger.debug(
                    f"  [{status_label}] Skipping duplicate bidID={bid_id}"
                )
                continue
            seen_ids.add(bid_id)

            title      = self._clean(link.get_text())
            detail_url = urljoin(self.BASE_URL, href)

            bid_number = ""
            small_span = bid_title_div.find("span", style=re.compile(r"0\.75em", re.I))
            if small_span:
                bid_number = self._clean(small_span.get_text()).replace("Bid No.", "").strip()

            desc_snippet = ""
            for sp in bid_title_div.find_all("span", recursive=False)[2:]:
                txt = self._clean(sp.get_text())
                if txt and "[Read" not in txt:
                    desc_snippet = txt
                    break

            status_text = ""
            closes_text = ""
            if bid_status_div:
                val_divs = bid_status_div.find_all("div")
                if len(val_divs) >= 2:
                    val_spans = val_divs[1].find_all("span")
                    if val_spans:
                        status_text = self._clean(val_spans[0].get_text())
                    if len(val_spans) >= 2:
                        closes_text = self._clean(val_spans[1].get_text())
            if not status_text and bid_status_div:
                all_spans = bid_status_div.find_all("span")
                for sp in all_spans:
                    txt = self._clean(sp.get_text())
                    if txt:
                        status_text = txt
                        break
            if not status_text and bid_status_div:
                status_text = self._clean(bid_status_div.get_text(" "))
            if not status_text:
                status_text = status_label

            results.append({
                "bid_id":       bid_id,
                "title":        title,
                "bid_number":   bid_number,
                "category":     current_cat,
                "status":       status_text,
                "closes_raw":   closes_text,
                "desc_snippet": desc_snippet,
                "detail_url":   detail_url,
            })

        self.logger.info(f"[{status_label}] {len(results)} row(s) parsed.")
        return results

    # ══════════════════════════════════════════════════════════════════════════
   

    def fetch_detail(self, bid_id: int, detail_url: str) -> dict:
        self.logger.info(f"  Detail bidID={bid_id} …")
        try:
            r = self.session.get(detail_url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.error(f"  Detail fetch failed: {e}")
            return {}
        if self.debug:
            self._debug_save(r.text, f"detail_{bid_id}.html")
        return self._parse_detail(r.text, bid_id, detail_url)

    # ── Label/value extraction that works regardless of exact DOM shape ───────

    LABEL_PATTERNS = {
        "category":         r"category",
        "status_det":       r"status",
        "bid_num":          r"bid\s*(?:no|number|#)\b",
        "dept":             r"department",
        "pub_date_raw":     r"publication\s*date",
        "pub_info":         r"publication\s*info",
        "closing_raw":      r"closing\s*date",
        "bid_opening_raw":  r"bid\s*open(?:ing)?",
        "pre_bid_raw":      r"pre.?bid",
        "contact":          r"contact",
        "description":      r"description|summary|scope\s*of\s*work|project\s*description",
        "awarded_to":       r"awarded?\s*(?:to|vendor|contractor|company)",
        "award_amount":     r"award\s*(?:amount|value|price|cost)",
        "award_date_raw":   r"award\s*(?:date|ed\s*date)",
    }

    def _label_value_pairs_from_tags(self, content, tag_names, class_re=None):
        """
        Generic label:value extractor that works for several possible DOM shapes:
          - <span class="BidContent">Label:</span><span class="BidContent">Value</span>
          - <td>Label:</td><td>Value</td>
          - <div>Label:</div><div>Value</div>
        Tolerant of empty/whitespace nodes sitting between label and value.
        Returns a dict of {normalized_label_key: cleaned_value_text}.
        """
        if class_re is not None:
            nodes = content.find_all(tag_names, class_=class_re)
        else:
            nodes = content.find_all(tag_names)

        pairs = {}
        i = 0
        n = len(nodes)
        while i < n:
            label_txt = self._clean(nodes[i].get_text(" "))
            if label_txt.endswith(":"):
                label_key = label_txt.rstrip(":").strip().lower()
                j = i + 1
                while j < n and not self._clean(nodes[j].get_text(" ")):
                    j += 1
                val_txt = ""
                if j < n:
                    candidate = self._clean(nodes[j].get_text(" "))
                    if not candidate.endswith(":"):
                        val_txt = candidate
                        i = j + 1
                    else:
                        i = j
                else:
                    i = j
                if label_key and val_txt:
                    pairs.setdefault(label_key, val_txt)
            else:
                i += 1
        return pairs

    def _label_value_pairs_from_table(self, content):
        """
        Handles the case where label/value live in adjacent <td> cells of the
        same <tr>, e.g. <tr><td>Description:</td><td>...</td></tr>.
        """
        pairs = {}
        for tr in content.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            for idx in range(len(cells) - 1):
                label_txt = self._clean(cells[idx].get_text())
                if label_txt.endswith(":") and len(label_txt) < 60:
                    label_key = label_txt.rstrip(":").strip().lower()
                    val_txt = self._clean(cells[idx + 1].get_text())
                    if label_key and val_txt and not val_txt.endswith(":"):
                        pairs.setdefault(label_key, val_txt)
        return pairs

    def _label_value_pairs_from_text_split(self, content):
        """
        Last-resort fallback: take the raw visible text of the whole content
        block and split on known label phrases using regex, in case labels
        and values aren't in separate tags at all (e.g. one big paragraph
        like "Description: ... Publication Date/Time: ... Closing Date/Time: ...").
        """
        full_text = self._clean(content.get_text("\n"))
        pairs = {}
        label_alternatives = "|".join(
            [
                r"Category", r"Status", r"Bid\s*(?:No\.?|Number|#)",
                r"Department", r"Publication\s*Date(?:/Time)?",
                r"Publication\s*Information", r"Closing\s*Date(?:/Time)?",
                r"Bid\s*Opening(?:\s*Information)?", r"Pre-?[Bb]id\s*Meeting",
                r"Contact\s*Person", r"Description", r"Awarded\s*To",
                r"Award\s*Amount", r"Award\s*Date",
            ]
        )
        label_re = re.compile(rf"({label_alternatives})\s*:", re.I)
        matches = list(label_re.finditer(full_text))
        for idx, m in enumerate(matches):
            label_key = self._clean(m.group(1)).lower()
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(full_text)
            val_txt = self._clean(full_text[start:end])
            if val_txt:
                pairs.setdefault(label_key, val_txt)
        return pairs

    def _map_pairs_to_fields(self, pairs: dict, target: dict) -> None:
        """Maps loosely-keyed label dict onto the canonical field names in `target`,
        using regex matching on the label key (so 'bid opening information',
        'bid opening', 'bid open date' all map to bid_opening_raw, etc.)."""
        for raw_key, val_txt in pairs.items():
            if _is_boilerplate(val_txt):
                continue
            key = raw_key.lower()
            for field, pattern in self.LABEL_PATTERNS.items():
                if re.search(pattern, key, re.I):
                    if not target.get(field):
                        target[field] = val_txt
                    break

    def _parse_detail(self, html: str, bid_id: int, detail_url: str) -> dict:
        soup    = BeautifulSoup(html, "lxml")
        content = soup.find(id="contentarea") or soup.body

        # ── Title ─────────────────────────────────────────────────────────────
        title = ""
        h1 = content.find("h1", class_=re.compile(r"BidPublicHeader", re.I))
        if h1:
            title = self._clean(h1.get_text())
            title = re.sub(r"^[\u2022\u00b7\*•]+\s*|^bull;\s*", "", title, flags=re.I).strip()
        if not title:
            h1_any = content.find(["h1", "h2"])
            if h1_any:
                title = self._clean(h1_any.get_text())
        if not title:
            og = soup.find("meta", {"property": "og:title"})
            if og and og.get("content"):
                title = re.sub(r"^Bids?\s*[•&]\s*", "", og["content"], flags=re.I)
                title = self._clean(title)

        # ── og:description (semicolon-separated label:value pairs) ────────────
        og_desc_tag  = soup.find("meta", {"property": "og:description"})
        og_desc_text = og_desc_tag.get("content", "") if og_desc_tag else ""

        target = {
            "category": "", "status_det": "", "bid_num": "", "dept": "",
            "pub_date_raw": "", "pub_info": "", "closing_raw": "",
            "bid_opening_raw": "", "pre_bid_raw": "", "contact": "",
            "description": "", "awarded_to": "", "award_amount": "",
            "award_date_raw": "",
        }

        
        span_pairs = self._label_value_pairs_from_tags(
            content, "span", class_re=re.compile(r"\bBidContent\b|\bBidContentBold\b", re.I)
        )
        self._map_pairs_to_fields(span_pairs, target)

        # ── Strategy 2: any generic span/div/strong/b/td label:value pairs ─────
        if not target["description"] or not target["bid_opening_raw"]:
            generic_pairs = self._label_value_pairs_from_tags(
                content, ["span", "div", "strong", "b", "td", "th"]
            )
            self._map_pairs_to_fields(generic_pairs, target)

        # ── Strategy 3: table row label/value pairs ─────────────────────────────
        if not target["description"] or not target["bid_opening_raw"]:
            table_pairs = self._label_value_pairs_from_table(content)
            self._map_pairs_to_fields(table_pairs, target)

        # ── Strategy 4: raw-text regex split ────────────────────────────────────
        if not target["description"] or not target["bid_opening_raw"] or not target["contact"]:
            text_pairs = self._label_value_pairs_from_text_split(content)
            self._map_pairs_to_fields(text_pairs, target)

       
        if og_desc_text and not _is_boilerplate(og_desc_text):
            og_pairs = {}
            for segment in re.split(r";\s*", og_desc_text):
                m = re.match(r"\s*([^:]{2,60}):\s*(.+)$", segment.strip())
                if m:
                    og_pairs[self._clean(m.group(1)).lower()] = self._clean(m.group(2))
            self._map_pairs_to_fields(og_pairs, target)

        category       = target["category"]
        status_det     = target["status_det"]
        bid_num        = target["bid_num"]
        dept           = target["dept"]
        pub_date_raw   = target["pub_date_raw"]
        pub_info       = target["pub_info"]
        closing_raw    = target["closing_raw"]
        bid_opening_raw = target["bid_opening_raw"]
        pre_bid_raw    = target["pre_bid_raw"]
        contact        = target["contact"]
        description    = target["description"]
        awarded_to     = target["awarded_to"]
        award_amount   = target["award_amount"]
        award_date_raw = target["award_date_raw"]

        # ── Description fallback: longest meaningful <p> ────────────────────────
        if not description:
            candidates = [
                self._clean(p.get_text(" "))
                for p in content.find_all("p")
                if len(self._clean(p.get_text(" "))) > 60 and not _is_boilerplate(p.get_text())
            ]
            if candidates:
                description = max(candidates, key=len)

        # ── Description fallback: largest non-boilerplate text block ───────────
        if not description:
            blocks = [
                self._clean(div.get_text(" "))
                for div in content.find_all("div")
                if len(self._clean(div.get_text(" "))) > 150
                and not _is_boilerplate(div.get_text())
            ]
            if blocks:
                description = min(blocks, key=len)  # smallest *qualifying* block, avoids whole-page dumps

        # ── Contact fallbacks ─────────────────────────────────────────────────
        GENERIC_MAILBOX_RE = re.compile(
            r"^(notifications?|noreply|no-reply|info|webmaster|subscribe|admin)@", re.I
        )
        if not contact or _is_boilerplate(contact):
            contact = ""
            email_anchors = [
                a for a in content.find_all("a", href=re.compile(r"mailto:", re.I))
            ]
            for email_a in email_anchors:
                email_addr = re.sub(r"^mailto:", "", email_a["href"]).strip()
                if GENERIC_MAILBOX_RE.match(email_addr):
                    continue
                parent = email_a.find_parent(["td", "div", "span", "p", "li"])
                surrounding = self._clean(parent.get_text(" ")) if parent else ""
                if surrounding and not _is_boilerplate(surrounding) and len(surrounding) < 200:
                    contact = surrounding
                    break
                if not _is_boilerplate(email_addr):
                    contact = email_addr
                    break

        # ── Pre-bid mandatory ─────────────────────────────────────────────────
        pre_bid_mandatory = None
        src = (pre_bid_raw + " " + description).lower()
        if "non-mandatory" in src or "non mandatory" in src:
            pre_bid_mandatory = False
        elif "mandatory" in src:
            pre_bid_mandatory = True

        # ── Location extractions from description ───────────────────────────────
        bid_opening_location = pre_bid_location = doc_purchase_info = ""
        m = re.search(
            r"bids?\s+will\s+be\s+(?:opened)[^.]{0,160}?(?:in|at)\s+([^.]{10,160})\.",
            description, re.I)
        if m:
            bid_opening_location = self._clean(m.group(1))

        m = re.search(
            r"pre.?bid\s+(?:conference|meeting)[^.]{0,200}?(?:in|at)\s+([^.]{10,200})\.",
            description, re.I)
        if m:
            pre_bid_location = self._clean(m.group(1))

        m = re.search(r"(?:available for purchase|payment of)[^.]{0,200}\.", description, re.I)
        if m:
            doc_purchase_info = self._clean(m.group(0))

        documents = self._extract_documents(content, detail_url)

        result = {
            "bid_id":               bid_id,
            "title":                title,
            "bid_number":           bid_num,
            "category":             category,
            "status":               status_det,
            "department":           dept,
            "pub_date_raw":         pub_date_raw,
            "publication_info":     pub_info,
            "closing_raw":          closing_raw,
            "bid_opening_raw":      bid_opening_raw,
            "bid_opening_location": bid_opening_location,
            "pre_bid_raw":          pre_bid_raw,
            "pre_bid_location":     pre_bid_location,
            "pre_bid_mandatory":    pre_bid_mandatory,
            "doc_purchase_info":    doc_purchase_info,
            "description":          description,
            "contact":              contact,
            "og_description":       og_desc_text,
            "detail_url":           detail_url,
            "documents":            documents,
            "awarded_to":           awarded_to,
            "award_amount":         award_amount,
            "award_date_raw":       award_date_raw,
        }

        self._extract_from_description(description, result)
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # DESCRIPTION FALLBACK EXTRACTOR
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_from_description(self, description: str, result: dict) -> None:
        if not description:
            return
        full_text = description + "\n" + result.get("contact", "")

        # ── Bid Opening Information: <date>@<time> ──────────────────────────────
        if not result.get("bid_opening_raw"):
            m = re.search(
                rf"Bid Opening Information\s*:\s*({self.DATE_RE})\s*@\s*({self.TIME_RE})",
                full_text, re.I)
            if m:
                result["bid_opening_raw"] = f"{m.group(1).strip()} {m.group(2).strip()}"

        # ── "Bids will be opened <weekday>, <date> at <time>" ───────────────────
        if not result.get("bid_opening_raw"):
            m = re.search(
                rf"bids?\s+will\s+be\s+opened\s+{self.WEEKDAY_RE}?,?\s*"
                rf"({self.DATE_RE})\s*(?:at|@)\s*({self.TIME_RE})",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                self.logger.info(f"  [desc] bid_opening_raw (opened-pattern): {result['bid_opening_raw']}")

        # ── "Bids will be received ... on <weekday>, <date> at <time>" ──────────
        if not result.get("bid_opening_raw"):
            m = re.search(
                rf"bids?\s+will\s+be\s+received[^.]*?on\s+{self.WEEKDAY_RE},?\s*"
                rf"({self.DATE_RE})\s*(?:at|@)\s*({self.TIME_RE})",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                self.logger.info(f"  [desc] bid_opening_raw (received-pattern): {result['bid_opening_raw']}")

        # ── Merge: structured field has date-only, description has matching time ─
        if result.get("bid_opening_raw") and not re.search(r"\d{1,2}:\d{2}", result["bid_opening_raw"]):
            date_only = result["bid_opening_raw"].strip()
            escaped   = re.escape(date_only).replace(r"\ ", r"\s*")
            m = re.search(
                rf"{escaped}\s*(?:at|@)?\s*({self.TIME_RE})",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = f"{date_only} {m.group(1).strip()}"
                self.logger.info(f"  [merge] bid_opening_raw with time: {result['bid_opening_raw']}")
            else:
                # try fuzzy: same day-of-month/year mentioned anywhere near a time
                day_m = re.search(r"(\d{1,2}),?\s*(\d{4})", date_only)
                if day_m:
                    fuzzy = re.search(
                        rf"{re.escape(day_m.group(1))},?\s*{re.escape(day_m.group(2))}"
                        rf"[^.]{{0,40}}?({self.TIME_RE})",
                        full_text, re.I,
                    )
                    if fuzzy:
                        result["bid_opening_raw"] = f"{date_only} {fuzzy.group(1).strip()}"
                        self.logger.info(f"  [merge-fuzzy] bid_opening_raw: {result['bid_opening_raw']}")

        # ── Pre-Bid Meeting: <date>@<time> ───────────────────────────────────────
        if not result.get("pre_bid_raw"):
            m = re.search(
                rf"Pre-?bid Meeting\s*:\s*({self.DATE_RE})\s*@\s*({self.TIME_RE})",
                full_text, re.I)
            if m:
                result["pre_bid_raw"] = f"{m.group(1).strip()} {m.group(2).strip()}"

        if not result.get("pre_bid_raw"):
            m = re.search(
                rf"pre.?bid\s+(?:conference|meeting)[^.]*?on\s+{self.WEEKDAY_RE},?\s*"
                rf"({self.DATE_RE})\s*(?:at|@)\s*({self.TIME_RE})",
                full_text, re.I)
            if m:
                result["pre_bid_raw"] = f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"

        # ── Awarded to / amount / date ───────────────────────────────────────────
        if not result.get("awarded_to"):
            m = re.search(
                r"(?:awarded?\s+to|contract\s+awarded?\s+to)\s*:?\s*"
                r"([A-Z][A-Za-z0-9 ,\.&\-]{2,80}?)(?:\s*[;$\n]|for\s+\$|$)",
                full_text, re.I | re.M)
            if m:
                result["awarded_to"] = m.group(1).strip().rstrip(",.")
                self.logger.info(f"  [desc] awarded_to: {result['awarded_to']}")

        if not result.get("award_amount"):
            m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", full_text)
            if m:
                result["award_amount"] = "$" + m.group(1)

        if not result.get("award_date_raw"):
            m = re.search(
                rf"(?:awarded?|contract\s+date)[^:]*:\s*({self.DATE_RE})",
                full_text, re.I)
            if m:
                result["award_date_raw"] = m.group(1).strip()

        # ── Contact person: "Name - email - phone" pattern ──────────────────────
        contact_m = re.search(
            r"([A-Z][a-z]+(?:\.?\s+[A-Z][a-z]+)+)"
            r"\s*[-–]\s*([\w\.\+]+@[\w\.]+)"
            r"\s*[-–]\s*([\d\-\+\(\)\s]{7,20})",
            full_text)
        if contact_m:
            result["contact"] = (
                f"{contact_m.group(1).strip()} - "
                f"{contact_m.group(2).strip()} - "
                f"{contact_m.group(3).strip().rstrip('.,')}"
            )
        elif "@" not in result.get("contact", "") and not _is_boilerplate(result.get("contact", "")) \
                and result.get("contact"):
            # We already have a plausible name/contact value but no email yet —
            # see if an email address appears nearby in the full text and append it.
            email_m = re.search(r"[\w\.\+\-]+@[\w\.\-]+", full_text)
            if email_m:
                result["contact"] = f"{result['contact']} - {email_m.group(0).strip()}"
                self.logger.info(f"  [desc] contact (appended email): {result['contact']}")
        elif not result.get("contact") or _is_boilerplate(result.get("contact", "")) \
                or len(result.get("contact", "")) > 200:
            # "Name\nemail" on separate lines, or "Mr./Ms. Name" + email anywhere
            name_m = re.search(
                r"((?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*[A-Z][a-z]+\s+(?:[A-Z]\.?\s+)?[A-Z][a-z]+)"
                r"[\s\n,-]*([\w\.\+\-]+@[\w\.\-]+)",
                full_text,
            )
            if name_m:
                result["contact"] = f"{self._clean(name_m.group(1))} - {name_m.group(2).strip()}"
                self.logger.info(f"  [desc] contact (name+email): {result['contact']}")
            else:
                m = re.search(
                    r"contact\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+at\s+([\d\-\(\)\.]{7,20})",
                    full_text)
                if m:
                    result["contact"] = f"{m.group(1).strip()} - {m.group(2).strip().rstrip(',.')}"
                elif _is_boilerplate(result.get("contact", "")):
                    # never leave boilerplate sitting in contact
                    result["contact"] = ""

        # ── Parse dates for newly extracted raws ─────────────────────────────────
        if result.get("bid_opening_raw") and not result.get("bid_opening_date"):
            result["bid_opening_date"] = self._parse_date(result["bid_opening_raw"])
        if result.get("pre_bid_raw") and not result.get("pre_bid_date"):
            result["pre_bid_date"] = self._parse_date(result["pre_bid_raw"])
        if result.get("award_date_raw") and not result.get("award_date"):
            result["award_date"] = self._parse_date(result["award_date_raw"])

        if result.get("pre_bid_mandatory") is None:
            src = (result.get("pre_bid_raw", "") + " " + description).lower()
            if "non-mandatory" in src or "non mandatory" in src:
                result["pre_bid_mandatory"] = False
            elif "mandatory" in src:
                result["pre_bid_mandatory"] = True

    # ══════════════════════════════════════════════════════════════════════════
    

    def _extract_documents(self, root, page_url: str) -> list:
        docs = []
        seen = set()
        for a in root.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            full_url = urljoin(self.BASE_URL, href)
            if self.NON_DOC_PATTERN.search(full_url):
                continue
            if not self.DOC_PATTERN.search(full_url):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            doc_title = self._clean(a.get_text()) or \
                        os.path.basename(urlparse(full_url).path) or "attachment"
            docs.append({
                "title":        doc_title,
                "original_url": full_url,
                "s3_path":      None,
                "uploaded_at":  None,
            })
        return docs

    # ══════════════════════════════════════════════════════════════════════════
    

    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        if not self.s3 or not documents:
            return documents

        folder  = f"{teb_number.replace('/', '_')}_{mongo_id}"
        updated = []

        for d in documents:
            url = d.get("original_url")
            if not url:
                updated.append(d)
                continue
            try:
                dl_resp = self.session.get(
                    url, timeout=60, stream=True,
                    headers={"Referer": self.BIDS_URL,
                             "Accept":  "application/pdf,application/octet-stream,*/*;q=0.8"},
                    allow_redirects=True,
                )
                if dl_resp.status_code != 200:
                    self.logger.warning(f"  Doc HTTP {dl_resp.status_code} — skip: {url}")
                    updated.append(d); continue
                content_bytes = dl_resp.content
                if not content_bytes:
                    self.logger.warning(f"  Doc empty — skip: {url}")
                    updated.append(d); continue

                ct_header = dl_resp.headers.get("Content-Type", "")
                mime_type = self._mime_from_url_or_header(url, ct_header)
                fname     = self._safe_filename(url, d.get("title", ""))
                key       = f"{self.s3_base_folder}/{folder}/{fname}"

                self.s3.put_object(Bucket=self.s3_bucket, Key=key,
                                   Body=content_bytes, ContentType=mime_type)
                d["s3_path"]    = f"s3://{self.s3_bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc).isoformat()
                self.logger.info(f"  S3 ✓ {key}")
            except Exception as e:
                self.logger.error(f"  S3 upload failed {url}: {e}")
            updated.append(d)

        if self.use_storage and mongo_id:
            self.raw_col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})
        return updated

    # ══════════════════════════════════════════════════════════════════════════
    

    def _retry_missing_uploads(self, hash_id: str, bid_id: int):
        existing = self.raw_col.find_one({"hash_id": hash_id})
        if not existing:
            return
        docs    = existing.get("documents", [])
        pending = [d for d in docs if not d.get("s3_path")]
        if not pending:
            return
        self.logger.info(f"  bid {bid_id} — {len(pending)} doc(s) pending upload.")
        uploaded = self.upload_to_s3(pending, existing.get("teb_number", f"UNKNOWN_{bid_id}"),
                                     existing["_id"])
        url_map = {d["original_url"]: d for d in uploaded if d.get("original_url")}
        merged  = [url_map.get(d.get("original_url"), d) for d in docs]
        self.raw_col.update_one({"_id": existing["_id"]}, {"$set": {"documents": merged}})

    # ══════════════════════════════════════════════════════════════════════════
   

    def _build_record(self, stub: dict, detail: dict, teb_number: str) -> dict:
        title       = detail.get("title")       or stub.get("title", "")
        bid_number  = detail.get("bid_number")  or stub.get("bid_number", "")
        category    = detail.get("category")    or stub.get("category", "")
        status      = detail.get("status")      or stub.get("status", "")
        closing_raw = detail.get("closing_raw") or stub.get("closes_raw", "")
        award_date_raw = detail.get("award_date_raw", "")

        return {
            "hash_id":              self._hash(stub["bid_id"]),
            "teb_number":           teb_number,
    
            "bid_id":               stub["bid_id"],
            "title":                title,
            "bid_number":           bid_number,
            "category":             category,
            "department":           detail.get("department", ""),
            "status":               status,
            "pub_date_raw":         detail.get("pub_date_raw", ""),
            "pub_date":             self._parse_date(detail.get("pub_date_raw", "")),
            "publication_info":     detail.get("publication_info", ""),
            "closing_raw":          closing_raw,
            "closing_date":         self._parse_date(closing_raw),
            "bid_opening_raw":      detail.get("bid_opening_raw", ""),
            "bid_opening_date":     detail.get("bid_opening_date") or self._parse_date(detail.get("bid_opening_raw", "")),
            "bid_opening_location": detail.get("bid_opening_location", ""),
            "pre_bid_raw":          detail.get("pre_bid_raw", ""),
            "pre_bid_date":         detail.get("pre_bid_date") or self._parse_date(detail.get("pre_bid_raw", "")),
            "pre_bid_location":     detail.get("pre_bid_location", ""),
            "pre_bid_mandatory":    detail.get("pre_bid_mandatory"),
            "doc_purchase_info":    detail.get("doc_purchase_info", ""),
            "description":          detail.get("description", "") or stub.get("desc_snippet", ""),
            "contact":              detail.get("contact", ""),
            "og_description":       detail.get("og_description", ""),
            "detail_url":           stub["detail_url"],
            "documents":            detail.get("documents", []),
            "awarded_to":           detail.get("awarded_to", ""),
            "award_amount":         detail.get("award_amount", ""),
            "etl_status":           "pending",
            "award_date_raw":       award_date_raw,
            "award_date":           detail.get("award_date") or self._parse_date(award_date_raw),
            "source":               "Allegany County, MD - Bid Postings (Awarded)",
            "scraped_at":           datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════════════════════════════
   

    def _save_json(self, records: list) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path  = os.path.join(self.output_dir, f"allegany_awards_{stamp}.json")

        def _serial(o):
            if isinstance(o, datetime):
                return o.isoformat()
            return str(o)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=_serial, ensure_ascii=False)
        self.logger.info(f"Saved {len(records)} record(s) → {path}")
        return path

    # ══════════════════════════════════════════════════════════════════════════
   

    def scrape(self) -> list:
        all_records = []

        self.prime_session()
        time.sleep(random.uniform(1, 2))

        stubs = self.fetch_all_awarded()
        self.logger.info(f"\nTotal awarded stubs found: {len(stubs)}")

        if self.max_bids:
            stubs = stubs[: self.max_bids]

        time.sleep(random.uniform(0.8, 1.5))

        for idx, stub in enumerate(stubs, 1):
            bid_id  = stub["bid_id"]
            hash_id = self._hash(bid_id)

            self.logger.info(
                f"[Awarded] {idx}/{len(stubs)} bidID={bid_id} | {stub['title'][:55]}"
            )

            try:
                if self.use_storage and self.raw_col.find_one({"hash_id": hash_id}):
                    self.logger.info("  Already in DB — checking uploads.")
                    self._retry_missing_uploads(hash_id, bid_id)
                    continue

                detail = self.fetch_detail(bid_id, stub["detail_url"])
                teb_no = self._teb()
                record = self._build_record(stub, detail, teb_no)

                if self.use_storage:
                    try:
                        res = self.raw_col.insert_one(record)
                        self.logger.info(f"  Stored. TEB={teb_no} | _id={res.inserted_id}")
                    except DuplicateKeyError:
                        self.logger.info("  Race-condition duplicate — skipping.")
                        self._retry_missing_uploads(hash_id, bid_id)
                        continue

                    if record.get("documents"):
                        record["documents"] = self.upload_to_s3(
                            record["documents"], teb_no, res.inserted_id)

                all_records.append(record)

            except Exception as e:
                self.logger.error(f"  Error bidID={bid_id}: {e}", exc_info=True)

            time.sleep(random.uniform(0.8, 1.8))

        self.logger.info(f"\nTotal awarded records scraped: {len(all_records)}")

        if not self.use_storage:
            self._save_json(all_records)

        return all_records


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Scrape Allegany County awarded bids.")
    ap.add_argument("--no-db",      action="store_true")
    ap.add_argument("--debug",      action="store_true")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--max-bids",   type=int, default=None)
    args = ap.parse_args()

    scraper = AlleganyAwardScraper(
        use_storage = not args.no_db,
        debug       = args.debug,
        output_dir  = args.output_dir,
        max_bids    = args.max_bids,
    )
    scraper.scrape()


if __name__ == "__main__":
    main()