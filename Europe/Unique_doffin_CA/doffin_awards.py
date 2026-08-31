import hashlib
import json
import logging
import os
import re
import time
import random
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry
from deep_translator import GoogleTranslator

try:
    from curl_cffi import requests as cf_requests
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False

try:
    import boto3
    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("doffin_awards")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

PORTAL_BASE = "https://www.doffin.no"
API_BASE    = "https://api.doffin.no/webclient/api/v2"
SEARCH_URL  = f"{API_BASE}/search-api/search"
DETAIL_URL  = f"{API_BASE}/notices-api/notices"

PAGE_SIZE = 100
HARD_CAP  = 1000

AWARD_NOTICE_TYPES = ["ANNOUNCEMENT_OF_CONCLUSION_OF_CONTRACT"]

STATUS_DISPLAY = {
    "ACTIVE":       "OPEN",
    "DISCONTINUED": "CLOSED",
    "AWARDED":      "AWARDED",
    "CANCELLED":    "CANCELLED",
    "EXPIRED":      "CLOSED",
}

LABEL_MAP = {
    "Kjøper":                            "Buyer",
    "Prosedyre":                         "Procedure",
    "Hensikt":                           "Purpose",
    "Kontrakt":                          "Contract",
    "Resultater":                        "Results",
    "Tilbud":                            "Tender",
    "Endring":                           "Modification",
    "Generelt":                          "General",
    "Offisielt navn":                    "Official name",
    "Juridisk type kjøper":              "Legal type of buyer",
    "Oppdragsgivers virksomhet":         "Contracting authority activity",
    "Tittel":                            "Title",
    "Beskrivelse":                       "Description",
    "Intern identifikator":              "Internal identifier",
    "Kontraktens art":                   "Nature of contract",
    "Hoved klassifisering":              "Main classification (CPV)",
    "Ytterligere klassifisering":        "Additional classification (CPV)",
    "Sted for gjennomføring":            "Place of performance",
    "Estimert verdi":                    "Estimated value",
    "Varighet":                          "Duration",
    "Frist":                             "Deadline",
    "Tilbudsfrist":                      "Tender deadline",
    "Kunngjøringsdato":                  "Publication date",
    "Kontaktpunkt":                      "Contact point",
    "E-postadresse":                     "Email address",
    "E-post":                            "Email",
    "Nettadresse":                       "Website",
    "Internett-adresse":                 "Website",
    "Telefon":                           "Phone",
    "Adresse":                           "Address",
    "Postadresse":                       "Postal address",
    "Postnummer":                        "Postal code",
    "By":                                "City",
    "Sted":                              "City",
    "Land":                              "Country",
    "Underenhet i land":                 "Region",
    "Organisasjonsnummer":               "Organization number",
    "Vinnende tilbud":                   "Winning tender",
    "Dato for kontrakt":                 "Contract award date",
    "Antall tilbud mottatt":             "Number of tenders received",
    "Kontraktverdi":                     "Contract value",
    "Leverandør":                        "Supplier",
    "Tildelt leverandør":                "Awarded supplier",
    "Tildelingsdato":                    "Award date",
    "Antall tilbud":                     "Number of tenders",
    "Laveste tilbud":                    "Lowest tender",
    "Høyeste tilbud":                    "Highest tender",
    "Kontraktsum":                       "Contract sum",
    "Rammekontrakt":                     "Framework contract",
    "Delkontrakt":                       "Lot",
    "Delkontraktnummer":                 "Lot number",
    "Delkontraktstittel":                "Lot title",
    "Prosedyretype":                     "Procedure type",
    "Rollene til denne virksomheten":    "Roles of this organization",
    "Virksomheter":                      "Organizations",
    "Kunngjøringsinformasjon":           "Notice Information",
    "Varselidentifikator/-versjon":      "Notice identifier/version",
    "Type skjema":                       "Form type",
    "Type varsel":                       "Notice type",
    "Varsel utsendelsesdato":            "Notice dispatch date",
    "Varsel om utsendelsesdato (eSender)": "Notice dispatch date (eSender)",
    "Språk der denne kunngjøringen er offisielt tilgjengelig": "Language(s) of official availability",
    "Sentral statlig myndighet":         "Central government authority",
    "Kommunale myndigheter":             "Municipal authorities",
    "Offentlig orden og trygghet":       "Public order and safety",
    "Alminnelig offentlig tjenesteyting": "General public services",
    "Ikke stedbunden":                   "Not location-bound",
    "Varer":                             "Supplies / Goods",
    "Tjenester":                         "Services",
    "Bygg- og anleggsarbeid":            "Works / Construction",
    "Ja":                                "Yes",
    "Nei":                               "No",
    "Norge":                             "Norway",
    "Hvor som helst i det gitte landet": "Anywhere in the given country",
    "Kunngjøring av konkurranse":        "Announcement of Competition",
    "Konkurransegrunnlag":               "Competition Documents",
    "Tildelingskunngjøring":             "Contract Award Notice",
    "Forhåndskunngjøring":               "Prior Information Notice",
    "Veiledende kunngjøring":            "Advisory Notice",
    "Planlegginsgkunngjøring":           "Planning Notice",
    "Kvalifikasjonssystem":              "Qualification System",
}

