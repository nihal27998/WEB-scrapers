import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry

try:
    from deep_translator import GoogleTranslator
    HAVE_TRANSLATOR = True
except ImportError:
    HAVE_TRANSLATOR = False

try:
    import boto3
    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False

load_dotenv()

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("platformazakupowa")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL = "https://platformazakupowa.pl"
ORG      = os.getenv("PZ_ORG", "umw.edu")   # organization slug from the URL
TABS     = ("active", "closed")             # which tabs to scrape; also: "ended", "canceled"

LISTING_BASE = f"{BASE_URL}/pn/{ORG}/proceedings"


def listing_url(tab: str, page: int = 1) -> str:
    return (
        f"{LISTING_BASE}?page={page}&tab={tab}"
        f"&query=&sort-field=endsAt&sort-type=DESC"
    )


# Polish → English static label map (fast path; anything not here still goes
# through translate_batch())
LABEL_MAP = {
    "Ogłaszający":                  "Publishing entity",
    "Zamawiający":                  "Contracting authority",
    "Numer postępowania":           "Proceeding number",
    "Numer ogłoszenia":             "Announcement number",
    "Tryb postępowania":            "Procedure",
    "Rodzaj postępowania":          "Proceeding type",
    "Rodzaj zamówienia":            "Order type",
    "Termin składania ofert":       "Offer submission deadline",
    "Termin otwarcia ofert":        "Offer opening date",
    "Data publikacji":              "Publication date",
    "Data zakończenia":             "End date",
    "Kategoria":                    "Category",
    "Przedmiot zamówienia":         "Subject of the order",
    "Opis przedmiotu zamówienia":   "Description of the subject of the order",
    "Miejsce realizacji":           "Place of performance",
    "Osoba do kontaktu":            "Contact person",
    "Adres":                        "Address",
    "Telefon":                      "Phone",
    "E-mail":                       "Email",
    "Województwo":                  "Voivodeship",
    "Wartość zamówienia":           "Order value",
    "Przetarg nieograniczony":      "Open tender",
    "Dostawa":                      "Supply",
    "Usługa":                       "Service",
    "Roboty budowlane":             "Construction works",
    "tak":                          "Yes",
    "nie":                          "No",
}

# Listing tab -> normalized bid status
STATUS_MAP = {
    "active":   "Open",
    "ended":    "Ended",
    "canceled": "Canceled",
    "closed":   "Closed",
}

DOC_EXT_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|zip|rar|7z|pptx?|jpg|jpeg|png|txt|xml|csv)(\?|$)", re.I
)
DOC_KEYWORD_RE = re.compile(
    r"pobierz|download|plik|za[łl]acznik|za[łl][aą]cznik|dokument", re.I
)

# ── Terminy widget labels (Published / Placing offers / Opening of offers) ──
_TERMINY_LABELS = {
    "published_raw": re.compile(r"^(Published|Data\s+publikacji|Publikacja)$", re.I),
    "deadline_raw":  re.compile(r"^(Placing offers|Termin\s+sk[łl]adania\s+ofert)$", re.I),
    "opening_raw":   re.compile(r"^(Opening of offers|Termin\s+otwarcia\s+ofert)$", re.I),
}

# ══════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════


def clean_text(v) -> str | None:
    if v is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(v)).strip()
    return cleaned or None


def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()


def parse_date(raw: str | None, ctx: str = "") -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning(f"Date parse failed '{raw}' [{ctx}]")
        return None


def tr(text: str | None) -> str | None:
    """Map a known Polish label/value to English via the static dict."""
    if text is None:
        return None
    return LABEL_MAP.get(str(text).strip(), str(text).strip())


def translate_batch(texts: list) -> list:
    """Translate a list of Polish strings to English via Google Translate."""
    if not texts or not HAVE_TRANSLATOR:
        return list(texts)
    indices      = [i for i, t in enumerate(texts) if t and str(t).strip()]
    to_translate = [str(texts[i]) for i in indices]
    if not to_translate:
        return list(texts)
    try:
        sep        = " ||| "
        joined     = sep.join(to_translate)
        translated = GoogleTranslator(source="pl", target="en").translate(joined)
        parts      = [p.strip() for p in translated.split("|||")]
        result     = list(texts)
        if len(parts) == len(indices):
            for idx, part in zip(indices, parts):
                result[idx] = part
        else:
            for idx, original in zip(indices, to_translate):
                try:
                    result[idx] = GoogleTranslator(source="pl", target="en").translate(original)
                except Exception:
                    pass
        return result
    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")
        return list(texts)


