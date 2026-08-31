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


# ── STATUS / CATEGORY FILTERS ───────────────────────────────────────────────
CLOSED_CATEGORY_MARKERS = ("closed",)


# ══════════════════════════════════════════════════════════════════════════════
class DanvilleBidScraper:

    BASE_URL = "https://www.danvilleva.gov"
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

    # ──────────────────────────────────────────────────────────────────────────
    def __init__(self, use_storage=True, debug=False, output_dir="output", max_bids=None,
                 proxy=None, proxy_list=None, rotate_proxy=False):
        self.debug      = debug
        self.output_dir = output_dir
        self.max_bids   = max_bids

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("DanvilleBids")

        # ── Proxy configuration ────────────────────────────────────────────
        # Priority: explicit `proxy` / `proxy_list` args > PROXY_URL / PROXY_LIST
        # env vars.
        #   PROXY_LIST  comma-separated proxy URLs, e.g.
        #               "http://user:pass@host1:port,http://user:pass@host2:port"
        #   PROXY_URL   a single proxy URL used for every request
        # If rotate_proxy=True and a proxy_list is supplied, a new proxy is
        # picked from the list for every outgoing request (see
        # `_proxies_for_request` / `_request`).
        self.rotate_proxy  = rotate_proxy
        self.proxy_list    = proxy_list or self._parse_proxy_list(os.getenv("PROXY_LIST", ""))
        self.single_proxy  = proxy or os.getenv("PROXY_URL", "") or "http://12.50.107.217:80"
        self.current_proxy = None

        self.session = requests.Session()
        retry = Retry(
            total=4, backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

        if self.proxy_list and self.rotate_proxy:
            proxy_mode = f"rotating ({len(self.proxy_list)} proxies, per-request)"
            # No fixed session-level proxy — chosen per request.
        elif self.proxy_list:
            self.current_proxy = self.proxy_list[0]
            self.session.proxies.update(self._proxy_dict(self.current_proxy))
            proxy_mode = f"fixed (first of {len(self.proxy_list)} from PROXY_LIST)"
        elif self.single_proxy:
            self.current_proxy = self.single_proxy
            self.session.proxies.update(self._proxy_dict(self.current_proxy))
            proxy_mode = "fixed (single proxy)"
        else:
            proxy_mode = "none (direct connection)"

        self.logger.info(f"Proxy mode: {proxy_mode}")

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
        self.raw_col        = db["danville_city_tenders"]
        self.meta_col       = db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        self.s3_bucket      = os.getenv("S3_BUCKET_NAME", "")
        self.s3_base_folder = "tender_documents/danville_city_tenders"
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
    # PROXY HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_proxy_list(raw: str) -> list:
        return [p.strip() for p in raw.split(",") if p.strip()]

    @staticmethod
    def _proxy_dict(proxy_url: str) -> dict:
        """requests-style proxies dict for both http:// and https:// targets."""
        return {"http": proxy_url, "https": proxy_url}

    def _next_proxy(self) -> str | None:
        """Pick a proxy URL for the next outgoing request, if rotation enabled."""
        if not self.proxy_list:
            return self.single_proxy or None
        if self.rotate_proxy:
            self.current_proxy = random.choice(self.proxy_list)
        return self.current_proxy

    def _request(self, method: str, url: str, **kwargs):
        """
        Wrapper around self.session.request that injects per-request proxy
        rotation (when enabled) and falls back to a direct connection if a
        proxy request fails, retrying once.
        """
        proxies = None
        if self.rotate_proxy and self.proxy_list:
            chosen = self._next_proxy()
            proxies = self._proxy_dict(chosen)
            kwargs["proxies"] = proxies
            self.logger.debug(f"  using proxy: {chosen}")

        try:
            return self.session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            if proxies:
                self.logger.warning(
                    f"  Request via proxy failed ({e}); retrying with a different proxy…"
                )
                retry_proxy = self._next_proxy()
                if retry_proxy and retry_proxy != proxies.get("https"):
                    kwargs["proxies"] = self._proxy_dict(retry_proxy)
                    try:
                        return self.session.request(method, url, **kwargs)
                    except requests.exceptions.RequestException as e2:
                        self.logger.error(f"  Retry via alternate proxy also failed: {e2}")
                        raise
            raise

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _clean(text):
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _text_with_br(tag, sep=" | "):
        if tag is None:
            return ""
        tag_copy = BeautifulSoup(str(tag), "lxml")
        for br in tag_copy.find_all("br"):
            br.replace_with(sep)
        return re.sub(r"\s+", " ", tag_copy.get_text()).strip(" |").strip()

    @staticmethod
    def _bid_id_from_href(href):
        m = re.search(r"bid[Ii][Dd]=(\d+)", href or "")
        return int(m.group(1)) if m else None

    def _hash(self, bid_id):
        return hashlib.md5(f"danville_city_bids_{bid_id}".encode()).hexdigest()

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
        if re.search(
            r"open|upon|contract|n/?a|tbd|until further|cancelled|awarded",
            raw, re.I
        ):
            return None

        raw = re.sub(r"@", " ", raw)
        raw = re.sub(r"([ap])\.m\.", r"\1m", raw, flags=re.I)
        raw = re.sub(r"\s+", " ", raw).strip()

        try:
            return date_parser.parse(raw)
        except Exception:
            self.logger.warning(f"Cannot parse date: {raw!r}")
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
            if url_ext:
                base += url_ext
            else:
                base += ".pdf"
        return base

    @staticmethod
    def _is_closed_category(category: str) -> bool:
        cat = (category or "").lower()
        return any(marker in cat for marker in CLOSED_CATEGORY_MARKERS)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1 – PRIME SESSION
    # ══════════════════════════════════════════════════════════════════════════

    def prime_session(self):
        self.logger.info("Priming session …")
        r = self._request("GET", self.BIDS_URL, timeout=30)
        r.raise_for_status()
        self.logger.info(f"Session primed. Cookies: {list(self.session.cookies.keys())}")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 – LISTING PAGE
    # ══════════════════════════════════════════════════════════════════════════

    LISTING_PARAMS = {
        "CatID":       "All",
        "txtSort":     "Category",
        "showAllBids": "on",
        "Status":      "",
    }

    def fetch_listing(self) -> list[dict]:
        self.logger.info("Fetching bid listing …")
        r = self._request("GET", self.BIDS_URL, params=self.LISTING_PARAMS, timeout=30)
        r.raise_for_status()
        if self.debug:
            self._debug_save(r.text, "listing.html")
        rows = self._parse_listing(r.text)
        self.logger.info(f"{len(rows)} row(s) parsed from listing.")
        return rows

    def _parse_listing(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")

        bid_items = soup.find("div", class_="bidItems")
        if not bid_items:
            bid_items = soup.find("div", id="contentarea") or soup.body
            self.logger.warning("div.bidItems not found — using full contentarea.")

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
            if bid_id is None or bid_id in seen_ids:
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
                txt = re.sub(r"\[?\s*Read\s*on.*$", "", txt, flags=re.I).strip()
                if txt:
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

            is_closed_cat = self._is_closed_category(current_cat)

            results.append({
                "bid_id":            bid_id,
                "title":             title,
                "bid_number":        bid_number,
                "category":          current_cat,
                "is_closed_category": is_closed_cat,
                "status":            status_text,
                "closes_raw":        closes_text,
                "desc_snippet":      desc_snippet,
                "detail_url":        detail_url,
            })

        return results

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 – DETAIL PAGE
    # ══════════════════════════════════════════════════════════════════════════

    def fetch_detail(self, bid_id: int, detail_url: str) -> dict:
        self.logger.info(f"  Detail bidID={bid_id} …")
        try:
            r = self._request("GET", detail_url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.error(f"  Detail fetch failed: {e}")
            return {}
        if self.debug:
            self._debug_save(r.text, f"detail_{bid_id}.html")
        return self._parse_detail(r.text, bid_id, detail_url)

    def _parse_detail(self, html: str, bid_id: int, detail_url: str) -> dict:
        soup    = BeautifulSoup(html, "lxml")
        content = soup.find(id="contentarea") or soup.body

        summary = {}
        summary_table = content.find("table", summary=re.compile(r"Bids? [Dd]etails?", re.I))
        if summary_table:
            for tr in summary_table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) == 2:
                    label = self._clean(tds[0].get_text()).rstrip(":")
                    value = self._clean(tds[1].get_text())
                    if label:
                        summary[label] = value

        title      = summary.get("Bid Title", "")
        bid_number = summary.get("Bid Number", "")
        category   = summary.get("Category", "")
        status_det = summary.get("Status", "")

        if not title:
            title_tag = soup.find("title")
            if title_tag:
                t = self._clean(title_tag.get_text())
                t = re.sub(r"^Bid Postings\s*[•&]\s*", "", t, flags=re.I)
                title = t

        fields = {}
        detail_table = None
        for tbl in content.find_all("table"):
            if tbl.find("span", class_="BidListHeader"):
                detail_table = tbl
                break

        if detail_table:
            pending_label = None
            for tr in detail_table.find_all("tr"):
                header_span = tr.find("span", class_="BidListHeader")
                if header_span:
                    pending_label = self._clean(header_span.get_text()).rstrip(":")
                    continue
                if pending_label:
                    val_td = tr.find("td")
                    if val_td is not None:
                        lis = val_td.find_all("li")
                        if lis:
                            value = " | ".join(self._clean(li.get_text()) for li in lis)
                        else:
                            value = self._text_with_br(val_td)
                        fields[pending_label] = value
                    pending_label = None

        description      = fields.get("Description", "")
        pub_date_raw     = fields.get("Publication Date/Time", "")
        closing_raw      = fields.get("Closing Date/Time", "")
        pre_bid_raw      = fields.get("Pre-bid Meeting", "")
        bid_opening_raw  = (
            fields.get("Bid Opening Information")
            or fields.get("Bid Opening")
            or ""
        )
        contact          = fields.get("Contact Person", "")
        doc_purchase_info = fields.get("Document Purchase Information", "") or fields.get("Plan Deposit", "")

        og_desc_tag  = soup.find("meta", {"property": "og:description"})
        og_desc_text = og_desc_tag.get("content", "") if og_desc_tag else ""
        if og_desc_text:
            if not pub_date_raw:
                m = re.search(r"Publication Date[/\s]*Time:\s*([^;]+)", og_desc_text, re.I)
                if m:
                    pub_date_raw = m.group(1).strip()
            if not closing_raw:
                m = re.search(r"Closing Date[/\s]*Time:\s*([^;]+)", og_desc_text, re.I)
                if m:
                    closing_raw = m.group(1).strip()

        pre_bid_mandatory = None
        src_text = (pre_bid_raw + " " + description).lower()
        if "non-mandatory" in src_text or "non mandatory" in src_text:
            pre_bid_mandatory = False
        elif "mandatory" in src_text:
            pre_bid_mandatory = True

        bid_opening_location = ""
        m = re.search(
            r"bids?\s+will\s+be\s+(?:opened|open(?:ed)?)\s+and[^.|]{0,120}?(?:in|at)\s+([^.|]{10,120})",
            description, re.I
        )
        if m:
            bid_opening_location = self._clean(m.group(1))

        pre_bid_location = ""
        m = re.search(
            r"pre.?bid\s+(?:conference|meeting)[^.|]{0,200}?(?:in|at)\s+([^.|]{10,200})",
            description, re.I
        )
        if m:
            pre_bid_location = self._clean(m.group(1))

        documents = self._extract_documents(content, detail_url)

        result = {
            "bid_id":               bid_id,
            "title":                title,
            "bid_number":           bid_number,
            "category":             category,
            "status":               status_det,
            "department":           "",
            "pub_date_raw":         pub_date_raw,
            "publication_info":     "",
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
        }

        self._extract_from_description(description, result)

        return result

    # ══════════════════════════════════════════════════════════════════════════
    # DESCRIPTION FALLBACK EXTRACTOR
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_from_description(self, description: str, result: dict) -> None:
        if not description:
            return

        contact_text = result.get("contact", "")
        full_text = description + "\n" + contact_text

        if not result.get("bid_opening_raw"):
            m = re.search(
                r"Bid Opening\s+(?:[A-Za-z]+,?\s*)?"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*(?:at|@)\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                self.logger.info(f"  [desc] bid_opening_raw: {result['bid_opening_raw']}")

        if not result.get("pre_bid_raw"):
            m = re.search(
                r"Pre.?[Bb]id\s+(?:[A-Za-z]+,?\s*)?"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*(?:at|@)\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["pre_bid_raw"] = f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                self.logger.info(f"  [desc] pre_bid_raw: {result['pre_bid_raw']}")

        if contact_text and "@" not in contact_text:
            email_match = re.search(r"[\w\.\+\-]+@[\w\.\-]+\.\w+", full_text)
            if email_match:
                result["contact"] = f"{contact_text} | E-Mail: {email_match.group(0)}"

        if result.get("bid_opening_raw") and not result.get("bid_opening_date"):
            result["bid_opening_date"] = self._parse_date(result["bid_opening_raw"])

        if result.get("pre_bid_raw") and not result.get("pre_bid_date"):
            result["pre_bid_date"] = self._parse_date(result["pre_bid_raw"])

        if result.get("pre_bid_mandatory") is None:
            src = (result.get("pre_bid_raw", "") + " " + description).lower()
            if "non-mandatory" in src or "non mandatory" in src:
                result["pre_bid_mandatory"] = False
            elif "mandatory" in src:
                result["pre_bid_mandatory"] = True

    # ══════════════════════════════════════════════════════════════════════════
    # DOCUMENT EXTRACTOR
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_documents(self, root, page_url: str) -> list[dict]:
        docs = []
        seen = set()

        search_root = root.find("div", class_="relatedDocuments") or root

        for a in search_root.find_all("a", href=True):
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
    # STEP 4 – S3 UPLOAD
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
                dl_resp = self._request(
                    "GET",
                    url,
                    timeout=60,
                    stream=True,
                    headers={
                        "Referer": self.BIDS_URL,
                        "Accept":  "application/pdf,application/octet-stream,*/*;q=0.8",
                    },
                    allow_redirects=True,
                )

                if dl_resp.status_code != 200:
                    self.logger.warning(
                        f"  Doc download HTTP {dl_resp.status_code} — skipping: {url}"
                    )
                    updated.append(d)
                    continue

                content_bytes = dl_resp.content
                if not content_bytes:
                    self.logger.warning(f"  Doc empty body — skipping: {url}")
                    updated.append(d)
                    continue

                ct_header = dl_resp.headers.get("Content-Type", "")
                mime_type = self._mime_from_url_or_header(url, ct_header)
                fname     = self._safe_filename(url, d.get("title", ""))
                key       = f"{self.s3_base_folder}/{folder}/{fname}"

                self.s3.put_object(
                    Bucket      = self.s3_bucket,
                    Key         = key,
                    Body        = content_bytes,
                    ContentType = mime_type,
                )
                d["s3_path"]    = f"s3://{self.s3_bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc).isoformat()
                self.logger.info(f"  S3 ✓ {key}")

            except Exception as e:
                self.logger.error(f"  S3 upload failed {url}: {e}")

            updated.append(d)

        if self.use_storage and mongo_id:
            self.raw_col.update_one(
                {"_id": mongo_id},
                {"$set": {"documents": updated}},
            )
        return updated

    # ══════════════════════════════════════════════════════════════════════════
    # RETRY MISSING UPLOADS
    # ══════════════════════════════════════════════════════════════════════════

    def _retry_missing_uploads(self, hash_id: str, bid_id: int):
        existing = self.raw_col.find_one({"hash_id": hash_id})
        if not existing:
            return
        docs    = existing.get("documents", [])
        pending = [d for d in docs if not d.get("s3_path")]
        if not pending:
            self.logger.info(f"  bid {bid_id} — all docs already uploaded, skipping.")
            return
        self.logger.info(f"  bid {bid_id} — {len(pending)} doc(s) pending upload.")
        uploaded = self.upload_to_s3(
            pending,
            teb_number = existing.get("teb_number", f"UNKNOWN_{bid_id}"),
            mongo_id   = existing["_id"],
        )
        url_map = {d["original_url"]: d for d in uploaded if d.get("original_url")}
        merged  = [url_map.get(d.get("original_url"), d) for d in docs]
        self.raw_col.update_one(
            {"_id": existing["_id"]},
            {"$set": {"documents": merged}},
        )

    # ══════════════════════════════════════════════════════════════════════════
    # RECORD ASSEMBLY
    # ══════════════════════════════════════════════════════════════════════════

    def _build_record(self, stub: dict, detail: dict, teb_number: str) -> dict:
        title       = detail.get("title")       or stub.get("title", "")
        bid_number  = detail.get("bid_number")  or stub.get("bid_number", "")
        category    = detail.get("category")    or stub.get("category", "")
        status      = detail.get("status")      or stub.get("status", "")
        closing_raw = detail.get("closing_raw") or stub.get("closes_raw", "")

        return {
            "hash_id":              self._hash(stub["bid_id"]),
            "teb_number":           teb_number,
            "etl_status":           "pending",
            "bid_id":               stub["bid_id"],
            "title":                title,
            "bid_number":           bid_number,
            "category":             category,
            "is_closed_category":   stub.get("is_closed_category", False),
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
            "source":               "Danville, VA - Bid Postings",
            "scraped_at":           datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # LOCAL JSON OUTPUT
    # ══════════════════════════════════════════════════════════════════════════

    def _save_json(self, records: list) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path  = os.path.join(self.output_dir, f"danville_bids_{stamp}.json")

        def _serial(o):
            if isinstance(o, datetime):
                return o.isoformat()
            return str(o)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=_serial, ensure_ascii=False)
        self.logger.info(f"Saved {len(records)} record(s) → {path}")
        return path

    # ══════════════════════════════════════════════════════════════════════════
    # MAIN SCRAPE LOOP
    # ══════════════════════════════════════════════════════════════════════════

    def scrape(self) -> list[dict]:
        all_records = []

        self.prime_session()
        time.sleep(random.uniform(1, 2))

        stubs = self.fetch_listing()
        if self.max_bids:
            stubs = stubs[: self.max_bids]

        time.sleep(random.uniform(0.8, 1.5))

        for idx, stub in enumerate(stubs, 1):
            bid_id  = stub["bid_id"]
            hash_id = self._hash(bid_id)
            tag     = "Closed" if stub.get("is_closed_category") else "Open"

            self.logger.info(
                f"[{tag}] {idx}/{len(stubs)} bidID={bid_id} | "
                f"{stub['title'][:55]}"
            )

            try:
                if self.use_storage and self.raw_col.find_one({"hash_id": hash_id}):
                    self.logger.info(f"  Already in DB — checking uploads.")
                    self._retry_missing_uploads(hash_id, bid_id)
                    continue

                detail = self.fetch_detail(bid_id, stub["detail_url"])
                teb_no = self._teb()
                record = self._build_record(stub, detail, teb_no)

                if self.use_storage:
                    try:
                        res = self.raw_col.insert_one(record)
                        self.logger.info(
                            f"  Stored in Mongo. TEB={teb_no} | _id={res.inserted_id}"
                        )
                    except DuplicateKeyError:
                        self.logger.info(f"  Race-condition duplicate — skipping.")
                        self._retry_missing_uploads(hash_id, bid_id)
                        continue

                    if record.get("documents"):
                        record["documents"] = self.upload_to_s3(
                            record["documents"], teb_no, res.inserted_id
                        )

                all_records.append(record)

            except Exception as e:
                self.logger.error(
                    f"  Error bidID={bid_id}: {e}", exc_info=True
                )

            time.sleep(random.uniform(0.8, 1.8))

        self.logger.info(f"\nTotal records scraped: {len(all_records)}")

        if not self.use_storage:
            self._save_json(all_records)

        return all_records


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="Scrape Danville, VA bids (Bids.aspx, all categories incl. closed/archival)."
    )
    ap.add_argument("--no-db",      action="store_true",
                    help="Skip Mongo/S3, write local JSON instead.")
    ap.add_argument("--debug",      action="store_true",
                    help="Dump raw HTML of every page to ./debug_html/")
    ap.add_argument("--output-dir", default="output",
                    help="Directory for local JSON output (default: ./output)")
    ap.add_argument("--max-bids",   type=int, default=None,
                    help="Limit total bids processed (handy for quick tests).")
    ap.add_argument("--proxy",      default=None,
                    help="Single proxy URL to use for all requests, "
                         "e.g. http://user:pass@host:port")
    ap.add_argument("--proxy-list", default=None,
                    help="Comma-separated list of proxy URLs to rotate through.")
    ap.add_argument("--rotate-proxy", action="store_true",
                    help="Rotate randomly through --proxy-list / PROXY_LIST "
                         "on every request instead of using a single fixed proxy.")
    args = ap.parse_args()

    proxy_list = args.proxy_list.split(",") if args.proxy_list else None

    scraper = DanvilleBidScraper(
        use_storage  = not args.no_db,
        debug        = args.debug,
        output_dir   = args.output_dir,
        max_bids     = args.max_bids,
        proxy        = args.proxy,
        proxy_list   = proxy_list,
        rotate_proxy = args.rotate_proxy,
    )
    scraper.scrape()


if __name__ == "__main__":
    main()