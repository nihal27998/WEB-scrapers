

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
logger = logging.getLogger("ezak_fnbrno")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL  = "https://ezak.fnbrno.cz"
INDEX_URL = (
    f"{BASE_URL}/contract_index.html"
    "?type=all&state=all&archive=ALL&contract_place="
)

# Czech → English static label map
LABEL_MAP = {
    # Phases
    "Příjem nabídek":                         "Receiving Offers",
    "Hodnocení":                              "Evaluation",
    "Zadáno":                                 "Awarded",
    "Zrušeno":                                "Cancelled",
    "Zrušeno (v archivu)":                   "Cancelled (in archive)",
    "Uzavřeno":                               "Closed",
    "Uzavřeno (v archivu)":                  "Closed (in archive)",
    "Objednáno":                              "Ordered",
    "Objednáno (v archivu)":                 "Ordered (in archive)",
    "Zadáno (v archivu)":                    "Awarded (in archive)",
    "Výzva k podání žádostí o účast":         "Call for Applications",
    "Projevení předběžného zájmu":            "Expression of Preliminary Interest",
    "Prokazování kvalifikace":                "Qualification Verification",
    "Příjem žádostí o účast v DNS":           "Receiving DNS Applications",
    "Hodnocení kvalifikace":                  "Qualification Evaluation",
    "Posouzení žádostí o účast v DNS":        "Assessment of DNS Applications",
    "Příjem předběžných nabídek":             "Receiving Preliminary Offers",
    "V jednání":                              "In Negotiation",
    "Vyhodnoceno":                            "Evaluated",
    "Zadávání":                               "Awarding",
    # Regime
    "nadlimitní":                             "Above-threshold",
    "podlimitní":                             "Below-threshold",
    "VZ malého rozsahu":                      "Small-scale Contract",
    "mimo režim ZZVZ":                        "Outside ZZVZ Regime",
    # Contract type
    "Dodávky":                                "Supplies",
    "Služby":                                 "Services",
    "Stavební práce":                         "Works",
    # Procedure type
    "otevřené řízení":                        "Open Procedure",
    "užší řízení":                            "Restricted Procedure",
    "jednací řízení s uveřejněním":           "Competitive Procedure with Negotiation",
    "jednací řízení bez uveřejnění":          "Negotiated Procedure without Publication",
    "zjednodušené podlimitní řízení":         "Simplified Below-threshold Procedure",
    "řízení pro DNS":                         "DNS Procedure",
    "soutěž o návrh":                         "Design Contest",
    # Common values
    "ano":                                    "Yes",
    "ne":                                     "No",
    "neuvedena":                              "Not stated",
}

# Phases → bid_status = "Open"
OPEN_PHASES = {
    "Receiving Offers",
    "Receiving Bids",
}

