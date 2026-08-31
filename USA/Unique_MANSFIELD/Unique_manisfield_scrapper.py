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
CLOSED_STATUSES = {"closed"}


# ══════════════════════════════════════════════════════════════════════════════
class MansfieldTexasBidScraper:

    BASE_URL = "https://www.mansfieldtexas.gov"
    BIDS_URL = f"{BASE_URL}/Bids.aspx"

    # Listing URL params for open vs closed
    LISTING_MODES = [
        {
            "label": "Open",
            "params": {
                "CatID":       "showStatus",
                "txtSort":     "Category",
                "Status":      "open",
            },
            "filter_status": None,
        },
        {
            "label": "Closed",
            "params": {
                "CatID":       "showStatus",
                "txtSort":     "Category",
                "showAllBids": "on",
                "Status":      "closed",
            },
            "filter_status": CLOSED_STATUSES,
        },
    ]

    DOC_PATTERN = re.compile(
        r"(DocumentCenter|AttachmentCenter"
        r"|\.pdf($|\?)|\.docx?($|\?)|\.xlsx?($|\?)"
        r"|\.zip($|\?)|\.rtf($|\?)|\.pptx?($|\?))",
        re.I,
    )
    NON_DOC_PATTERN = re.compile(r"ImageRepository/Document", re.I)

    # Used to pull a phone number / email back out of a contact string so it
    # can be re-assembled as "Name, Phone, Email" no matter how the source
    # HTML originally separated them (br tags collapsing to spaces, pipes,
    # dashes, etc).
    EMAIL_RE = re.compile(r"[\w\.\+\-]+@[\w\.\-]+\.\w+")
    PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

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

    # ── Table A: top "Bid details" summary table -- label and value are two
    #    <td>s in the SAME <tr> (span.BidDetail label / span.BidDetailSpec value) ──
    SUMMARY_TABLE_KEY_MAP = {
        "bid number":    "bid_number",
        "bid title":     "title",
        "category":      "category",
        "status":        "status",
        "department":    "department",
        "bid recipient": "_bid_recipient",  # fallback only, used if Contact Person row is absent
    }

    # ── Table B: lower "Bid Details" table -- span.BidListHeader is alone in
    #    one <tr>, the value (span.BidDetail / span.BidDetailSpec) is alone in
    #    the NEXT <tr>. This is where almost all of the substantive bid info
    #    actually lives -- NOT in span.BidContent/.BidContentBold, which this
    #    CMS template doesn't use for any of these fields. ──
    MAIN_TABLE_KEY_MAP = {
        "publication date/time":          "pub_date_raw",
        "publication information":        "publication_info",
        "closing date/time":              "closing_raw",
        "bid opening information":        "bid_opening_raw",
        "bid opening date/time":          "bid_opening_raw",
        "bid opening time":               "bid_opening_raw",
        "pre-bid conference":              "pre_bid_raw",
        "pre-bid conference information":  "pre_bid_raw",
        "pre-bid date/time":               "pre_bid_raw",
        "pre-bid information":             "pre_bid_raw",
        "pre-bid meeting":                 "pre_bid_raw",
        "submittal information":          "submittal_info",
        "contact person":                 "contact",
        "download available":             "download_available",
        "fee":                            "fee",
        "plan & spec available":          "plan_spec",
        "business hours":                 "business_hours",
        "miscellaneous":                  "miscellaneous",
        "description":                    "description",
        "document purchase information":  "doc_purchase_info",
        "related documents":              "_documents",  # handled specially, not plain text
    }

    # ──────────────────────────────────────────────────────────────────────────
    def __init__(self, use_storage=True, debug=False, output_dir="output", max_bids=None):
        self.debug      = debug
        self.output_dir = output_dir
        self.max_bids   = max_bids

        self.session = requests.Session()
        retry = Retry(
            total=4, backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
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
        self.logger = logging.getLogger("MansfieldTXBids")

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
        self.raw_col        = db["mansfield_tx_tenders"]
        self.meta_col       = db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        self.s3_bucket      = os.getenv("S3_BUCKET_NAME", "")
        self.s3_base_folder = "tender_documents/mansfield_tx_tenders"
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
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _clean(text):
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _bid_id_from_href(href):
        m = re.search(r"[Bb]id[Ii][Dd]=(\d+)", href or "")
        return int(m.group(1)) if m else None

    def _hash(self, bid_id):
        return hashlib.md5(f"mansfield_tx_bids_{bid_id}".encode()).hexdigest()

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

    def _format_contact(self, raw: str) -> str:
        """Normalize a contact string into 'Name, Phone, Email' regardless
        of whether the source HTML separated the pieces with <br> (which
        BeautifulSoup collapses to a plain space), pipes, or dashes."""
        if not raw:
            return raw
        raw = self._clean(raw)

        email_m = self.EMAIL_RE.search(raw)
        phone_m = self.PHONE_RE.search(raw)
        email   = email_m.group(0) if email_m else ""
        phone   = phone_m.group(0) if phone_m else ""

        rest = raw
        if email:
            rest = rest.replace(email, " ")
        if phone:
            rest = rest.replace(phone, " ")

        # Whatever separators (|, -, extra spaces, existing commas) are left
        # over in `rest` get collapsed so the name/title comes through as a
        # single clean chunk.
        pieces = re.split(r"\s*[|,]\s*|\s{2,}|\s+-\s+", rest)
        pieces = [self._clean(p).strip(" -") for p in pieces]
        pieces = [p for p in pieces if p]
        name_part = " ".join(pieces)

        return ", ".join(p for p in [name_part, phone, email] if p)

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
    # STEP 1 – PRIME SESSION
    # ══════════════════════════════════════════════════════════════════════════

    def prime_session(self):
        self.logger.info("Priming session …")
        r = self.session.get(self.BIDS_URL, timeout=30)
        r.raise_for_status()
        self.logger.info(f"Session primed. Cookies: {list(self.session.cookies.keys())}")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 – LISTING PAGE
    # ══════════════════════════════════════════════════════════════════════════

    def fetch_listing(self, params: dict, label: str, filter_status) -> list:
        self.logger.info(f"Fetching [{label}] listing …")
        r = self.session.get(self.BIDS_URL, params=params, timeout=30)
        r.raise_for_status()
        if self.debug:
            self._debug_save(r.text, f"listing_{label.lower()}.html")
        rows = self._parse_listing(r.text, label)

        if filter_status:
            before = len(rows)
            rows = [
                row for row in rows
                if row["status"].strip().lower() in filter_status
            ]
            self.logger.info(
                f"[{label}] Status filter kept {len(rows)}/{before} rows "
                f"(keeping: {filter_status})"
            )
        return rows

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

            if not status_text:
                self.logger.warning(
                    f"[{status_label}] bidID={bid_id} — could not parse real status "
                    f"from bidStatus div, leaving status_text empty (not defaulting to '{status_label}')."
                )

            results.append({
                "bid_id":       bid_id,
                "title":        title,
                "bid_number":   bid_number,
                "category":     current_cat,
                "status":       status_text,
                "listing_mode": status_label,
                "closes_raw":   closes_text,
                "desc_snippet": desc_snippet,
                "detail_url":   detail_url,
            })

        self.logger.info(f"[{status_label}] {len(results)} row(s) parsed from HTML.")
        return results

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 – DETAIL PAGE
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

    def _parse_summary_table(self, content) -> dict:
        """Table A: the top 'Bid details' table where label and value sit
        as two <td>s in the same <tr> (e.g. Bid Number / Bid Title /
        Category / Status / Bid Recipient)."""
        out = {}
        for row in content.select("table[summary='Bid details'] tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label = self._clean(cells[0].get_text()).rstrip(":").lower()
            value = self._clean(cells[1].get_text())
            key = self.SUMMARY_TABLE_KEY_MAP.get(label)
            if key:
                out[key] = value
        return out

    def _parse_main_table(self, content) -> dict:
        """Table B: the lower 'Bid Details' table where span.BidListHeader
        sits alone in one <tr> and its value (span.BidDetail /
        span.BidDetailSpec) sits alone in the NEXT <tr>. Whatever headers
        aren't present on a given bid simply won't appear in the returned
        dict -- callers should treat missing keys as "this bid has no such
        section" rather than as a parse failure."""
        out = {"documents": []}
        for header_span in content.select("span.BidListHeader"):
            label = self._clean(header_span.get_text()).rstrip(":").lower()
            header_row = header_span.find_parent("tr")
            if header_row is None:
                continue
            value_row = header_row.find_next_sibling("tr")
            if value_row is None:
                continue

            if label == "related documents":
                for a in value_row.find_all("a", href=True):
                    out["documents"].append({
                        "title":        self._clean(a.get_text()),
                        "type":         "Tender_document",
                        "original_url": urljoin(self.BASE_URL, a["href"]),
                        "s3_path":      None,
                        "uploaded_at":  None,
                    })
                continue

            value_tag = value_row.find("span", class_=re.compile(r"BidDetail(Spec)?", re.I))
            value = self._clean(value_tag.get_text(" ")) if value_tag else self._clean(value_row.get_text(" "))

            key = self.MAIN_TABLE_KEY_MAP.get(label)
            if key:
                out[key] = value
        return out

    def _parse_detail(self, html: str, bid_id: int, detail_url: str) -> dict:
        soup    = BeautifulSoup(html, "lxml")
        content = soup.find(id="contentarea") or soup.body

        summary = self._parse_summary_table(content)
        main    = self._parse_main_table(content)

        # ── Title ─────────────────────────────────────────────────────────────
        # og:title / twitter:title on this CMS are double-HTML-escaped
        # (content="Bids &amp;bull; ...") which decodes ONCE to the literal
        # text "Bids &bull; ..." — not a real bullet character. Stripping a
        # bare "&" off that afterwards is what produces a "bull; ..."
        # prefix stuck on the title. The "Bid Title:" row in the summary
        # table is plain clean text, so use that instead and never touch
        # og:title for this field.
        title = summary.get("title", "")
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                # The page <title> uses a real "•" character (not an
                # entity), so splitting on it directly is safe here.
                raw_title = self._clean(title_tag.get_text())
                title = raw_title.split("•", 1)[-1].strip() or raw_title

        # ── og:description — plain text, not double-escaped; safe to keep
        #    as a last-resort fallback only ───────────────────────────────
        og_desc_tag  = soup.find("meta", {"property": "og:description"})
        og_desc_text = og_desc_tag.get("content", "") if og_desc_tag else ""

        category    = summary.get("category", "")
        status_det  = summary.get("status", "")
        bid_num     = summary.get("bid_number", "")
        dept        = summary.get("department", "")

        pub_date_raw       = main.get("pub_date_raw", "")
        closing_raw        = main.get("closing_raw", "")
        bid_opening_raw    = main.get("bid_opening_raw", "")
        pre_bid_raw        = main.get("pre_bid_raw", "")
        pub_info           = main.get("publication_info", "")
        submittal_info     = main.get("submittal_info", "")
        fee                = main.get("fee", "")
        plan_spec          = main.get("plan_spec", "")
        business_hours     = main.get("business_hours", "")
        miscellaneous      = main.get("miscellaneous", "")
        download_avail     = main.get("download_available", "")
        description        = main.get("description", "")
        doc_purchase_info  = main.get("doc_purchase_info", "")

        # ── Contact ───────────────────────────────────────────────────────────
        # Contact Person (Table B) wins when present, since it's normally
        # the fuller value (name + title + email + phone). Bid Recipient
        # (Table A) is the fallback for bids that only have that. Either
        # way this is scoped to the exact row, never the whole page.
        contact = main.get("contact", "") or summary.get("_bid_recipient", "")

        # Last-resort fallback from og:description for the two date fields,
        # only if the structured table genuinely had nothing for them.
        if og_desc_text:
            if not pub_date_raw:
                m = re.search(r"Publication Date[/\s]*Time:\s*([^;]+)", og_desc_text, re.I)
                if m: pub_date_raw = m.group(1).strip()
            if not closing_raw:
                m = re.search(r"Closing Date[/\s]*Time:\s*([^;]+)", og_desc_text, re.I)
                if m: closing_raw = m.group(1).strip()

        if not description:
            paras = [
                self._clean(p.get_text(" "))
                for p in content.find_all("p")
                if len(self._clean(p.get_text(" "))) > 60
            ]
            if paras:
                description = "\n\n".join(paras)

        if not contact:
            email_anchor = content.find("a", href=re.compile(r"mailto:", re.I))
            if email_anchor:
                email_addr = re.sub(r"^mailto:", "", email_anchor["href"]).strip()
                parent     = email_anchor.find_parent(["td", "div", "span", "p", "li"])
                contact = self._clean(parent.get_text(" ")) if parent else email_addr

        pre_bid_mandatory = None
        src_text = (pre_bid_raw + " " + description).lower()
        if "non-mandatory" in src_text or "non mandatory" in src_text:
            pre_bid_mandatory = False
        elif "mandatory" in src_text:
            pre_bid_mandatory = True

        bid_opening_location = ""
        m = re.search(
            r"bids?\s+will\s+be\s+(?:opened|open(?:ed)?)\s+and[^.]{0,120}?(?:in|at)\s+([^.]{10,120})\.",
            description, re.I
        )
        if m:
            bid_opening_location = self._clean(m.group(1))

        pre_bid_location = ""
        m = re.search(
            r"pre.?bid\s+(?:conference|meeting)[^.]{0,200}?(?:in|at)\s+([^.]{10,200})\.",
            description, re.I
        )
        if m:
            pre_bid_location = self._clean(m.group(1))

        if not doc_purchase_info:
            m = re.search(
                r"(?:available for purchase|payment of)[^.]{0,200}\.",
                description, re.I
            )
            if m:
                doc_purchase_info = self._clean(m.group(0))

        # Documents: combine the explicit "Related Documents" row with any
        # other document-looking links elsewhere on the page (e.g. inline
        # in the description), de-duplicated by URL.
        documents = list(main.get("documents", []))
        seen_urls = {d["original_url"] for d in documents}
        for d in self._extract_documents(content, detail_url):
            if d["original_url"] not in seen_urls:
                documents.append(d)
                seen_urls.add(d["original_url"])

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
            "submittal_info":       submittal_info,
            "fee":                  fee,
            "plan_spec":            plan_spec,
            "business_hours":       business_hours,
            "miscellaneous":        miscellaneous,
            "download_available":   download_avail,
            "description":          description,
            "contact":              contact,
            "og_description":       og_desc_text,
            "detail_url":           detail_url,
            "documents":            documents,
        }

        self._extract_from_description(description, result)
        return result

    def _extract_from_description(self, description: str, result: dict) -> None:
        if not description:
            return
        contact_text = result.get("contact", "")
        full_text = description + "," + contact_text

        if not result.get("bid_opening_raw"):
            m = re.search(
                r"Bid Opening Information\s*:\s*"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*@\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = f"{m.group(1).strip()} {m.group(2).strip()}"

        if not result.get("bid_opening_raw"):
            m = re.search(
                r"bids?\s+will\s+be\s+received[^.]*?on\s+"
                r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*(?:at|@)\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["bid_opening_raw"] = (
                    f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                )

        if not result.get("pre_bid_raw"):
            m = re.search(
                r"Pre-?bid Meeting\s*:\s*"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*@\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["pre_bid_raw"] = f"{m.group(1).strip()} {m.group(2).strip()}"

        if not result.get("pre_bid_raw"):
            m = re.search(
                r"pre.?bid\s+(?:conference|meeting)[^.]*?on\s+"
                r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*"
                r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*(?:at|@)\s*([\d:]+\s*[APap]\.?[Mm]\.?)",
                full_text, re.I
            )
            if m:
                result["pre_bid_raw"] = (
                    f"{m.group(1).strip().rstrip(',')} {m.group(2).strip()}"
                )

        if not result.get("contact") or len(result.get("contact", "")) > 300:
            pipe_match = re.search(
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
                r"\s*\|\s*([^|]+)"
                r"\s*\|\s*([\w\.\+]+@[\w\.]+)"
                r"\s*\|\s*([\d\-\+\(\)\s\.]{7,20})",
                result.get("contact", "")
            )
            if pipe_match:
                result["contact"] = (
                    f"{pipe_match.group(1).strip()} | "
                    f"{pipe_match.group(2).strip()} | "
                    f"{pipe_match.group(3).strip()} | "
                    f"{pipe_match.group(4).strip().rstrip('.,')}"
                )
            else:
                dash_match = re.search(
                    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
                    r"\s*-\s*([\w\.\+]+@[\w\.]+)"
                    r"\s*-\s*([\d\-\+\(\)\s]{7,20})",
                    full_text
                )
                if dash_match:
                    result["contact"] = (
                        f"{dash_match.group(1).strip()} - "
                        f"{dash_match.group(2).strip()} - "
                        f"{dash_match.group(3).strip().rstrip('.,')}"
                    )

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
                "type": "Tender_document",
                "original_url": full_url,
                "s3_path":      None,
                "uploaded_at":  None,
            })
        return docs

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

    def _build_record(self, stub: dict, detail: dict, teb_number: str) -> dict:
        title       = detail.get("title")       or stub.get("title", "")
        bid_number  = detail.get("bid_number")  or stub.get("bid_number", "")
        category    = detail.get("category")    or stub.get("category", "")
        status      = detail.get("status") or stub.get("status") or "unknown"   
        closing_raw = detail.get("closing_raw") or stub.get("closes_raw", "")

        return {
            "hash_id":              self._hash(stub["bid_id"]),
            "teb_number":           teb_number,
            "etl_status":           "pending",
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
            "submittal_info":       detail.get("submittal_info", ""),
            "fee":                  detail.get("fee", ""),
            "plan_spec":            detail.get("plan_spec", ""),
            "business_hours":       detail.get("business_hours", ""),
            "miscellaneous":        detail.get("miscellaneous", ""),
            "download_available":   detail.get("download_available", ""),
            "description":          detail.get("description", "") or stub.get("desc_snippet", ""),
            "contact":              self._format_contact(detail.get("contact", "")),
            "og_description":       detail.get("og_description", ""),
            "detail_url":           stub["detail_url"],
            "documents":            detail.get("documents", []),
            "source":               "Mansfield, TX - Bid Postings",
            "scraped_at":           datetime.now(timezone.utc).isoformat(),
        }

    def _save_json(self, records: list) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path  = os.path.join(self.output_dir, f"mansfield_tx_bids_{stamp}.json")

        def _serial(o):
            if isinstance(o, datetime):
                return o.isoformat()
            return str(o)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=_serial, ensure_ascii=False)
        self.logger.info(f"Saved {len(records)} record(s) → {path}")
        return path

    def scrape(self) -> list:
        all_records = []

        self.prime_session()
        time.sleep(random.uniform(1, 2))

        for mode in self.LISTING_MODES:
            label         = mode["label"]
            params        = mode["params"]
            filter_status = mode["filter_status"]

            self.logger.info(f"\n{'='*60}")
            self.logger.info(f" Scraping [{label}] bids")
            self.logger.info(f"{'='*60}")

            stubs = self.fetch_listing(params, label, filter_status)
            if self.max_bids:
                stubs = stubs[: self.max_bids]

            time.sleep(random.uniform(0.8, 1.5))

            for idx, stub in enumerate(stubs, 1):
                bid_id  = stub["bid_id"]
                hash_id = self._hash(bid_id)

                self.logger.info(
                    f"[{label}] {idx}/{len(stubs)} bidID={bid_id} | "
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

            self.logger.info(f"[{label}] Done. {len(stubs)} bid(s) processed.")

        self.logger.info(f"\nTotal records scraped: {len(all_records)}")

        if not self.use_storage:
            self._save_json(all_records)

        return all_records


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="Scrape Mansfield TX bids (Open + Closed)."
    )
    ap.add_argument("--no-db",      action="store_true",
                    help="Skip Mongo/S3, write local JSON instead.")
    ap.add_argument("--debug",      action="store_true",
                    help="Dump raw HTML of every page to ./debug_html/")
    ap.add_argument("--output-dir", default="output",
                    help="Directory for local JSON output (default: ./output)")
    ap.add_argument("--max-bids",   type=int, default=None,
                    help="Limit bids per status (handy for quick tests).")
    args = ap.parse_args()

    scraper = MansfieldTexasBidScraper(
        use_storage = not args.no_db,
        debug       = args.debug,
        output_dir  = args.output_dir,
        max_bids    = args.max_bids,
    )
    scraper.scrape()


if __name__ == "__main__":
    main()