NOTICE_TYPE_MAP = {
    "ADVISORY_NOTICE":                           "Advisory Notice / Request for Information",
    "PLANNING":                                  "Planning Notice",
    "PRIOR_INFORMATION_NOTICE":                  "Prior Information Notice",
    "CONTRACT_NOTICE":                           "Contract Notice",
    "CONTRACT_AWARD_NOTICE":                     "Contract Award Notice",
    "COMPETITION":                               "Competition / Tender",
    "ANNOUNCEMENT_OF_COMPETITION":               "Announcement of Competition",
    "DYNAMIC_PURCHASING_SCHEME":                 "Dynamic Purchasing Scheme",
    "RESULT":                                    "Result / Award",
    "DESIGN_CONTEST":                            "Design Contest",
    "RESULTS_OF_DESIGN_CONTEST":                 "Results of Design Contest",
    "MODIFICATION_NOTICE":                       "Modification Notice",
    "VOLUNTARY_EX_ANTE_TRANSPARENCY_NOTICE":     "Voluntary Ex-Ante Transparency Notice",
    "CONCESSION_AWARD":                          "Concession Award",
    "QUALIFICATION_SYSTEM":                      "Qualification System",
    "SIMPLIFIED_CONTRACT_NOTICE":                "Simplified Contract Notice",
    "CALL_FOR_COMPETITION_SIMPLIFIED":           "Simplified Call for Competition",
    "ANNOUNCEMENT_OF_CONCLUSION_OF_CONTRACT":    "Announcement of Contract Award",
}

CPV_TOP = {
    "03": "Agricultural, farming, fishing and forestry products",
    "09": "Petroleum products, fuel and electricity",
    "14": "Mining, basic metals and related products",
    "15": "Food, beverages, tobacco and related products",
    "22": "Printed matter and related products",
    "24": "Chemical products",
    "30": "Office and computing machinery",
    "31": "Electrical machinery and apparatus",
    "32": "Radio, television, communications equipment",
    "33": "Medical equipment, pharmaceuticals",
    "34": "Transport equipment",
    "35": "Security, fire-fighting, police and defence equipment",
    "38": "Laboratory, optical and precision equipment",
    "39": "Furniture, household goods",
    "42": "Industrial machinery",
    "44": "Construction structures and materials",
    "45": "Construction work",
    "48": "Software package and information systems",
    "50": "Repair and maintenance services",
    "60": "Transport services",
    "64": "Postal and telecommunications services",
    "65": "Public utilities",
    "66": "Financial and insurance services",
    "70": "Real estate services",
    "71": "Architectural, construction and engineering services",
    "72": "IT services: consulting, software development",
    "73": "Research and development services",
    "75": "Administration, defence and social security services",
    "79": "Business services",
    "80": "Education and training services",
    "85": "Health and social work services",
    "90": "Sewage, refuse, cleaning services",
    "92": "Recreational, cultural and sporting services",
    "98": "Other community, social and personal services",
}

# ══════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════

def clean_text(v) -> str | None:
    if v is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(v)).strip()
    return cleaned or None

def clean_number(v) -> float | None:
    """Strip currency formatting (commas, spaces, NBSPs, NOK suffixes, ...) -> float."""
    if v is None:
        return None
    digits = re.sub(r"[^\d.]", "", str(v))
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None

def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()

def parse_date(raw, ctx="") -> datetime | None:
    if not raw:
        return None
    try:
        # dayfirst=True: Doffin/eForms dates are Norwegian-formatted (DD.MM.YYYY).
        # Without this, ambiguous dates like "08.05.2026" (8 May) can silently
        # mis-parse as month=08 (August).
        dt = dateutil_parser.parse(str(raw).strip(), dayfirst=True)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.warning(f"Date parse failed '{raw}' [{ctx}]: {e}")
        return None

def tr(text) -> str | None:
    if text is None:
        return None
    return LABEL_MAP.get(str(text), str(text))

def tr_notice_type(nt) -> str | None:
    if not nt:
        return None
    return NOTICE_TYPE_MAP.get(str(nt), str(nt).replace("_", " ").title())

def cpv_desc(code) -> str | None:
    if not code or len(str(code)) < 2:
        return None
    return CPV_TOP.get(str(code)[:2])

def detect_portal(url) -> str | None:
    if not url:
        return None
    lower = url.lower()
    if "mercell"   in lower: return "Mercell"
    if "eu-supply" in lower: return "EU Supply"
    if "eusupply"  in lower: return "EU Supply"
    if "artifik"   in lower: return "Artifik CTM"
    if "visma"     in lower: return "Visma"
    if "anbud365"  in lower: return "Anbud365"
    if "ibinder"   in lower: return "iBinder"
    if "mercado"   in lower: return "Mercado"
    try:
        return urlparse(url).netloc or None
    except Exception:
        return None

def translate_batch(texts: list) -> list:
    if not texts:
        return texts
    indices      = []
    to_translate = []
    for i, t in enumerate(texts):
        if t and str(t).strip():
            indices.append(i)
            to_translate.append(str(t))
    if not to_translate:
        return texts

    result = list(texts)
    try:
        separator  = " ||| "
        chunks     = []
        chunk      = []
        chunk_len  = 0
        chunk_idxs = []
        idx_chunks = []

        for i, text in zip(indices, to_translate):
            piece = (separator if chunk else "") + text
            if chunk_len + len(piece) > 4500 and chunk:
                chunks.append(chunk)
                idx_chunks.append(chunk_idxs)
                chunk      = [text]
                chunk_idxs = [i]
                chunk_len  = len(text)
            else:
                chunk.append(text)
                chunk_idxs.append(i)
                chunk_len += len(piece)

        if chunk:
            chunks.append(chunk)
            idx_chunks.append(chunk_idxs)

        for texts_chunk, idxs_chunk in zip(chunks, idx_chunks):
            joined     = separator.join(texts_chunk)
            translated = GoogleTranslator(source="auto", target="en").translate(joined)
            parts      = [p.strip() for p in translated.split("|||")]
            for idx, part in zip(idxs_chunk, parts):
                result[idx] = part

    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")

    return result