# Phases → bid_status = "Closed"
CLOSED_PHASES = {
    "Awarded",
    "Awarded (in archive)",
    "Cancelled",
    "Cancelled (in archive)",
    "Canceled",
    "Canceled (in archive)",
    "Closed",
    "Closed (in archive)",
    "Ordered",
    "Ordered (in archive)",
    "Placed",
    "Placed (in archive)",
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
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
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
    """Map a known Czech label to English via static dict."""
    if text is None:
        return None
    return LABEL_MAP.get(str(text).strip(), str(text).strip())


def translate_batch(texts: list) -> list:
    """Translate a list of Czech strings to English via Google Translate."""
    if not texts or not HAVE_TRANSLATOR:
        return texts
    indices      = [i for i, t in enumerate(texts) if t and str(t).strip()]
    to_translate = [str(texts[i]) for i in indices]
    if not to_translate:
        return texts
    try:
        sep        = " ||| "
        joined     = sep.join(to_translate)
        translated = GoogleTranslator(source="cs", target="en").translate(joined)
        parts      = [p.strip() for p in translated.split("|||")]
        result     = list(texts)
        for idx, part in zip(indices, parts):
            result[idx] = part
        return result
    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")
        return texts


# ══════════════════════════════════════════════════════════════════
# SCRAPER
# ══════════════════════════════════════════════════════════════════

class EzakFNBScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self):
        # ── HTTP session ──────────────────────────────────────────
        self.session = requests.Session()
        retry   = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update(self._HEADERS)
        logger.info("HTTP session ready")

        # ── MongoDB ───────────────────────────────────────────────
        mongo_uri = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri)
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["ezak_fnbrno_tenders"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",     unique=True)
        self.col.create_index("contract_id")
        self.col.create_index([("phase", 1), ("start_date", -1)])
        logger.info("MongoDB connected → tender_bharo.ezak_fnbrno_tenders")

        # ── S3 ────────────────────────────────────────────────────
        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/ezak_fnbrno"
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

        # ── Resume support ────────────────────────────────────────
        self._scraped_ids: set = set()
        self._load_scraped_ids()

        # ── Debug flag ────────────────────────────────────────────
        self._debug_listing_saved = False
        self._debug_detail_saved  = False

    def _load_scraped_ids(self):
        try:
            ids = self.col.distinct("contract_id")
            self._scraped_ids = set(ids)
            logger.info(f"Resume: {len(self._scraped_ids)} contracts already in DB")
        except Exception as e:
            logger.warning(f"Could not load scraped IDs: {e}")

    # ── TEB ID ────────────────────────────────────────────────────

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_ezak_fnbrno"},
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
        return BeautifulSoup(r.content, "html.parser")

    def _sleep(self):
        time.sleep(random.uniform(0.8, 1.8))

    # ── Listing page parser ───────────────────────────────────────

    def _parse_listing_page(self, soup: BeautifulSoup) -> list[dict]:
        contracts = []

        if not self._debug_listing_saved:
            with open("debug_listing.html", "w", encoding="utf-8") as f:
                f.write(soup.prettify())
            logger.info("DEBUG: saved debug_listing.html")
            self._debug_listing_saved = True

        all_links = soup.find_all("a", href=re.compile(r"contract_display_\d+\.html"))
        logger.info(f"  Contract links found on page: {len(all_links)}")

        for link_tag in all_links:
            title_cs   = clean_text(link_tag.get_text())
            detail_url = urljoin(BASE_URL, link_tag["href"])

            regime_cs = phase_cs = start_raw = deadline_raw = None

            parent_row = link_tag.find_parent("tr")
            if parent_row:
                next_row = parent_row.find_next_sibling("tr")
                if next_row:
                    cells = next_row.find_all("td")
                    if cells:
                        regime_cs    = clean_text(cells[0].get_text()) if len(cells) > 0 else None
                        phase_cs     = clean_text(cells[1].get_text()) if len(cells) > 1 else None
                        start_raw    = clean_text(cells[2].get_text()) if len(cells) > 2 else None
                        deadline_raw = clean_text(cells[3].get_text()) if len(cells) > 3 else None

                if not regime_cs:
                    cells = parent_row.find_all("td")
                    if len(cells) > 1:
                        regime_cs    = clean_text(cells[1].get_text()) if len(cells) > 1 else None
                        phase_cs     = clean_text(cells[2].get_text()) if len(cells) > 2 else None
                        start_raw    = clean_text(cells[3].get_text()) if len(cells) > 3 else None
                        deadline_raw = clean_text(cells[4].get_text()) if len(cells) > 4 else None

            if not parent_row:
                parent_div = link_tag.find_parent("div")
                if parent_div:
                    text  = parent_div.get_text(" ", strip=True)
                    dates = re.findall(r"\d{1,2}\.\d{1,2}\.\d{4}", text)
                    if dates:
                        start_raw    = dates[0]
                        deadline_raw = dates[1] if len(dates) > 1 else None

            contracts.append({
                "title_cs":       title_cs,
                "detail_url":     detail_url,
                "regime_cs":      regime_cs,
                "phase_cs":       phase_cs,
                "start_date_raw": start_raw,
                "deadline_raw":   deadline_raw,
            })

        return contracts

    # ── Pagination helper ─────────────────────────────────────────

    def _get_total_pages(self, soup: BeautifulSoup) -> int:
        last = soup.find("a", title=re.compile(r"poslední stránka", re.I))
        if last and last.get("href"):
            m = re.search(r"page=(\d+)", last["href"])
            if m:
                return int(m.group(1))
        max_page = 1
        for a in soup.find_all("a", href=re.compile(r"page=\d+")):
            m = re.search(r"page=(\d+)", a["href"])
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    # ── ROT13 decoder ─────────────────────────────────────────────

    @staticmethod
    def _decode_rot13_block(soup: BeautifulSoup, block_id: str) -> str | None:
        """
        The site hides sensitive text (contact person, email) inside script tags:
            $("#infoBlockContact").html(Rot13.convert('Bqq\u011byra\u00ed ...'))
        We find the script, extract the ROT13 string, decode unicode escapes,
        then apply ROT13 to recover the original Czech text.
        """
        import codecs
        pattern = re.compile(
            rf'{re.escape(block_id)}.*?Rot13\.convert\([\'"](.+?)[\'"]\)',
            re.S
        )
        for script in soup.find_all("script"):
            text = script.string or ""
            m = pattern.search(text)
            if m:
                rot13_raw = m.group(1)
                # Decode \uXXXX unicode escapes
                try:
                    rot13_raw = rot13_raw.encode("utf-8").decode("unicode_escape", errors="replace")
                except Exception:
                    pass
                # Apply ROT13 (only rotates ASCII a-z/A-Z; Czech chars pass through)
                decoded = codecs.decode(rot13_raw, "rot_13")
                return clean_text(decoded)
        return None

    # ── Contact fields extractor ──────────────────────────────────

    def _extract_contact_fields(self, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        """
        contact_point  → text under <h4>Adresa kontaktního místa</h4>
                          (bid submission address / e-tool link)
        contact_person → decoded from ROT13 in script tag:
                          $("#infoBlockContact").html(Rot13.convert('...'))
        """
        contact_point  = None
        contact_person = None

        # ── contact_person: ROT13-encoded in a <script> tag ───────
        contact_person = self._decode_rot13_block(soup, '$("#infoBlockContact")')

        # ── contact_point: plain text under the h4 heading ────────
        for h in soup.find_all(["h2", "h3", "h4"]):
            if re.search(r"Adresa kontaktního místa", h.get_text(strip=True), re.I):
                texts = []
                for sibling in h.next_siblings:
                    if hasattr(sibling, "name") and sibling.name in ("h2", "h3", "h4"):
                        break
                    t = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
                    if t:
                        texts.append(t)
                raw = " ".join(texts).strip()
                # Strip Czech label prefix "Nabídky, resp. žádosti o účast podávat na:"
                raw = re.sub(r"^Nabídky[^:]*:\s*", "", raw, flags=re.I).strip()
                contact_point = raw or None
                break

        return contact_point, contact_person

    # ── Detail page parser ────────────────────────────────────────

    def _parse_detail(self, soup: BeautifulSoup, detail_url: str) -> dict:
        if not self._debug_detail_saved:
            with open("debug_detail.html", "w", encoding="utf-8") as f:
                f.write(soup.prettify())
            logger.info("DEBUG: saved debug_detail.html")
            self._debug_detail_saved = True

        result = {"detail_url": detail_url}

        # Contract ID from URL
        m = re.search(r"contract_display_(\d+)\.html", detail_url)
        result["contract_id"] = m.group(1) if m else None

        # Full page text for regex fallbacks
        page_text = soup.get_text(" ", strip=True)

        def rx(pattern, flags=0):
            mm = re.search(pattern, page_text, flags)
            return clean_text(mm.group(1)) if mm else None

        # ── Procurement phase ─────────────────────────────────────
        result["phase_cs"] = rx(
            r"(Příjem nabídek|Hodnocení|Zadáno(?:\s+\(v archivu\))?|"
            r"Zrušeno(?:\s+\(v archivu\))?|Uzavřeno(?:\s+\(v archivu\))?|"
            r"Objednáno(?:\s+\(v archivu\))?|V jednání|Vyhodnoceno|Zadávání|"
            r"Výzva k podání|Projevení předběžného|Prokazování kvalifikace|"
            r"Příjem žádostí|Hodnocení kvalifikace|Posouzení žádostí|Příjem předběžných)"
        )

        # ── Reference numbers ─────────────────────────────────────
        result["dbid"]            = rx(r"DBID[:\s]+(\d+)")
        result["system_number"]   = rx(r"Systémové číslo[:\s]+([\w\d]+)")
        result["evidence_number"] = rx(r"Evidenční číslo zadavatele[:\s]+([\w/\d\-]+)")
        result["law_reference"]   = rx(r"Dle zákona[:\s]+(č\.\s*[\d/\w\s]+?)(?:\s{2,}|$)")
        result["vvz_number"]      = rx(r"Evidenční číslo ve VVZ[:\s]+([\w\d\-]+)")

        # ── Dates ─────────────────────────────────────────────────
        result["start_date_raw"] = rx(r"Datum zahájení[:\s]+([\d.]+)")
        result["deadline_raw"]   = rx(r"Nabídku podat do[:\s]+([\d.]+(?:\s+[\d:]+)?)")

        # ── Title, type, description ──────────────────────────────
        result["title_cs"]         = rx(r"Název[:\s]+(.+?)(?:\s{2,}|Druh veřejné|$)")
        result["contract_type_cs"] = rx(r"Druh veřejné zakázky[:\s]+(.+?)(?:\s{2,}|Stručný|$)")
        result["description_cs"]   = rx(
            r"Stručný popis předmětu[:\s]+(.+?)(?:Druh zadávacího|Druh řízení|Místo plnění|$)",
            re.DOTALL
        )

        # ── Procedure, regime, value ──────────────────────────────
        result["procedure_type_cs"] = rx(r"Druh řízení[:\s]+(.+?)(?:\s{2,}|Režim|$)")
        result["regime_cs"]         = rx(r"Režim veřejné zakázky[:\s]+(.+?)(?:\s{2,}|Předpokládaná|$)")
        result["estimated_value_cs"]= rx(r"Předpokládaná hodnota[:\s]+(.+?)(?:\s{2,}|Místo|$)")

        # ── Place of performance ──────────────────────────────────
        result["place_of_performance_cs"] = rx(
            r"Místo plnění[:\s]+(.+?)(?:\s{2,}|Zadavatel|$)", re.DOTALL
        )

        # ── Contracting authority ─────────────────────────────────
        result["authority_name_cs"]       = rx(r"Úřední název[:\s]+(.+?)(?:\s{2,}|IČO|$)")
        result["authority_ico"]           = rx(r"IČO[:\s]+(\d+)")
        result["authority_address_cs"]    = rx(
            r"Poštovní adresa[:\s]+(.+?)(?:\s{2,}|Název oddělení|Id profilu|Adresa kontaktního|$)",
            re.DOTALL
        )
        result["authority_department_cs"] = rx(r"Název oddělení[:\s]+(.+?)(?:\s{2,}|Id profilu|$)")
        result["authority_vvz_profile"]   = rx(r"Id profilu zadavatele ve VVZ[:\s]+(\d+)")

        # ── Contact point & contact person ────────────────────────
        # Uses dedicated BS4 extractor that walks the exact h4 → sibling structure
        contact_point, contact_person = self._extract_contact_fields(soup)
        result["contact_point_cs"]  = contact_point
        result["contact_person_cs"] = contact_person

        # ── CPV items ─────────────────────────────────────────────
        cpv_items = []
        for heading in soup.find_all(["h2", "h3", "h4", "strong"]):
            if "CPV" in heading.get_text():
                tbl = heading.find_next("table")
                if tbl:
                    for row in tbl.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        if len(cells) >= 2:
                            cpv_items.append({
                                "name_cs":          clean_text(cells[0].get_text()),
                                "name_en":          None,
                                "cpv_code":         clean_text(cells[1].get_text()),
                                "additional_codes": clean_text(cells[2].get_text()) if len(cells) > 2 else None,
                            })
                    break
        result["cpv_items"] = cpv_items

        # ── Documents ─────────────────────────────────────────────
        documents = []
        for heading in soup.find_all(["h2", "h3", "h4"]):
            if re.search(r"Zadávací dokumentace|Veřejné dokumenty", heading.get_text(), re.I):
                tbl = heading.find_next("table")
                if tbl:
                    for row in tbl.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        if not cells:
                            continue
                        name_link      = cells[0].find("a")
                        doc_name_cs    = clean_text(cells[0].get_text())
                        doc_detail_url = urljoin(BASE_URL, name_link["href"]) if name_link else None

                        dl_cell   = cells[2] if len(cells) > 2 else (cells[1] if len(cells) > 1 else None)
                        dl_link   = dl_cell.find("a") if dl_cell else None
                        file_url  = urljoin(BASE_URL, dl_link["href"]) if dl_link else None
                        file_name = clean_text(dl_link.get_text()) if dl_link else None
                        size_cs   = clean_text(cells[3].get_text()) if len(cells) > 3 else None

                        documents.append({
                            "name_cs":     doc_name_cs,
                            "name_en":     None,
                            "detail_url":  doc_detail_url,
                            "file_url":    file_url,
                            "file_name":   file_name,
                            "size":        size_cs,
                            "s3_path":     None,
                            "uploaded_at": None,
                        })
                break
        result["documents"] = documents

        # ── URL links ─────────────────────────────────────────────
        url_links = []
        for heading in soup.find_all(["h2", "h3", "h4"]):
            if re.search(r"URL", heading.get_text(), re.I):
                tbl = heading.find_next("table")
                if tbl:
                    for row in tbl.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        if len(cells) >= 2:
                            link_tag = cells[1].find("a")
                            url_links.append({
                                "name_cs": clean_text(cells[0].get_text()),
                                "name_en": None,
                                "url":     link_tag["href"] if link_tag else clean_text(cells[1].get_text()),
                            })
                break
        result["url_links"] = url_links

        return result

    # ── Translate detail dict ─────────────────────────────────────

    def _translate_detail(self, raw: dict) -> dict:
        fields = [
            "title_cs",
            "description_cs",
            "contract_type_cs",
            "procedure_type_cs",
            "regime_cs",
            "estimated_value_cs",
            "place_of_performance_cs",
            "authority_name_cs",
            "authority_address_cs",
            "authority_department_cs",
            "phase_cs",
            "contact_point_cs",
            "contact_person_cs",
        ]

        values     = [raw.get(f) for f in fields]
        translated = translate_batch(values)

        d = dict(raw)
        for field, trans in zip(fields, translated):
            en_key = field.replace("_cs", "_en")
            static = tr(raw.get(field))
            d[en_key] = static if (static and static != raw.get(field)) else trans

        # CPV names
        cpv_names = [c.get("name_cs") for c in raw.get("cpv_items", [])]
        trans_cpv = translate_batch(cpv_names)
        for item, name_en in zip(d.get("cpv_items", []), trans_cpv):
            item["name_en"] = name_en

        # Document names
        doc_names  = [doc.get("name_cs") for doc in raw.get("documents", [])]
        trans_docs = translate_batch(doc_names)
        for doc, name_en in zip(d.get("documents", []), trans_docs):
            doc["name_en"] = name_en

        # URL link names
        link_names  = [lnk.get("name_cs") for lnk in raw.get("url_links", [])]
        trans_links = translate_batch(link_names)
        for lnk, name_en in zip(d.get("url_links", []), trans_links):
            lnk["name_en"] = name_en

        return d

    # ── Build MongoDB document ────────────────────────────────────

    def _build_doc(self, listing: dict, detail: dict) -> dict:
        contract_id  = detail.get("contract_id") or ""
        detail_url   = detail.get("detail_url", "")
        start_raw    = detail.get("start_date_raw") or listing.get("start_date_raw")
        deadline_raw = detail.get("deadline_raw")   or listing.get("deadline_raw")

        def en(cs_key: str):
            en_key = cs_key.replace("_cs", "_en")
            return detail.get(en_key) or detail.get(cs_key)

        # ── Bid status ────────────────────────────────────────────
        phase_en = en("phase_cs") or tr(listing.get("phase_cs")) or ""
        logger.info(f"  PHASE DEBUG: phase_en='{phase_en}'")

        if phase_en in OPEN_PHASES:
            bid_status = "Open"
        elif phase_en in CLOSED_PHASES:
            bid_status = "Closed"
        else:
            # Final fallback: check deadline
            deadline_dt = parse_date(deadline_raw, "deadline")
            if deadline_dt and deadline_dt < datetime.now(timezone.utc):
                bid_status = "Closed"
            else:
                bid_status = "Open"

        return {
            # ── Identity ──────────────────────────────────────────
            "hash_id":          generate_hash(contract_id or detail_url),
            "teb_number":       self._teb_id(),
            "contract_id":      contract_id,
            "dbid":             detail.get("dbid"),
            "system_number":    detail.get("system_number"),
            "evidence_number":  detail.get("evidence_number"),
            "vvz_number":       detail.get("vvz_number"),
            "law_reference":    detail.get("law_reference"),

            # ── Bid status ────────────────────────────────────────
            "bid_status":       bid_status,

            # ── Source ────────────────────────────────────────────
            "source":           "EZAK FNBrno (Fakultní nemocnice Brno)",
            "source_url":       detail_url,
            "portal_url":       BASE_URL,

            # ── Title / description ───────────────────────────────
            "title":            en("title_cs") or listing.get("title_cs"),
            "description":      en("description_cs"),

            # ── Classification ────────────────────────────────────
            "contract_type":    en("contract_type_cs"),
            "regime":           en("regime_cs") or tr(listing.get("regime_cs")),
            "procedure_type":   en("procedure_type_cs"),

            # ── Phase ─────────────────────────────────────────────
            "phase":            phase_en,

            # ── Value ─────────────────────────────────────────────
            "estimated_value":  en("estimated_value_cs"),

            # ── Dates ─────────────────────────────────────────────
            "start_date":       parse_date(start_raw,    "start_date"),
            "start_date_raw":   start_raw,
            "deadline":         parse_date(deadline_raw, "deadline"),
            "deadline_raw":     deadline_raw,

            # ── Place ─────────────────────────────────────────────
            "place_of_performance": en("place_of_performance_cs"),

            # ── Contracting authority ─────────────────────────────
            "authority_name":        en("authority_name_cs"),
            "authority_ico":         detail.get("authority_ico"),
            "authority_address":     en("authority_address_cs"),
            "authority_department":  en("authority_department_cs"),
            "authority_vvz_profile": detail.get("authority_vvz_profile"),

            # ── Contact ───────────────────────────────────────────
            # contact_point  = address/method to submit bids (under "Adresa kontaktního místa")
            # contact_person = named contact person          (under "Kontakt")
            "contact_point":  en("contact_point_cs"),
            "contact_person": en("contact_person_cs"),

            # ── CPV items ─────────────────────────────────────────
            "cpv_items": [
                {
                    "name":             item.get("name_en") or item.get("name_cs"),
                    "cpv_code":         item.get("cpv_code"),
                    "additional_codes": item.get("additional_codes"),
                }
                for item in detail.get("cpv_items", [])
            ],

            # ── Documents ─────────────────────────────────────────
             "documents": [
                {
                    "name":        doc.get("name_en") or doc.get("name_cs"),
                    "detail_url":  doc.get("detail_url"),
                    "original_url":    doc.get("file_url"),
                    "title":   doc.get("file_name"),
                    "type":"Tender_document",
                    "s3_path":     doc.get("s3_path"),
                    "uploaded_at": doc.get("uploaded_at"),
                }
                for doc in detail.get("documents", [])
            ],

            # ── URL links ─────────────────────────────────────────
            "url_links": [
                {
                    "name": lnk.get("name_en") or lnk.get("name_cs"),
                    "url":  lnk.get("url"),
                }
                for lnk in detail.get("url_links", [])
            ],

            # ── ETL metadata ──────────────────────────────────────
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    # ── S3 upload ─────────────────────────────────────────────────

    def _upload_docs_s3(self, doc: dict, mongo_id) -> None:
        if not self.s3:
            return
        folder  = f"{doc['contract_id']}_{mongo_id}"
        updated = []
        for att in doc.get("documents", []):
            url = att.get("file_url")
            if not url:
                updated.append(att)
                continue
            try:
                r  = self._get(url, timeout=60)
                ct = r.headers.get("content-type", "application/octet-stream").split(";")[0]
                fname = att.get("file_name") or os.path.basename(urlparse(url).path) or "document"
                ext_map = {
                    "application/pdf": ".pdf",
                    "application/zip": ".zip",
                    "application/msword": ".doc",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                    "application/vnd.ms-excel": ".xls",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                }
                if not os.path.splitext(fname)[1]:
                    fname += ext_map.get(ct, ".bin")
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

    # ── Process one contract ──────────────────────────────────────

    def _process_contract(self, listing_info: dict) -> bool:
        detail_url  = listing_info["detail_url"]
        m           = re.search(r"contract_display_(\d+)\.html", detail_url)
        contract_id = m.group(1) if m else None

        if contract_id and contract_id in self._scraped_ids:
            logger.info(f"  ↷ skip (already in DB): {contract_id}")
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
            if contract_id:
                self._scraped_ids.add(contract_id)
            logger.info(
                f"  ✓ {contract_id} | TEB={final_doc['teb_number']} "
                f"| bid_status={final_doc['bid_status']} "
                f"| contact_point={final_doc.get('contact_point')} "
                f"| {final_doc['title'] or '(no title)'}"
            )
            if final_doc.get("documents") and self.s3:
                self._upload_docs_s3(final_doc, result.inserted_id)
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": final_doc["hash_id"]},
                {"$set": {**final_doc, "updated_at": datetime.now(timezone.utc)}},
            )
            if contract_id:
                self._scraped_ids.add(contract_id)
            logger.info(f"  ↺ {contract_id} updated (duplicate)")

        self._sleep()
        return True

    # ── Main scrape loop ──────────────────────────────────────────

    def scrape(self) -> None:
        logger.info("=" * 60)
        logger.info("Starting EZAK FNBrno scraper")
        logger.info(f"Index URL: {INDEX_URL}")
        logger.info("=" * 60)

        try:
            soup = self._soup(INDEX_URL)
        except Exception as e:
            logger.error(f"Failed to load index page: {e}")
            return

        total_pages = self._get_total_pages(soup)
        logger.info(f"Total pages found: {total_pages}")

        inserted_total = 0

        for page_num in range(1, total_pages + 1):
            logger.info(f"── Page {page_num}/{total_pages}")

            if page_num == 1:
                page_soup = soup
            else:
                page_url = (
                    f"{BASE_URL}/contract_index.html"
                    f"?archive=ALL&state=all&type=all&page={page_num}"
                )
                try:
                    page_soup = self._soup(page_url)
                except Exception as e:
                    logger.error(f"Page {page_num} load failed: {e}")
                    continue

            contracts = self._parse_listing_page(page_soup)
            logger.info(f"  Found {len(contracts)} contracts on this page")

            for listing_info in contracts:
                try:
                    if self._process_contract(listing_info):
                        inserted_total += 1
                except Exception as e:
                    logger.error(f"  Contract error [{listing_info.get('detail_url')}]: {e}")

            logger.info(f"  DB total so far: {len(self._scraped_ids)} unique contracts")
            time.sleep(random.uniform(1.0, 2.0))

        logger.info("=" * 60)
        logger.info(f"DONE. Inserted/updated this run: {inserted_total}")
        logger.info(f"Unique contracts in DB: {len(self._scraped_ids)}")
        logger.info("=" * 60)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    EzakFNBScraper().scrape()