def translate_one(text: str | None) -> str | None:
    if not text:
        return text
    return translate_batch([text])[0]


# ══════════════════════════════════════════════════════════════════
# SCRAPER
# ══════════════════════════════════════════════════════════════════


class PlatformaZakupowaScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,pl;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self, org: str = ORG, tabs: tuple = TABS):
        self.org  = org
        self.tabs = tabs
        self.listing_base = f"{BASE_URL}/pn/{org}/proceedings"

        # ── HTTP session ──────────────────────────────────────────
        self.session = requests.Session()
        retry   = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update(self._HEADERS)
        self.session.headers["Referer"] = self.listing_base
        logger.info("HTTP session ready")

        # ── MongoDB ───────────────────────────────────────────────
        mongo_uri = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri)
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["platformazakupowa_tenders"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id", unique=True)
        self.col.create_index("proceeding_id")
        self.col.create_index([("status", 1), ("deadline", -1)])
        logger.info("MongoDB connected → tender_bharo.platformazakupowa_tenders")

        # ── S3 ────────────────────────────────────────────────────
        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/platformazakupowa"
        if HAVE_BOTO3 and self.bucket:
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
            logger.info(f"S3 configured: bucket={self.bucket}")
        else:
            self.s3 = None
            logger.info("S3 disabled (no boto3 and/or no S3_BUCKET_NAME) — documents keep source URL only")

        # ── Resume support ────────────────────────────────────────
        self._scraped_ids: set = set()
        self._load_scraped_ids()

        # ── Organization display name (filled on first listing fetch) ──
        self.org_name_pl: str | None = None
        self.org_name_en: str | None = None

        # ── Debug flags ───────────────────────────────────────────
        self._debug_listing_saved = False
        self._debug_detail_saved  = False

    def _load_scraped_ids(self):
        try:
            ids = self.col.distinct("proceeding_id")
            self._scraped_ids = set(str(i) for i in ids)
            logger.info(f"Resume: {len(self._scraped_ids)} proceedings already in DB")
        except Exception as e:
            logger.warning(f"Could not load scraped IDs: {e}")

    # ── TEB ID ────────────────────────────────────────────────────

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_platformazakupowa"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        m   = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{m[now.month]}/{seq:08d}"

    # ── HTTP helpers ──────────────────────────────────────────────

    def _get(self, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 30)
        for attempt in range(1, 4):
            try:
                r = self.session.get(url, **kw)
                r.raise_for_status()
                return r
            except Exception as e:
                logger.warning(f"GET {url} attempt {attempt}/3: {e}")
                if attempt < 3:
                    time.sleep(random.uniform(2, 4) * attempt)
        raise RuntimeError(f"GET {url} failed after 3 attempts")

    def _soup(self, url: str) -> BeautifulSoup:
        r = self._get(url)
        return BeautifulSoup(r.content, "lxml")

    def _sleep(self):
        time.sleep(random.uniform(0.8, 1.8))

    # ── Organization name ────────────────────────────────────────

    def _ensure_org_name(self, soup: BeautifulSoup) -> None:
        """
        Extract + translate the buying organization's display name once per run.
        Source: <title>Supplier Profile - <ORG NAME></title>
        """
        if self.org_name_pl:
            return

        name_pl = None
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text())
            if raw:
                m = re.search(r"Supplier Profile\s*-\s*(.+)", raw, re.I)
                if m:
                    name_pl = clean_text(m.group(1))

        if not name_pl:
            header = soup.find("header", class_="header")
            if header:
                img = header.find("img", alt=True)
                if img and clean_text(img.get("alt", "")).lower() not in ("", "logo"):
                    name_pl = clean_text(img.get("alt"))

        if name_pl:
            self.org_name_pl = name_pl
            self.org_name_en = translate_one(name_pl) or name_pl
            logger.info(f"Organization name: '{name_pl}' -> '{self.org_name_en}'")
        else:
            logger.warning("Could not extract organization name from listing page")

    # ── Listing page parser ──────────────────────────────────────

    def _parse_listing_page(self, soup: BeautifulSoup, tab: str) -> list[dict]:
        """
        Row structure:
          <tbody class="proceedings-table-body">
            <tr>
              <td>ID</td><td>NAME</td><td>BUYER</td><td>END_DATE</td>
              <td>PROCEDURE</td><td class="text-center">TYPE</td>
              <td><a href="/transakcja/ID">Go to</a></td>
            </tr>
          </tbody>
        """
        proceedings = []

        if not self._debug_listing_saved:
            with open("debug_listing.html", "w", encoding="utf-8") as f:
                f.write(soup.prettify())
            logger.info("DEBUG: saved debug_listing.html")
            self._debug_listing_saved = True

        pane = soup.find("div", id=f"tab-{tab}")
        body = pane.find("tbody", class_="proceedings-table-body") if pane else None
        if body is None:
            body = soup.find("tbody", class_="proceedings-table-body")
        if body is None:
            logger.warning(f"  No proceedings table body found for tab='{tab}'")
            return proceedings

        for row in body.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 7:
                continue

            proceeding_id = clean_text(cells[0].get_text())
            link = cells[6].find("a", href=re.compile(r"/transakcja/(\d+)"))
            detail_url = urljoin(BASE_URL, link["href"]) if link else (
                urljoin(BASE_URL, f"/transakcja/{proceeding_id}") if proceeding_id else None
            )
            if not proceeding_id or not detail_url:
                continue

            proceedings.append({
                "proceeding_id": proceeding_id,
                "tab":           tab,
                "title_pl":      clean_text(cells[1].get_text()),
                "buyer_pl":      clean_text(cells[2].get_text()),
                "end_date_raw":  clean_text(cells[3].get_text()),
                "procedure_pl":  clean_text(cells[4].get_text()),
                "type_pl":       clean_text(cells[5].get_text()),
                "detail_url":    detail_url,
            })

        logger.info(f"  Contract rows found on page: {len(proceedings)}")
        return proceedings

    # ── Pagination helper ────────────────────────────────────────

    def _get_total_pages(self, soup: BeautifulSoup) -> int:
        """
        <ul class="pagination">
          <li class="disabled"><a href="?page=1&tab=active..." aria-label="Previous page">«</a></li>
          <li class="active"><a href="?page=1&tab=active...">1</a></li>
          <li><a href="?page=2&tab=active...">2</a></li>
          <li><a href="?page=2&tab=active..." aria-label="Next">»</a></li>
        </ul>
        """
        pagination = soup.find("ul", class_="pagination")
        if not pagination:
            return 1

        max_page = 1
        for a in pagination.find_all("a", href=re.compile(r"page=\d+")):
            link_text = clean_text(a.get_text()) or ""
            if link_text.isdigit():
                m = re.search(r"page=(\d+)", a["href"])
                if m:
                    max_page = max(max_page, int(m.group(1)))
        return max_page

    # ── Terminy widget parser ────────────────────────────────────

    def _parse_terminy(self, soup: BeautifulSoup) -> dict:
        """
        Parses the 'Terminy' widget:
            <div class="proceeding-info-list-container">
              <h3 class="proceeding-info-list-title">Terminy</h3>
              <ul class="proceeding-info-list">
                <li class="proceeding-info-list-item">
                  <div>Published <i .../></div>
                  <div class="flex items-center">
                    <span class="proceeding-info-date">2026-05-18</span>
                    <span class="proceeding-info-time">08:07:00</span>
                  </div>
                </li>
                ...
              </ul>
            </div>
        This widget uses labels + separate date/time spans, so it doesn't
        match the generic 2-cell <tr>/<dt><dd> sweep used elsewhere.
        """
        out = {}

        container = None
        for c in soup.find_all("div", class_="proceeding-info-list-container"):
            title = c.find("h3", class_="proceeding-info-list-title")
            if title and clean_text(title.get_text()) == "Terminy":
                container = c
                break
        if container is None:
            return out

        for li in container.find_all("li", class_="proceeding-info-list-item"):
            divs = li.find_all("div", recursive=False)
            if len(divs) < 2:
                continue

            label = clean_text(divs[0].get_text())
            if not label:
                continue

            date_span = divs[1].find("span", class_="proceeding-info-date")
            time_span = divs[1].find("span", class_="proceeding-info-time")
            date_val  = clean_text(date_span.get_text()) if date_span else None
            time_val  = clean_text(time_span.get_text()) if time_span else None
            if not date_val:
                continue

            raw = f"{date_val} {time_val}" if time_val else date_val

            for key, pattern in _TERMINY_LABELS.items():
                if pattern.match(label):
                    out.setdefault(key, raw)
                    break

        return out

    # ── Detail page parser ───────────────────────────────────────

    def _parse_detail(self, soup: BeautifulSoup, detail_url: str) -> dict:
        if not self._debug_detail_saved:
            with open("debug_detail.html", "w", encoding="utf-8") as f:
                f.write(soup.prettify())
            logger.info("DEBUG: saved debug_detail.html")
            self._debug_detail_saved = True

        result = {"detail_url": detail_url}

        m = re.search(r"/transakcja/(\d+)", detail_url)
        result["proceeding_id"] = m.group(1) if m else None

        # ── Title ───────────────────────────────────────────────
        title_tag = soup.find("title")
        title_pl = None
        if title_tag:
            raw = clean_text(title_tag.get_text())
            if raw:
                raw = re.sub(r"^Proceeding:\s*", "", raw, flags=re.I)
                raw = re.sub(r"\s*-\s*Platforma Zakupowa\s*$", "", raw, flags=re.I)
                title_pl = clean_text(raw)
        result["title_pl"] = title_pl

        # ── Generic label/value sweep (2-cell <tr>, <dt>/<dd>) ────
        pairs = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) == 2:
                label = clean_text(cells[0].get_text())
                value = clean_text(cells[1].get_text())
                if label and value and len(label) < 80:
                    pairs.setdefault(label, value)
        for dt_tag in soup.find_all("dt"):
            dd_tag = dt_tag.find_next_sibling("dd")
            if dd_tag:
                label = clean_text(dt_tag.get_text())
                value = clean_text(dd_tag.get_text())
                if label and value:
                    pairs.setdefault(label, value)
        result["field_pairs_pl"] = pairs

        # ── Terminy widget (Published / Placing offers / Opening of offers) ──
        terminy = self._parse_terminy(soup)
        result["publication_date_raw"] = terminy.get("published_raw")
        result.setdefault("deadline_raw", terminy.get("deadline_raw"))
        result["opening_date_raw"] = terminy.get("opening_raw")

        for label, value in pairs.items():
            if re.search(r"Numer\s+post[eę]powania", label, re.I):
                result["tender_number"] = value
            elif re.search(r"Tryb\s+post[eę]powania", label, re.I):
                result.setdefault("procedure_pl", value)
            elif re.search(r"Termin\s+sk[łl]adania\s+ofert", label, re.I):
                result.setdefault("deadline_raw", value)
            elif re.search(r"Zamawiaj[aą]cy|Ogłaszaj[aą]cy", label, re.I):
                result.setdefault("authority_pl", value)
            elif re.search(r"Miejsce\s+realizacji", label, re.I):
                result.setdefault("place_of_performance_pl", value)

        # ── Description (best effort) ─────────────────────────────
        description_pl = None
        for heading in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
            text = clean_text(heading.get_text())
            if text and re.search(r"Opis\s+przedmiotu|Przedmiot\s+zam[oó]wienia", text, re.I):
                nxt = heading.find_next(["p", "div"])
                if nxt:
                    desc = clean_text(nxt.get_text())
                    if desc and len(desc) > 20:
                        description_pl = desc
                        break
        result["description_pl"] = description_pl

        # ── Documents ──────────────────────────────────────────────
        documents = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = clean_text(a.get_text()) or ""
            is_doc_ext = bool(DOC_EXT_RE.search(href))
            is_doc_kw  = bool(DOC_KEYWORD_RE.search(href) or DOC_KEYWORD_RE.search(text))
            if not (is_doc_ext or is_doc_kw):
                continue
            full_url = urljoin(BASE_URL, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            fname = text or os.path.basename(urlparse(full_url).path) or "document"
            documents.append({
                "name_pl":     fname,
                "name_en":     None,
                "file_url":    full_url,
                "file_name":   os.path.basename(urlparse(full_url).path) or fname,
                "s3_path":     None,
                "uploaded_at": None,
            })
        result["documents"] = documents

        return result

    # ── Translate detail dict ───────────────────────────────────

    def _translate_detail(self, raw: dict) -> dict:
        fields = [
            "title_pl", "description_pl", "procedure_pl",
            "authority_pl", "place_of_performance_pl",
        ]
        values     = [raw.get(f) for f in fields]
        translated = translate_batch(values)

        d = dict(raw)
        for field, trans in zip(fields, translated):
            en_key = field.replace("_pl", "_en")
            static = LABEL_MAP.get((raw.get(field) or "").strip())
            d[en_key] = static if static else trans

        # field_pairs_pl -> field_pairs_en
        pairs = raw.get("field_pairs_pl", {})
        labels_pl = list(pairs.keys())
        values_pl = list(pairs.values())
        labels_en = [LABEL_MAP.get(l.strip()) or translate_one(l) for l in labels_pl]
        values_en = translate_batch(values_pl)
        d["field_pairs_en"] = {
            (le or lp): ve for lp, le, ve in zip(labels_pl, labels_en, values_en)
        }

        # Document names
        doc_names  = [doc.get("name_pl") for doc in raw.get("documents", [])]
        trans_docs = translate_batch(doc_names)
        for doc, name_en in zip(d.get("documents", []), trans_docs):
            doc["name_en"] = name_en

        return d

    # ── Build MongoDB document ──────────────────────────────────

    def _build_doc(self, listing: dict, detail: dict) -> dict:
        pid        = detail.get("proceeding_id") or listing.get("proceeding_id")
        detail_url = detail.get("detail_url") or listing.get("detail_url")

        deadline_raw = detail.get("deadline_raw") or listing.get("end_date_raw")

        def en(pl_key: str):
            en_key = pl_key.replace("_pl", "_en")
            return detail.get(en_key) or detail.get(pl_key)

        title_en     = en("title_pl") or translate_one(listing.get("title_pl"))
        buyer_en     = translate_one(listing.get("buyer_pl"))
        procedure_en = en("procedure_pl") or tr(listing.get("procedure_pl"))
        type_en      = tr(listing.get("type_pl"))

        return {
            # ── Identity ────────────────────────────────────────
            "hash_id":         generate_hash(pid or detail_url),
            "teb_number":      self._teb_id(),
            "proceeding_id":   pid,
            "tender_number":   detail.get("tender_number"),

            # ── Source ──────────────────────────────────────────
            "source":          "Platforma Zakupowa",
            "organization":    self.org,
            "organization_name": self.org_name_en,
            "source_url":      detail_url,
            "portal_url":      BASE_URL,

            # ── Status ──────────────────────────────────────────
            "status": STATUS_MAP.get(listing.get("tab"), listing.get("tab")),

            # ── Title / description ─────────────────────────────
            "title":       title_en,
            "description": en("description_pl"),

            # ── Classification ──────────────────────────────────
            "buyer":                buyer_en,
            "authority":            en("authority_pl"),
            "procedure":            procedure_en,
            "proceeding_type":      type_en,
            "place_of_performance": en("place_of_performance_pl"),

            # ── Dates ───────────────────────────────────────────
            "deadline_raw": deadline_raw,
            "deadline":     parse_date(deadline_raw, "deadline"),
            "publication_date_raw": detail.get("publication_date_raw"),
            "publication_date":     parse_date(detail.get("publication_date_raw"), "publication_date"),
            "opening_date_raw":     detail.get("opening_date_raw"),
            "opening_date":         parse_date(detail.get("opening_date_raw"), "opening_date"),

            # ── Extra fields (translated label -> translated value) ─
            "fields": detail.get("field_pairs_en", {}),

            # ── Documents ───────────────────────────────────────
            "documents": [
                {
                    "title":         doc.get("name_en") or doc.get("name_pl"),
                    "original_url": doc.get("file_url"),
                    "type": "Tender_Document",
                    "file_name":    doc.get("file_name"),
                    "s3_path":      doc.get("s3_path"),
                    "uploaded_at":  doc.get("uploaded_at"),
                }
                for doc in detail.get("documents", [])
            ],

            # ── ETL metadata ────────────────────────────────────
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    # ── S3 upload ────────────────────────────────────────────────

    _EXT_MAP = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "image/jpeg": ".jpg",
        "image/png": ".png",
    }

    def _upload_docs_s3(self, doc: dict, mongo_id) -> None:
        if not self.s3:
            return
        folder  = f"{doc['proceeding_id']}_{mongo_id}"
        updated = []
        for att in doc.get("documents", []):
            url = att.get("original_url")
            if not url:
                updated.append(att)
                continue
            try:
                r  = self._get(url, timeout=60)
                ct = r.headers.get("content-type", "application/octet-stream").split(";")[0]
                fname = att.get("file_name") or os.path.basename(urlparse(url).path) or "document"
                if not os.path.splitext(fname)[1]:
                    fname += self._EXT_MAP.get(ct, ".bin")
                key = f"{self.s3_folder}/{folder}/{fname}"
                self.s3.put_object(Bucket=self.bucket, Key=key,
                                    Body=r.content, ContentType=ct)
                att["s3_path"]     = f"s3://{self.bucket}/{key}"
                att["uploaded_at"] = datetime.now(timezone.utc)
                logger.info(f"    S3 ✓ {fname}")
            except Exception as e:
                logger.warning(f"    S3 failed {url}: {e}")
            updated.append(att)
            self._sleep()
        self.col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})

    # ── Process one proceeding ──────────────────────────────────

    def _process_tender(self, listing_info: dict) -> bool:
        detail_url    = listing_info["detail_url"]
        proceeding_id = listing_info.get("proceeding_id")

        if proceeding_id and proceeding_id in self._scraped_ids:
            logger.info(f"  ↷ skip (already in DB): {proceeding_id}")
            return False

        try:
            soup   = self._soup(detail_url)
            raw    = self._parse_detail(soup, detail_url)
            detail = self._translate_detail(raw)
        except Exception as e:
            logger.error(f"  Detail failed [{detail_url}]: {e}")
            return False

        final_doc = self._build_doc(listing_info, detail)

        try:
            result = self.col.insert_one(final_doc)
            if proceeding_id:
                self._scraped_ids.add(proceeding_id)
            logger.info(
                f"  ✓ {proceeding_id} | TEB={final_doc['teb_number']} "
                f"| status={final_doc['status']} "
                f"| {final_doc['title'] or '(no title)'}"
            )
            if final_doc.get("documents") and self.s3:
                self._upload_docs_s3(final_doc, result.inserted_id)
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": final_doc["hash_id"]},
                {"$set": {**final_doc, "updated_at": datetime.now(timezone.utc)}},
            )
            if proceeding_id:
                self._scraped_ids.add(proceeding_id)
            logger.info(f"  ↺ {proceeding_id} updated (duplicate)")

        self._sleep()
        return True

    # ── Main scrape loop ────────────────────────────────────────

    def scrape(self) -> None:
        logger.info("=" * 60)
        logger.info("Starting Platforma Zakupowa scraper")
        logger.info(f"Org:  {self.org}")
        logger.info(f"Tabs: {self.tabs}")
        logger.info("=" * 60)

        inserted_total = 0

        for tab in self.tabs:
            logger.info(f"── Tab '{tab}'")

            try:
                soup = self._soup(listing_url(tab, page=1))
            except Exception as e:
                logger.error(f"Failed to load listing page for tab='{tab}': {e}")
                continue

            self._ensure_org_name(soup)
            total_pages = self._get_total_pages(soup)
            logger.info(f"  Total pages found: {total_pages}")

            for page_num in range(1, total_pages + 1):
                logger.info(f"  ── Page {page_num}/{total_pages}")

                if page_num == 1:
                    page_soup = soup
                else:
                    try:
                        page_soup = self._soup(listing_url(tab, page=page_num))
                    except Exception as e:
                        logger.error(f"  Page {page_num} load failed: {e}")
                        continue

                proceedings = self._parse_listing_page(page_soup, tab)
                logger.info(f"    Found {len(proceedings)} proceedings on this page")

                for listing_info in proceedings:
                    try:
                        if self._process_tender(listing_info):
                            inserted_total += 1
                    except Exception as e:
                        logger.error(f"    Proceeding error [{listing_info.get('detail_url')}]: {e}")

                logger.info(f"    DB total so far: {len(self._scraped_ids)} unique proceedings")
                time.sleep(random.uniform(1.0, 2.0))

        logger.info("=" * 60)
        logger.info(f"DONE. Inserted/updated this run: {inserted_total}")
        logger.info(f"Unique proceedings in DB: {len(self._scraped_ids)}")
        logger.info("=" * 60)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    PlatformaZakupowaScraper().scrape()