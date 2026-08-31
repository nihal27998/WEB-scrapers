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
logger = logging.getLogger("doffin")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

PORTAL_BASE = "https://www.doffin.no"
API_BASE    = "https://api.doffin.no/webclient/api/v2"
SEARCH_URL  = f"{API_BASE}/search-api/search"
DETAIL_URL  = f"{API_BASE}/notices-api/notices"

PAGE_SIZE = 100
HARD_CAP  = 1000   # API never returns more than this per query


STATUS_DISPLAY = {
    "ACTIVE":       "OPEN",
    "DISCONTINUED": "CLOSED",
    "AWARDED":      "Awarded",
    "CANCELLED":    "Cancelled",
    "EXPIRED":      "CLOSED"
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
    "ADVISORY_NOTICE":                       "Advisory Notice / Request for Information",
    "PLANNING":                              "Planning Notice",
    "PRIOR_INFORMATION_NOTICE":              "Prior Information Notice",
    "CONTRACT_NOTICE":                       "Contract Notice",
    "CONTRACT_AWARD_NOTICE":                 "Contract Award Notice",
    "COMPETITION":                           "Competition / Tender",
    "ANNOUNCEMENT_OF_COMPETITION":           "Announcement of Competition",
    "DYNAMIC_PURCHASING_SCHEME":             "Dynamic Purchasing Scheme",
    "RESULT":                                "Result / Award",
    "DESIGN_CONTEST":                        "Design Contest",
    "RESULTS_OF_DESIGN_CONTEST":             "Results of Design Contest",
    "MODIFICATION_NOTICE":                   "Modification Notice",
    "VOLUNTARY_EX_ANTE_TRANSPARENCY_NOTICE": "Voluntary Ex-Ante Transparency Notice",
    "CONCESSION_AWARD":                      "Concession Award",
    "QUALIFICATION_SYSTEM":                  "Qualification System",
    "SIMPLIFIED_CONTRACT_NOTICE":            "Simplified Contract Notice",
    "CALL_FOR_COMPETITION_SIMPLIFIED":       "Simplified Call for Competition",
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

def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()

def parse_date(raw, ctx="") -> datetime | None:
    if not raw:
        return None
    try:
        dt = dateutil_parser.parse(str(raw).strip())
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
    try:
        separator  = " ||| "
        joined     = separator.join(to_translate)
        translated = GoogleTranslator(source="auto", target="en").translate(joined)
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

class DoffinScraper:

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
        self.logger = logging.getLogger("doffin")

        # ── HTTP session ─────────────────────────────────────────
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

        # ── MongoDB ──────────────────────────────────────────────
        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["doffin_tenders"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",  unique=True)
        self.col.create_index("notice_id")
        self.col.create_index([("status", 1), ("issue_date", -1)])
        self.logger.info("MongoDB connected")

        # ── S3 ───────────────────────────────────────────────────
        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/doffin"
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

        # ── Progress tracking (resume support) ───────────────────
        self._scraped_ids: set[str] = set()
        self._load_scraped_ids()

        self._debug_detail_saved = False

    def _load_scraped_ids(self):
        """Load all already-scraped notice_ids from MongoDB for resume support."""
        try:
            ids = self.col.distinct("notice_id")
            self._scraped_ids = set(ids)
            self.logger.info(f"Resume: {len(self._scraped_ids)} notices already in DB")
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
            {"_id": "tb_global_id_doffin"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        m   = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{m[now.month]}/{seq:08d}"

    # ── Search API ───────────────────────────────────────────────

    def _build_body(self, page: int,
                    status: str | None = None,
                    cpv: str | None = None,
                    location: str | None = None) -> dict:
        """Build POST body. type is always locked to ANNOUNCEMENT_OF_COMPETITION."""
        return {
            "numHitsPerPage": PAGE_SIZE,
            "page":           page,
            "searchString":   "",
            "sortBy":         "RELEVANCE",
            "facets": {
                "cpvCodesLabel": {"checkedItems": []},
                "cpvCodesId":    {"checkedItems": [cpv] if cpv else []},
                "type":          {"checkedItems": [ "ANNOUNCEMENT_OF_COMPETITION"]},
                "status":        {"checkedItems": [status] if status else []},
                "location":      {"checkedItems": [location] if location else []},
            },
        }

    def _search(self, page: int,
                status: str | None = None,
                cpv: str | None = None,
                location: str | None = None) -> dict:
        body = self._build_body(page, status=status, cpv=cpv, location=location)
        resp = self._post(SEARCH_URL, json=body,
                          headers={"Content-Type": "application/json"})
        return resp.json()

    def _get_facet_ids(self, data: dict, facet_name: str) -> list[str]:
        """Extract bucket IDs from a search response's facets."""
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
                with open("debug_doffin_detail.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self.logger.info("DEBUG: saved debug_doffin_detail.json")
                self._debug_detail_saved = True
            return data
        except Exception as e:
            self.logger.error(f"Detail fetch failed [{notice_id}]: {e}")
            return None

    # ── eForm flattener ──────────────────────────────────────────

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

    # ── Section parsers ──────────────────────────────────────────

    def _parse_organizations(self, eform: list | None) -> str | None:
        if not eform:
            return None
        org_block = None
        for block in eform:
            if block.get("title") == "block05" or block.get("label") == "Virksomheter":
                org_block = block
                break
        if not org_block:
            return None
        orgs = []
        for org in (org_block.get("sections") or []):
            fields = {}
            for sec in (org.get("sections") or []):
                label = sec.get("label")
                value = sec.get("value")
                if label and value:
                    fields[label] = value
            parts = []
            if fields.get("Offisielt navn"):      parts.append(fields["Offisielt navn"])
            if fields.get("Organisasjonsnummer"): parts.append(f"Org.no: {fields['Organisasjonsnummer']}")
            if fields.get("Postadresse"):         parts.append(fields["Postadresse"].strip())
            if fields.get("By"):                  parts.append(fields["By"])
            if fields.get("Postnummer"):          parts.append(fields["Postnummer"])
            if fields.get("Land"):                parts.append(tr(fields["Land"]))
            if fields.get("Kontaktpunkt"):        parts.append(f"Contact: {fields['Kontaktpunkt']}")
            if fields.get("E-post"):              parts.append(f"Email: {fields['E-post']}")
            if fields.get("Telefon"):             parts.append(f"Tel: {fields['Telefon']}")
            if fields.get("Internett-adresse"):   parts.append(f"Web: {fields['Internett-adresse']}")
            if parts:
                orgs.append(", ".join(parts))
        if orgs:
            orgs = translate_batch(orgs)
        return " | ".join(orgs) if orgs else None

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
        parts = []
        for sec in (info_block.get("sections") or []):
            label = sec.get("label")
            value = sec.get("value")
            if label and value:
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

        est_val = raw.get("estimatedValue") or None
        if not est_val and core.get("estimatedValue"):
            est_val = core["estimatedValue"]

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
            "estimated_value":  est_val,
            "main_activity":    main_activity_en,
            "competition_docs_url":    comp_url,
            "competition_docs_portal": detect_portal(comp_url),
            "regulation_url":          reg_url,
            "sent_to_ted":             raw.get("sentToTed", False),
            "eform_fields":     eform_flat,
            "organizations":    organizations,
            "notice_info":      notice_info,
            "documents":        documents,
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

    def _process_notice(self, notice_id: str, status_api: str = "ACTIVE") -> bool:
        """Fetch + save one notice. Returns True if inserted/updated, False if skipped."""
        if notice_id in self._scraped_ids:
            return False  # already have it

        raw = self._fetch_detail(notice_id)
        if not raw:
            return False

        doc = self._build_doc(raw, status_api)

        try:
            result = self.col.insert_one(doc)
            self._scraped_ids.add(notice_id)
            self.logger.info(f"  ✓ {notice_id} | TEB={doc['teb_number']} | {doc['title'] or '(no title)'}")
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

    # ── Drain one search bucket (all pages) ──────────────────────

    def _drain_bucket(self, label: str,
                      status: str | None,
                      cpv: str | None,
                      location: str | None) -> tuple[int, int, list[str]]:
        """
        Fetch all pages of a (status, cpv, location) bucket.
        Returns (total_hits, accessible_hits, remaining_location_ids_if_capped).
        If after all pages we still seem capped (total > accessible),
        returns the location bucket IDs from the response so the caller
        can sub-divide by location.
        """
        try:
            first = self._search(1, status=status, cpv=cpv, location=location)
        except Exception as e:
            self.logger.error(f"  Bucket {label} page 1 failed: {e}")
            return 0, 0, []

        total      = first.get("numHitsTotal", 0)
        accessible = first.get("numHitsAccessible", total)
        accessible = min(total, accessible, HARD_CAP)

        if total == 0:
            return 0, 0, []

        # Collect location sub-buckets in case we need them
        loc_ids = self._get_facet_ids(first, "locations")

        self.logger.info(f"  {label}: {total} hits | {accessible} accessible")

        if total > HARD_CAP and not location:
            # Signal to caller: needs location sub-division
            return total, accessible, loc_ids

        total_pages = max(1, -(-accessible // PAGE_SIZE))
        inserted    = 0

        for page in range(1, total_pages + 1):
            if page == 1:
                data = first
            else:
                try:
                    data = self._search(page, status=status, cpv=cpv, location=location)
                    time.sleep(random.uniform(0.5, 1.2))
                except Exception as e:
                    self.logger.error(f"  {label} page {page} failed: {e}")
                    continue

            hits = data.get("hits", [])
            if not hits:
                break

            for hit in hits:
                nid    = str(hit.get("id", ""))
                hstatus = hit.get("status") or status or "ACTIVE"
                if not nid:
                    continue
                try:
                    if self._process_notice(nid, hstatus):
                        inserted += 1
                except Exception as e:
                    self.logger.error(f"  Notice [{nid}]: {e}")

        return total, accessible, []

    # ── Main scrape loop ─────────────────────────────────────────

    def scrape(self) -> None:
        """
        Strategy: ANNOUNCEMENT_OF_COMPETITION only, no status filter.
        Iterate all CPV buckets. If any CPV bucket still > 1000,
        sub-divide by location. Matches exactly what the website shows.
        """
        # ── Step 1: probe to discover all CPV bucket IDs ──────────
        self.logger.info("═" * 60)
        self.logger.info("Probing ANNOUNCEMENT_OF_COMPETITION — discovering CPV buckets …")

        try:
            probe   = self._search(1, status=None, cpv=None, location=None)
            cpv_ids = self._get_facet_ids(probe, "cpvCode")
            total   = probe.get("numHitsTotal", 0)
            self.logger.info(f"  Total records: {total} | CPV buckets: {len(cpv_ids)}")
        except Exception as e:
            self.logger.error(f"Probe failed: {e}")
            return

        # Add a None sentinel to catch tenders with no CPV code
        cpv_ids.append(None)
        n_cpv = len(cpv_ids)

        # ── Step 2: drain each CPV bucket ─────────────────────────
        self.logger.info("═" * 60)
        self.logger.info(f"Processing {n_cpv} CPV buckets …")

        for cpv_idx, cpv in enumerate(cpv_ids, 1):
            label = f"CPV={cpv or 'none'}"
            self.logger.info(f"[{cpv_idx}/{n_cpv}] {label}")

            total, accessible, loc_ids = self._drain_bucket(
                label, status=None, cpv=cpv, location=None
            )

            # If capped, sub-divide by location
            if total > HARD_CAP and loc_ids:
                self.logger.warning(
                    f"  ⚠ Capped ({total} > {HARD_CAP}). "
                    f"Sub-dividing into {len(loc_ids)} locations …"
                )
                for loc in loc_ids:
                    sub_label = f"CPV={cpv or 'none'}+LOC={loc}"
                    sub_total, sub_acc, _ = self._drain_bucket(
                        sub_label, status=None, cpv=cpv, location=loc
                    )
                    if sub_total > HARD_CAP:
                        self.logger.warning(
                            f"  ⚠⚠ STILL CAPPED at {sub_label}: "
                            f"{sub_total} hits. Some records may be missed."
                        )
                    time.sleep(random.uniform(0.5, 1.0))

            self.logger.info(f"  DB total so far: {len(self._scraped_ids)} unique notices")
            time.sleep(random.uniform(0.8, 1.5))

        # ── Done ──────────────────────────────────────────────────
        self.logger.info("═" * 60)
        self.logger.info(f"ALL DONE.")
        self.logger.info(f"Unique notices in DB: {len(self._scraped_ids)}")


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    DoffinScraper().scrape()