# ══════════════════════════════════════════════════════════════════
# SCRAPER
# ══════════════════════════════════════════════════════════════════

class DoffinAwardScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin":          PORTAL_BASE,
        "Referer":         f"{PORTAL_BASE}/",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-site",
        "Connection":      "keep-alive",
    }

    def __init__(self):
        self.logger = logging.getLogger("doffin_awards")

        if HAVE_CURL_CFFI:
            self.session      = cf_requests.Session()
            self._impersonate = "chrome124"
            self.logger.info("Session: curl_cffi (chrome124)")
        else:
            self.session = requests.Session()
            retry   = Retry(total=3, backoff_factor=2, status_forcelist=[429, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self._impersonate = None
            self.logger.info("Session: requests")
        self.session.headers.update(self._HEADERS)

        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["doffin_awards"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",  unique=True)
        self.col.create_index("notice_id")
        self.col.create_index([("status", 1), ("award_date", -1)])
        self.col.create_index("linked_competition_id")
        self.logger.info("MongoDB connected → doffin_awards")

        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/doffin_awards"
        if HAVE_BOTO3 and self.bucket:
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
            self.logger.info(f"S3 configured: bucket={self.bucket}")
        else:
            self.s3 = None

        self._scraped_ids: set[str] = set()
        self._load_scraped_ids()
        self._debug_detail_saved = False

    def _load_scraped_ids(self):
        try:
            ids = self.col.distinct("notice_id")
            self._scraped_ids = set(ids)
            self.logger.info(f"Resume: {len(self._scraped_ids)} award notices already in DB")
        except Exception as e:
            self.logger.warning(f"Could not load scraped IDs: {e}")

    # ── HTTP helpers ─────────────────────────────────────────────

    def _get(self, url, **kw):
        kw.setdefault("timeout", 30)
        if self._impersonate:
            kw["impersonate"] = self._impersonate
        for attempt in range(1, 4):
            try:
                r = self.session.get(url, **kw)
                r.raise_for_status()
                return r
            except Exception as e:
                self.logger.warning(f"GET {url} attempt {attempt}/3: {e}")
                if attempt < 3:
                    time.sleep(random.uniform(2, 4) * attempt)
        raise RuntimeError(f"GET {url} failed after 3 attempts")

    def _post(self, url, **kw):
        kw.setdefault("timeout", 30)
        if self._impersonate:
            kw["impersonate"] = self._impersonate
        for attempt in range(1, 4):
            try:
                r = self.session.post(url, **kw)
                r.raise_for_status()
                return r
            except Exception as e:
                self.logger.warning(f"POST {url} attempt {attempt}/3: {e}")
                if attempt < 3:
                    time.sleep(random.uniform(2, 4) * attempt)
        raise RuntimeError(f"POST {url} failed after 3 attempts")

    def _sleep(self):
        time.sleep(random.uniform(0.8, 1.8))

    # ── TEB ID ───────────────────────────────────────────────────

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_doffin_awards"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        m   = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/AW/{now.year}/{m[now.month]}/{seq:08d}"

    # ── Search API ───────────────────────────────────────────────

    def _build_body(self, page: int,
                    notice_type: str,
                    cpv: str | None = None,
                    location: str | None = None) -> dict:
        return {
            "numHitsPerPage": PAGE_SIZE,
            "page":           page,
            "searchString":   "",
            "sortBy":         "RELEVANCE",
            "facets": {
                "cpvCodesLabel": {"checkedItems": []},
                "cpvCodesId":    {"checkedItems": [cpv] if cpv else []},
                "type":          {"checkedItems": [notice_type]},
                "status":        {"checkedItems": []},
                "location":      {"checkedItems": [location] if location else []},
            },
        }

    def _search(self, page: int,
                notice_type: str,
                cpv: str | None = None,
                location: str | None = None) -> dict:
        body = self._build_body(page, notice_type, cpv=cpv, location=location)
        resp = self._post(SEARCH_URL, json=body,
                          headers={"Content-Type": "application/json"})
        return resp.json()

    def _get_facet_ids(self, data: dict, facet_name: str) -> list[str]:
        try:
            items = data["facets"][facet_name]["items"]
            return [i["id"] for i in items if i.get("id")]
        except (KeyError, TypeError):
            return []

    # ── Detail API ───────────────────────────────────────────────

    def _fetch_detail(self, notice_id: str) -> dict | None:
        url = f"{DETAIL_URL}/{notice_id}"
        try:
            resp = self._get(url)
            data = resp.json()
            if not self._debug_detail_saved:
                with open("debug_doffin_award_detail.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self.logger.info("DEBUG: saved debug_doffin_award_detail.json")
                self._debug_detail_saved = True
            return data
        except Exception as e:
            self.logger.error(f"Detail fetch failed [{notice_id}]: {e}")
            return None

    # ── eForm flattener (display copy: eform_fields, English) ────

    def _flatten_eform(self, eform: list | None) -> list[dict]:
        raw_entries = []

        def _walk(sections, depth=0):
            if not sections:
                return
            for sec in sections:
                label_no = sec.get("label")
                value_no = sec.get("value")
                label_en = tr(label_no)
                value_en = tr(value_no)
                raw_entries.append({
                    "label_no": label_no,
                    "label_en": label_en,
                    "value_no": value_no,
                    "value_en": value_en,
                    "depth":    depth,
                })
                _walk(sec.get("sections"), depth + 1)

        _walk(eform)

        needs_label = [e["label_no"] if e["label_en"] == e["label_no"] else None for e in raw_entries]
        needs_value = [e["value_no"] if e["value_en"] == e["value_no"] else None for e in raw_entries]

        translated_labels = translate_batch(needs_label)
        translated_values = translate_batch(needs_value)

        result = []
        for i, entry in enumerate(raw_entries):
            label = translated_labels[i] if translated_labels[i] else entry["label_en"]
            value = translated_values[i] if translated_values[i] else entry["value_en"]
            if label and value:
                result.append({"label": label, "value": value, "depth": entry["depth"]})

        return result

    # ── Generic eform tree helpers ────────────────────────────────

    def _find_block(self, eform: list | None, title: str, label: str) -> dict | None:
        """Find a top-level eform block by its title id (e.g. 'block06') or its
        Norwegian label (e.g. 'Resultater')."""
        for block in (eform or []):
            if block.get("title") == title or block.get("label") == label:
                return block
        return None

    def _flatten_no(self, sections, depth: int = 0) -> list[tuple]:
        """Flatten a `sections` subtree into ordered (label_no, value_no, depth)
        tuples, Norwegian/raw, preserving document order. This is the same shape
        as _flatten_eform's internal walk but scoped to a single block and not
        translated (translation is done separately, in bulk, by the caller)."""
        items = []
        if not sections:
            return items
        for sec in sections:
            label = sec.get("label")
            value = sec.get("value")
            if label and value:
                items.append((label, value, depth))
            nested = sec.get("sections")
            if nested:
                items.extend(self._flatten_no(nested, depth + 1))
        return items

    # ── block05 "Virksomheter" → name -> org contact details ─────

    @staticmethod
    def _norm_name(name) -> str | None:
        """Normalize a company name for lookup matching: collapse whitespace,
        strip, uppercase. Doffin cross-references winners between block06 and
        block05 by raw name text only (no shared ID), so small formatting
        differences ('Set Elektro AS ' vs 'SET ELEKTRO AS') would otherwise
        cause a silent lookup miss."""
        if not name:
            return None
        return re.sub(r"\s+", " ", str(name)).strip().upper()

    def _build_org_lookup(self, eform: list | None) -> dict:
        """Map normalized 'Offisielt navn' -> dict of contact details, sourced
        from block05 ('Virksomheter'). Award-result winners in block06 are
        referenced only by name, so this is needed to attach org
        number/address/email/etc. to a winner found in block06."""
        block = self._find_block(eform, "block05", "Virksomheter")
        lookup = {}
        if not block:
            return lookup

        for org in (block.get("sections") or []):
            flat = self._flatten_no(org.get("sections") or [])
            fields = {}
            for label, value, _ in flat:
                fields.setdefault(label, value)

            name = fields.get("Offisielt navn")
            if not name:
                continue

            lookup[self._norm_name(name)] = {
                "org_no":      clean_text(fields.get("Organisasjonsnummer")),
                "address":     clean_text(fields.get("Postadresse")),
                "postal_code": clean_text(fields.get("Postnummer")),
                "city":        clean_text(fields.get("By") or fields.get("Sted")),
                "country":     clean_text(tr(fields.get("Land"))),
                "email":       clean_text(fields.get("E-post") or fields.get("E-postadresse")),
                "phone":       clean_text(fields.get("Telefon")),
                "website":     clean_text(fields.get("Internett-adresse") or fields.get("Nettadresse")),
            }

        return lookup

    # ── block06 "Resultater" → real award results ─────────────────

    def _parse_award_results(self, eform: list | None) -> dict:
        """
        Parses block06 ('Resultater'), Doffin's actual award results.

        Returns {"total_value": <raw string or None>, "lots": [...]}.

        "total_value" is the procedure-level "Value of all contracts awarded
        in this procedure" figure — it sits OUTSIDE any per-lot group, so it's
        captured independently of the per-lot loop below.

        Each per-lot group is anchored by a field that translates to roughly
        "Identifier for (profit/winning/result) lot" and contains:
          - the framework/contract value fields for that lot,
          - the WINNING supplier (the first 'Offisielt navn' in the group,
            which has nested contract-identifier / date fields under it),
          - any losing tenderers (subsequent bare 'Offisielt navn' entries),
          - the number of tenders received.

        Matching is done on the *translated* label text (not a guessed
        Norwegian original) because most of these labels aren't in LABEL_MAP
        and get machine-translated on the fly — same as eform_fields does.
        Supplier/company names themselves are NEVER run through translation:
        machine translation can corrupt proper nouns (e.g. "SET ELEKTRO AS"
        coming back as "US ELECTRO SET"), and Norwegian company suffixes
        (AS, ASA, SA) don't need translating anyway.
        """
        block = self._find_block(eform, "block06", "Resultater")
        if not block:
            return {"total_value": None, "lots": []}

        flat = self._flatten_no(block.get("sections") or [])
        if not flat:
            return {"total_value": None, "lots": []}

        labels_no = [f[0] for f in flat]
        mapped    = [tr(l) for l in labels_no]
        needs_translation = [
            labels_no[i] if mapped[i] == labels_no[i] else None
            for i in range(len(labels_no))
        ]
        translated = translate_batch(needs_translation)
        labels_en  = [
            translated[i] if translated[i] else mapped[i]
            for i in range(len(labels_no))
        ]

        total_value: str | None = None
        records: list[dict] = []
        current = None

        for (label_no, value, depth), label_en in zip(flat, labels_en):
            l = (label_en or "").lower()

            if "value of all contracts awarded" in l or (
                "total" in l and "value" in l and "contract" in l
            ):
                total_value = clean_text(value)
                continue

            if "profit lot" in l or "winning lot" in l or "result lot" in l:
                current = {
                    "lot_number":          clean_text(value),
                    "framework_max_value": None,
                    "re_estimated_value":  None,
                    "decision_date":       None,
                    "award_date":          None,
                    "num_tenders":         None,
                    "contract_identifier": None,
                    "supplier_name":       None,
                    "other_tenderers":     [],
                }
                records.append(current)
                continue

            if current is None:
                continue

            if "maximum value" in l and ("framework" in l or "agreement" in l):
                current["framework_max_value"] = clean_text(value)
            elif "re-estimated" in l or "re estimated" in l:
                current["re_estimated_value"] = clean_text(value)
            elif label_no == "Offisielt navn" or l == "official name":
                if current["supplier_name"] is None:
                    current["supplier_name"] = clean_text(value)
                else:
                    current["other_tenderers"].append(clean_text(value))
            elif "contract identifier" in l or "payment method identifier" in l:
                if not current["contract_identifier"]:
                    current["contract_identifier"] = clean_text(value)
            elif "winner was chosen" in l or "winner was selected" in l:
                current["decision_date"] = parse_date(value, "decision_date")
            elif "conclusion of the contract" in l or "contract was concluded" in l:
                current["award_date"] = parse_date(value, "award_date")
            elif "number of tenders" in l or "requests to participate received" in l:
                current["num_tenders"] = clean_text(value)

        # Enrich winners with org contact details (block05), matched by
        # normalized name. Names themselves are used as-is — see docstring.
        org_lookup = self._build_org_lookup(eform)

        for r in records:
            if not r["supplier_name"]:
                continue
            org = org_lookup.get(self._norm_name(r["supplier_name"]), {})
            r["supplier_name_en"] = r["supplier_name"]
            for k, v in org.items():
                r[f"supplier_{k}"] = v

        return {"total_value": total_value, "lots": records}

    def _build_awarded_suppliers(self, award_results: list[dict]) -> list[dict]:
        """Flattens the per-lot award_results into the awarded_suppliers schema."""
        suppliers = []
        seen = set()

        for r in award_results:
            if not r.get("supplier_name"):
                continue
            key = (r.get("supplier_name_en") or r["supplier_name"], r.get("supplier_org_no"), r.get("lot_number"))
            if key in seen:
                continue
            seen.add(key)
            suppliers.append({
                "name":                r.get("supplier_name_en") or r.get("supplier_name"),
                "org_no":              r.get("supplier_org_no"),
                "country":             r.get("supplier_country"),
                "address":             r.get("supplier_address"),
                "postal_code":         r.get("supplier_postal_code"),
                "city":                r.get("supplier_city"),
                "email":               r.get("supplier_email"),
                "phone":               r.get("supplier_phone"),
                "website":             r.get("supplier_website"),
                "lot_number":          r.get("lot_number"),
                "contract_identifier": r.get("contract_identifier"),
                "decision_date":       r.get("decision_date"),
                "award_date":          r.get("award_date"),
                "num_tenders_received": r.get("num_tenders"),
                "framework_max_value": r.get("framework_max_value"),
                "re_estimated_value":  r.get("re_estimated_value"),
                "other_tenderers":     r.get("other_tenderers") or [],
            })

        return suppliers

    def _parse_lots(self, raw: dict) -> list[dict]:
        # NOTE: raw.get("core").get("lots") / raw.get("lots") do not exist in
        # Doffin's real API response — this is currently a no-op for this
        # notice type. Per-lot detail lives in eform block02 instead. Left
        # as-is / out of scope for the award_results fix; flag if you also
        # want this wired up to block02.
        lots_raw = (raw.get("core") or {}).get("lots") or raw.get("lots") or []
        lots = []
        for lot in lots_raw:
            if not isinstance(lot, dict):
                continue
            award = lot.get("award") or lot.get("awardedContract") or {}
            suppliers = []
            for s in (award.get("suppliers") or award.get("winners") or []):
                if isinstance(s, dict):
                    suppliers.append({
                        "name":   clean_text(s.get("name")),
                        "org_no": clean_text(s.get("organizationNumber") or s.get("id")),
                    })

            val_raw = (award.get("value") or award.get("contractValue") or
                       lot.get("estimatedValue") or {})
            lots.append({
                "lot_number":              clean_text(lot.get("lotNumber") or lot.get("id")),
                "lot_title":               clean_text(lot.get("title")),
                "lot_description":         clean_text(lot.get("description")),
                "award_date":              parse_date(award.get("date") or award.get("awardDate")),
                "num_tenders":             award.get("numberOfTenders") or award.get("numTenders"),
                "contract_value":          val_raw.get("amount") if isinstance(val_raw, dict) else val_raw,
                "contract_value_currency": val_raw.get("currency") if isinstance(val_raw, dict) else None,
                "suppliers":               suppliers,
                "framework_agreement":     lot.get("frameworkAgreement", False),
            })
        return lots

    def _parse_award_summary(self, raw: dict, award_results: dict) -> dict:
        """Aggregate award-level numbers, sourced from the real block06 results
        (award_results), falling back to the pre-award estimated value only
        when nothing concrete is available — and clearly distinguishing the two.

        Priority for contract_value:
          1. The procedure-level "Value of all contracts awarded in this
             procedure" figure (most accurate — an actual award total).
          2. Per-lot re-estimated / framework-max value (a ceiling estimate,
             not necessarily the real award sum, but the best per-lot figure
             Doffin gives).
          3. The pre-award estimatedValue (flagged via contract_value_is_estimate).
        """
        lots             = award_results.get("lots") or []
        total_value_raw  = award_results.get("total_value")

        award_date              = None
        num_tenders_received    = None
        contract_value          = None
        contract_value_currency = None
        contract_value_is_estimate = False

        if lots:
            dates = [r["award_date"] for r in lots if r.get("award_date")]
            if dates:
                award_date = min(dates)

            tender_counts = []
            for r in lots:
                n = clean_number(r.get("num_tenders"))
                if n is not None:
                    tender_counts.append(int(n))
            if tender_counts:
                num_tenders_received = sum(tender_counts)

        if total_value_raw:
            val = clean_number(total_value_raw)
            if val is not None:
                contract_value = val
                contract_value_currency = "NOK"

        if contract_value is None and lots:
            for r in lots:
                raw_value = r.get("re_estimated_value") or r.get("framework_max_value")
                if raw_value:
                    val = clean_number(raw_value)
                    if val is not None:
                        contract_value = val
                        contract_value_currency = "NOK"
                        break

        core = raw.get("core") or {}
        if contract_value is None:
            est = core.get("estimatedValue") or raw.get("estimatedValue")
            if isinstance(est, dict) and est.get("amount") is not None:
                contract_value = est.get("amount")
                contract_value_currency = est.get("code")
                contract_value_is_estimate = True

        linked = raw.get("procedureId") or core.get("procedureId")

        return {
            "award_date":                  award_date,
            "num_tenders_received":        num_tenders_received,
            "contract_value":              contract_value,
            "contract_value_currency":     contract_value_currency,
            "contract_value_is_estimate":  contract_value_is_estimate,
            "procedure_total_awarded_value": clean_number(total_value_raw),
            "lowest_offer":                None,
            "highest_offer":               None,
            "linked_competition_id":       clean_text(linked),
        }

    def _parse_organizations(self, eform: list | None) -> str | None:
        if not eform:
            return None

        def _extract_fields(sections) -> dict:
            fields = {}
            if not sections:
                return fields
            for sec in sections:
                label = sec.get("label")
                value = sec.get("value")
                if label and value:
                    fields[label] = value
                nested = sec.get("sections")
                if nested:
                    fields.update(_extract_fields(nested))
            return fields

        def _format_org(fields: dict) -> str | None:
            parts = []
            if fields.get("Offisielt navn"):      parts.append(fields["Offisielt navn"])
            if fields.get("Organisasjonsnummer"): parts.append(f"Org.no: {fields['Organisasjonsnummer']}")
            if fields.get("Postadresse"):         parts.append(fields["Postadresse"].strip())
            if fields.get("By"):                  parts.append(fields["By"])
            if fields.get("Sted"):                parts.append(fields["Sted"])
            if fields.get("Postnummer"):          parts.append(fields["Postnummer"])
            if fields.get("Land"):                parts.append(tr(fields["Land"]))
            if fields.get("Kontaktpunkt"):        parts.append(f"Contact: {fields['Kontaktpunkt']}")
            if fields.get("E-post"):              parts.append(f"Email: {fields['E-post']}")
            if fields.get("E-postadresse"):       parts.append(f"Email: {fields['E-postadresse']}")
            if fields.get("Telefon"):             parts.append(f"Tel: {fields['Telefon']}")
            if fields.get("Internett-adresse"):   parts.append(f"Web: {fields['Internett-adresse']}")
            if fields.get("Nettadresse"):         parts.append(f"Web: {fields['Nettadresse']}")
            return ", ".join(parts) if parts else None

        orgs = []

        for block in (eform or []):
            title = block.get("title", "")
            label = block.get("label", "")

            if title == "block05" or label == "Virksomheter":
                for org in (block.get("sections") or []):
                    fields = _extract_fields(org.get("sections") or [])
                    formatted = _format_org(fields)
                    if formatted:
                        orgs.append(formatted)

            elif title == "block01" or label == "Kjøper":
                if not orgs:
                    for sub in (block.get("sections") or []):
                        fields = _extract_fields(sub.get("sections") or [])
                        formatted = _format_org(fields)
                        if formatted:
                            orgs.append(formatted)
                            break

        if not orgs:
            return None

        orgs = translate_batch(orgs)
        return " | ".join(o for o in orgs if o)

    def _parse_notice_info(self, eform: list | None) -> str | None:
        if not eform:
            return None
        info_block = None
        for block in eform:
            if block.get("title") == "block07" or block.get("label") == "Kunngjøringsinformasjon":
                info_block = block
                break
        if not info_block:
            return None

        def _extract_flat(sections) -> list[tuple]:
            items = []
            if not sections:
                return items
            for sec in sections:
                label = sec.get("label")
                value = sec.get("value")
                if label and value:
                    items.append((label, value))
                nested = sec.get("sections")
                if nested:
                    items.extend(_extract_flat(nested))
            return items

        parts = []
        for label, value in _extract_flat(info_block.get("sections") or []):
            parts.append(f"{tr(label)}: {value}")

        if parts:
            parts = translate_batch(parts)
        return ", ".join(parts) if parts else None

    # ── Build MongoDB document ────────────────────────────────────

    def _build_doc(self, raw: dict, status_api: str) -> dict:
        notice_id = str(raw.get("id", ""))
        buyers    = raw.get("buyer") or []
        buyer     = buyers[0] if buyers else {}

        cpv_codes = [
            {"code": c, "description": cpv_desc(c)}
            for c in (raw.get("allCpvCodes") or raw.get("directCpvCodes") or [])
        ]

        place_raw = raw.get("placeOfPerformance") or []
        core      = raw.get("core") or {}
        comp_url  = raw.get("competitionDocsUrl")
        reg_url   = raw.get("regulationUrl")

        documents = []
        if comp_url:
            documents.append({
                "type":         "Tender_documents",
                "title":        "Competition Documents",
                "original_url": comp_url,
                "portal":       detect_portal(comp_url),
                "s3_path":      None,
                "uploaded_at":  None,
            })
        if reg_url:
            documents.append({
                "type":         "Tender_document",
                "title":        "Regulation Reference",
                "original_url": reg_url,
                "portal":       "lovdata.no",
                "s3_path":      None,
                "uploaded_at":  None,
            })

        eform_raw     = raw.get("eform")
        eform_flat    = self._flatten_eform(eform_raw)
        award_results = self._parse_award_results(eform_raw)
        organizations = self._parse_organizations(eform_raw)
        notice_info   = self._parse_notice_info(eform_raw)

        title_no          = clean_text(raw.get("heading"))
        description_no    = clean_text(raw.get("description"))
        buyer_name        = buyer.get("name")
        main_activity_raw = core.get("mainActivity")

        fields_no = [title_no, description_no, buyer_name, main_activity_raw, *place_raw]
        fields_en = translate_batch(fields_no)

        title_en         = fields_en[0]
        description_en   = fields_en[1]
        buyer_name_en    = fields_en[2]
        main_activity_en = fields_en[3]
        place_en         = fields_en[4:]

        notice_type_raw = raw.get("noticeType") or raw.get("type")
        notice_type_en  = tr_notice_type(notice_type_raw)

        award_summary      = self._parse_award_summary(raw, award_results)
        awarded_suppliers  = self._build_awarded_suppliers(award_results["lots"])
        lots               = self._parse_lots(raw)

        return {
            "hash_id":          generate_hash(notice_id),
            "teb_number":       self._teb_id(),
            "notice_id":        notice_id,
            "eform_id":         raw.get("eFormId"),
            "procedure_id":     raw.get("procedureId"),
            "source":           "Doffin (Norway Public Procurement)",
            "source_url":       f"https://www.doffin.no/notice/{notice_id}",
            "title":            title_en,
            "description":      description_en,
            "buyer_id":         buyer.get("id") or buyer.get("organizationId"),
            "buyer_name":       buyer_name_en,
            "issue_date":                     parse_date(raw.get("issueDate"), "issueDate"),
            "deadline":                       parse_date(raw.get("deadline"), "deadline"),
            "qualification_deadline":         parse_date(raw.get("qualificationDeadline")),
            "publication_date":               clean_text(raw.get("publicationDate")),
            "preferred_publication_date_ted": clean_text(raw.get("preferredPublicationDateTed")),
            "planned_date_ted":               clean_text(raw.get("plannedDateTed")),
            "notice_type":      notice_type_en,
            "notice_type_raw":  notice_type_raw,
            "all_types":        [tr_notice_type(t) for t in (raw.get("allTypes") or [])],
            "status":           STATUS_DISPLAY.get(status_api, status_api),
            "status_raw":       status_api,
            "cpv_codes":        cpv_codes,
            "direct_cpv_codes": raw.get("directCpvCodes") or [],
            "location_ids":         raw.get("locationId") or [],
            "place_of_performance": place_en,
            "estimated_value":  raw.get("estimatedValue") or core.get("estimatedValue"),
            "main_activity":    main_activity_en,
            "competition_docs_url":    comp_url,
            "competition_docs_portal": detect_portal(comp_url),
            "regulation_url":          reg_url,
            "sent_to_ted":             raw.get("sentToTed", False),
            "eform_fields":     eform_flat,
            "eform_results":    award_results,
            "organizations":    organizations,
            "notice_info":      notice_info,
            "documents":        documents,
            "award_date":               award_summary["award_date"],
            "num_tenders_received":     award_summary["num_tenders_received"],
            "contract_value":           award_summary["contract_value"],
            "contract_value_currency":  award_summary["contract_value_currency"],
            "contract_value_is_estimate": award_summary["contract_value_is_estimate"],
            "lowest_offer":             award_summary["lowest_offer"],
            "highest_offer":            award_summary["highest_offer"],
            "linked_competition_id":    award_summary["linked_competition_id"],
            "awarded_suppliers":        awarded_suppliers,
            "lots":                     lots,
            "etl_status":  "pending",
            "created_at":  datetime.now(timezone.utc),
            "updated_at":  datetime.now(timezone.utc),
        }

    # ── S3 upload ────────────────────────────────────────────────

    def _upload_docs_s3(self, doc: dict, mongo_id) -> None:
        if not self.s3:
            return
        folder  = f"{doc['notice_id']}_{mongo_id}"
        updated = []
        for att in doc.get("documents", []):
            url = att.get("original_url")
            if not url:
                updated.append(att)
                continue
            try:
                r  = self._get(url, timeout=60)
                ct = r.headers.get("content-type", "application/octet-stream").split(";")[0]
                fname = os.path.basename(urlparse(url).path) or "document"
                if not os.path.splitext(fname)[1]:
                    fname += {
                        "application/pdf": ".pdf",
                        "application/zip": ".zip",
                        "text/html":       ".html",
                    }.get(ct, ".bin")
                key = f"{self.s3_folder}/{folder}/{fname}"
                self.s3.put_object(Bucket=self.bucket, Key=key,
                                   Body=r.content, ContentType=ct)
                att["s3_path"]     = f"s3://{self.bucket}/{key}"
                att["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"    S3 ✓ {fname}")
            except Exception as e:
                self.logger.warning(f"    S3 failed {url}: {e}")
            updated.append(att)
            time.sleep(random.uniform(0.3, 0.8))
        self.col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})

    # ── Process one notice ───────────────────────────────────────

    def _process_notice(self, notice_id: str, status_api: str = "AWARDED") -> bool:
        if notice_id in self._scraped_ids:
            return False

        raw = self._fetch_detail(notice_id)
        if not raw:
            return False

        doc = self._build_doc(raw, status_api)

        try:
            result = self.col.insert_one(doc)
            self._scraped_ids.add(notice_id)
            suppliers_str = ", ".join(
                s["name"] for s in doc["awarded_suppliers"] if s.get("name")
            ) or "(no supplier)"
            self.logger.info(
                f"  ✓ {notice_id} | TEB={doc['teb_number']} | "
                f"Suppliers: {suppliers_str} | Value: {doc['contract_value']} {doc['contract_value_currency'] or ''}"
            )
            if doc.get("documents") and self.s3:
                self._upload_docs_s3(doc, result.inserted_id)
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": doc["hash_id"]},
                {"$set": {**doc, "updated_at": datetime.now(timezone.utc)}},
            )
            self._scraped_ids.add(notice_id)
            self.logger.info(f"  ↺ {notice_id} updated (duplicate)")

        self._sleep()
        return True

    # ── Drain one bucket ─────────────────────────────────────────

    def _drain_bucket(self, label: str,
                      notice_type: str,
                      cpv: str | None,
                      location: str | None) -> tuple[int, int, list[str]]:
        try:
            first = self._search(1, notice_type, cpv=cpv, location=location)
        except Exception as e:
            self.logger.error(f"  Bucket {label} page 1 failed: {e}")
            return 0, 0, []

        total      = first.get("numHitsTotal", 0)
        accessible = first.get("numHitsAccessible", total)
        accessible = min(total, accessible, HARD_CAP)

        if total == 0:
            return 0, 0, []

        loc_ids = self._get_facet_ids(first, "locations")
        self.logger.info(f"  {label}: {total} hits | {accessible} accessible")

        if total > HARD_CAP and not location:
            return total, accessible, loc_ids

        total_pages = max(1, -(-accessible // PAGE_SIZE))

        for page in range(1, total_pages + 1):
            if page == 1:
                data = first
            else:
                try:
                    data = self._search(page, notice_type, cpv=cpv, location=location)
                    time.sleep(random.uniform(0.5, 1.2))
                except Exception as e:
                    self.logger.error(f"  {label} page {page} failed: {e}")
                    continue

            hits = data.get("hits", [])
            if not hits:
                break

            for hit in hits:
                nid     = str(hit.get("id", ""))
                hstatus = hit.get("status") or "AWARDED"
                if not nid:
                    continue
                try:
                    self._process_notice(nid, hstatus)
                except Exception as e:
                    self.logger.error(f"  Notice [{nid}]: {e}")

        return total, accessible, []

    # ── Main scrape loop ─────────────────────────────────────────

    def scrape(self) -> None:
        """
        Scrapes ANNOUNCEMENT_OF_CONCLUSION_OF_CONTRACT notices from Doffin.
        Iterates all CPV buckets; subdivides by location if capped.
        """
        for notice_type in AWARD_NOTICE_TYPES:
            self.logger.info("═" * 60)
            self.logger.info(f"Probing notice type: {notice_type} …")

            try:
                probe   = self._search(1, notice_type, cpv=None, location=None)
                cpv_ids = self._get_facet_ids(probe, "cpvCode")
                total   = probe.get("numHitsTotal", 0)
                self.logger.info(f"  Total: {total} | CPV buckets: {len(cpv_ids)}")
            except Exception as e:
                self.logger.error(f"Probe failed for {notice_type}: {e}")
                continue

            cpv_ids.append(None)
            n_cpv = len(cpv_ids)

            for cpv_idx, cpv in enumerate(cpv_ids, 1):
                label = f"{notice_type}|CPV={cpv or 'none'}"
                self.logger.info(f"[{cpv_idx}/{n_cpv}] {label}")

                total, accessible, loc_ids = self._drain_bucket(
                    label, notice_type, cpv=cpv, location=None
                )

                if total > HARD_CAP and loc_ids:
                    self.logger.warning(
                        f"  ⚠ Capped ({total} > {HARD_CAP}). "
                        f"Sub-dividing into {len(loc_ids)} locations …"
                    )
                    for loc in loc_ids:
                        sub_label = f"{notice_type}|CPV={cpv or 'none'}+LOC={loc}"
                        sub_total, _, _ = self._drain_bucket(
                            sub_label, notice_type, cpv=cpv, location=loc
                        )
                        if sub_total > HARD_CAP:
                            self.logger.warning(
                                f"  ⚠⚠ STILL CAPPED {sub_label}: {sub_total} hits. Some records may be missed."
                            )
                        time.sleep(random.uniform(0.5, 1.0))

                self.logger.info(f"  DB total so far: {len(self._scraped_ids)} unique notices")
                time.sleep(random.uniform(0.8, 1.5))

        self.logger.info("═" * 60)
        self.logger.info("ALL DONE.")
        self.logger.info(f"Unique award notices in DB: {len(self._scraped_ids)}")


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    DoffinAwardScraper().scrape()