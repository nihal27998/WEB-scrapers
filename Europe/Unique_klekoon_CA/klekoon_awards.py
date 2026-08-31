
import argparse
import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

try:
    from deep_translator import GoogleTranslator
    HAVE_TRANSLATOR = True
except ImportError:
    HAVE_TRANSLATOR = False

load_dotenv()

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("klekoon_awards")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL    = "https://www.klekoon.com"
LISTING_URL = (
    f"{BASE_URL}/rechercher-une-annonce-ou-un-dce-dematerialise-sur-klekoon"
)
DETAIL_URL_TPL = f"{BASE_URL}/details-donnees-essentielles?DE_ID={{de_id}}&page=1"

DELAY_MIN = 1.2
DELAY_MAX = 2.8

# Static FR → EN label map for known categorical values
LABEL_MAP = {
    "Accord-cadre":                    "Framework Agreement",
    "Appel d'offres ouvert":           "Open Tender",
    "Appel d'offres restreint":        "Restricted Tender",
    "Procédure adaptée":               "Adapted Procedure (MAPA)",
    "Procédure adaptée (MAPA)":        "Adapted Procedure (MAPA)",
    "Procédure adapt\u00e9e":          "Adapted Procedure (MAPA)",
    "Proc\u00e9dure adapt\u00e9e":     "Adapted Procedure (MAPA)",
    "Procédure négociée":              "Negotiated Procedure",
    "Dialogue compétitif":             "Competitive Dialogue",
    "Marché de partenariat":           "Partnership Contract",
    "Concession":                      "Concession",
    "Système d'acquisition dynamique": "Dynamic Purchasing System",
    "Marché":                          "Contract",
    "march\u00e9":                     "Contract",
    "Travaux":                         "Works",
    "Fournitures":                     "Supplies",
    "Services":                        "Services",
    "Révisable":                       "Revisable",
    "Ferme":                           "Fixed",
    "Sans objet":                      "N/A",
    "Oui": "Yes", "Non": "No",
    "oui": "Yes", "non": "No",
}

# ══════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════

def clean_text(v) -> str | None:
    if v is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(v)).strip()
    # Remove stray HTML entities that slipped through
    cleaned = cleaned.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    return cleaned or None


def generate_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()


def parse_date(raw: str | None, ctx: str = "") -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    raw = re.sub(r"\s*[–\-]\s*\d+\s+jours?\s+restants?.*", "", raw, flags=re.I).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        logger.debug(f"Date parse failed '{raw}' [{ctx}]")
        return None


def tr(text: str | None) -> str | None:
    """Return static English translation from LABEL_MAP, or the original."""
    if text is None:
        return None
    s = str(text).strip()
    return LABEL_MAP.get(s, s)


def parse_amount(raw: str | None) -> float | None:
    """Parse French-formatted amounts like '1 250 000,00 €' → 1250000.0"""
    if not raw:
        return None
    # Remove currency symbols, spaces (thousands sep), nbsp
    raw = re.sub(r"[€$£\s\xa0]", "", raw)
    # French decimal is comma; thousands sometimes dot
    # "1.250.000,00" → "1250000.00"  OR  "1250000,00" → "1250000.00"
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def translate_batch(texts: list) -> list:
    """Translate a list of FR strings to EN. Returns original list on failure."""
    if not texts or not HAVE_TRANSLATOR:
        return texts
    indices = [i for i, t in enumerate(texts) if t and str(t).strip()]
    to_translate = [str(texts[i]) for i in indices]
    if not to_translate:
        return texts
    try:
        sep = " ||| "
        joined = sep.join(to_translate)
        translated = GoogleTranslator(source="fr", target="en").translate(joined)
        parts = [p.strip() for p in translated.split("|||")]
        result = list(texts)
        for idx, part in zip(indices, parts):
            result[idx] = part
        return result
    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")
        return texts


# ══════════════════════════════════════════════════════════════════
# LISTING PAGE PARSER
# ══════════════════════════════════════════════════════════════════

_DE_ID_IN_HREF = re.compile(r"DE_ID=(\d+)", re.I)


def parse_listing_page(html: str) -> tuple[list[dict], int]:
    """
    Extract DE_ID cards from the search results page.
    Anchors on <a href="*details-donnees-essentielles*DE_ID=N"> links.
    """
    soup  = BeautifulSoup(html, "lxml")
    items = []

    # Total count
    total = 0
    for tag in soup.find_all(string=re.compile(r"\d[\d\s]*\s+annonces?", re.I)):
        m = re.search(r"([\d\s]+)\s+annonces?", str(tag), re.I)
        if m:
            total = int(re.sub(r"\s", "", m.group(1)))
            break

    seen_ids: set = set()

    for a_tag in soup.find_all("a", href=_DE_ID_IN_HREF):
        href = a_tag.get("href", "")
        if "details-donnees-essentielles" not in href:
            continue
        m = _DE_ID_IN_HREF.search(href)
        if not m:
            continue
        de_id = m.group(1)
        if de_id in seen_ids:
            continue
        seen_ids.add(de_id)

        title_raw = clean_text(a_tag.get_text())

        # Find meta row (Emetteur / Parution) in surrounding siblings
        authority_raw = None
        dept_raw      = None
        pub_date_raw  = None

        card_div = a_tag.find_parent("div", id=de_id)
        if card_div:
            for sib in card_div.find_next_siblings("div"):
                sib_text = sib.get_text(" ", strip=True)
                if "Emetteur" in sib_text or "Parution" in sib_text:
                    m_em = re.search(
                        r"Emetteur\s*[:\-]\s*(.+?)(?:\s*\((\w{1,3})\))?(?:\s{2,}|$)",
                        sib_text, re.I,
                    )
                    if m_em:
                        authority_raw = clean_text(m_em.group(1))
                        dept_raw = clean_text(m_em.group(2)) if m_em.group(2) else None
                    m_pub = re.search(
                        r"Parution\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})", sib_text, re.I
                    )
                    if m_pub:
                        pub_date_raw = m_pub.group(1)
                    break

        items.append({
            "de_id":         de_id,
            "title_raw":     title_raw,
            "detail_url":    DETAIL_URL_TPL.format(de_id=de_id),
            "pub_date_raw":  pub_date_raw,
            "authority_raw": authority_raw,
            "dept_raw":      dept_raw,
        })

    logger.info(f"  Parsed {len(items)} award cards | total reported = {total}")
    return items, total


# ══════════════════════════════════════════════════════════════════
# DETAIL PAGE PARSER  — TABLE-BASED (real Klekoon structure)
# ══════════════════════════════════════════════════════════════════

# thead text → normalised section key
_SECTION_MAP = {
    r"num.ros\s+d.identif":              "ids",
    r"emetteur":                         "emetteur",
    r"caract.ristiques\s+du\s+march":    "marche",
    r"caract.ristiques\s+financi":       "financier",
    r"caract.ristiques\s+d.identif.*op": "operateurs",
    r"lots":                             "lots",
    r"fichiers\s+d.export":              "exports",
}

# label text → field name (for "marche" section rows)
_MARCHE_LABEL_MAP = {
    r"nature":                                    "contract_nature_fr",
    r"objet\s+du\s+march":                        "title_fr",
    r"code\s+cpv":                                "cpv",      # special: multi-value
    r"proc.dure\s+de\s+passation":                "procedure_type_fr",
    r"lieu\s+principal\s+d.ex.cution":            "lieu_raw",
    r"nom\s+du\s+lieu":                           "place_of_performance_fr",
    r"code\s+d.partement":                        "dept_code",
    r"dur.e\s+initiale\s+du\s+march":             "duration_months",
    r"date\s+de\s+notification":                  "notification_date_raw",
    r"date\s+initiale\s+de\s+publication":        "pub_date_raw",
    r"date\s+d.attribution":                      "award_date_raw",
}

_FINANCIER_LABEL_MAP = {
    r"montant.*ht":      "amount_ht_raw",
    r"montant.*ttc":     "amount_ttc_raw",
    r"forme\s+du\s+prix": "price_form_fr",
    r"valeur\s+totale":  "amount_ht_raw",
}


class AwardDetailParser:

    def parse(self, soup: BeautifulSoup, detail_url: str, de_id: str) -> dict:
        """
        Parse the Klekoon detail page.

        The page is structured as a sequence of <table class="table table-bordered">
        elements. Each table has:
          - <thead>: one <th colspan="2"> with the section name
          - <tbody>: <tr><td>label</td><td>value</td></tr> rows

        We iterate all tables, identify the section from thead text,
        then dispatch to a per-section handler.
        """
        result: dict = {
            "detail_url":    detail_url,
            "de_id":         de_id,
            "cpv_items":     [],
            "awardees":      [],
            "lots":          [],
        }

        tables = soup.find_all("table", class_="table")
        if not tables:
            logger.warning(f"[{de_id}] No <table class='table'> found on detail page")
            return result

        for table in tables:
            thead = table.find("thead")
            if not thead:
                continue
            section_raw = clean_text(thead.get_text()) or ""
            section_key = self._classify_section(section_raw)

            tbody = table.find("tbody")
            if not tbody:
                continue

            rows = tbody.find_all("tr")

            if section_key == "ids":
                self._parse_ids(rows, result)
            elif section_key == "emetteur":
                self._parse_emetteur(rows, result)
            elif section_key == "marche":
                self._parse_marche(rows, result)
            elif section_key == "financier":
                self._parse_financier(rows, result)
            elif section_key == "operateurs":
                self._parse_operateurs(rows, result)
            elif section_key == "lots":
                self._parse_lots(rows, result)
            else:
                logger.debug(f"[{de_id}] Unknown section: '{section_raw}'")

        return result

    # ── section classifier ────────────────────────────────────────

    def _classify_section(self, text: str) -> str:
        t = text.lower()
        for pattern, key in _SECTION_MAP.items():
            if re.search(pattern, t):
                return key
        return "unknown"

    # ── row helper ────────────────────────────────────────────────

    def _rows_to_pairs(self, rows) -> list[tuple[str, str]]:
        """Convert <tr><td>label</td><td>value</td></tr> to (label, value) pairs."""
        pairs = []
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) >= 2:
                label = clean_text(tds[0].get_text(" ", strip=True)) or ""
                value = clean_text(tds[1].get_text(" ", strip=True)) or ""
                pairs.append((label, value))
            elif len(tds) == 1:
                # Single-cell row — sometimes used for section sub-headers
                pairs.append((clean_text(tds[0].get_text(" ", strip=True)) or "", ""))
        return pairs

    # ── section parsers ───────────────────────────────────────────

    def _parse_ids(self, rows, result: dict) -> None:
        """
        Section: "Numéros d'identifications"
        Row 0: td="Numéro d'identification unique… : DE_ID"  td="UID : XXXXXXX"
        """
        for label, value in self._rows_to_pairs(rows):
            if re.search(r"num.ro\s+d.identif", label, re.I):
                # "Numéro d'identification unique de marché public : 2026000022052700"
                m = re.search(r":\s*(\d+)\s*$", label)
                if m:
                    result.setdefault("de_id", m.group(1))
                # UID is in the value
                m2 = re.search(r"UID\s*:\s*(\S+)", value, re.I)
                if m2:
                    result["uid"] = m2.group(1)

    def _parse_emetteur(self, rows, result: dict) -> None:
        """
        Section: "Emetteur"
        Rows: Nom / SIRET
        """
        for label, value in self._rows_to_pairs(rows):
            lc = label.lower()
            if re.search(r"\bnom\b", lc):
                result["authority_name"] = value
            elif re.search(r"siret", lc):
                result["authority_siret"] = value

    def _parse_marche(self, rows, result: dict) -> None:
        """
        Section: "Caractéristiques du marché public"
        Multi-row: Nature, Objet, CPV (can repeat), Procédure, Lieu,
                   Département, Durée, Date notification, Date publication
        """
        for label, value in self._rows_to_pairs(rows):
            matched = False
            for pattern, field in _MARCHE_LABEL_MAP.items():
                if re.search(pattern, label, re.I):
                    if field == "cpv":
                        # CPV rows repeat; collect all
                        # value like "45317300 - Travaux d'installation…"
                        # or via <ul><li><b>79212500</b> - ...</li></ul>
                        for cpv_val in self._extract_cpv_values(value):
                            result["cpv_items"].append(cpv_val)
                    elif field == "lieu_raw":
                        # "Code département : 79"
                        m = re.search(r":\s*(\d+)\s*$", value)
                        if m:
                            result["dept_code"] = m.group(1)
                    else:
                        result[field] = value
                    matched = True
                    break
            if not matched:
                logger.debug(f"  marche: unmatched label '{label}'")

    def _parse_financier(self, rows, result: dict) -> None:
        """
        Section: "Caractéristiques financières"
        Rows: Montant HT, Forme du prix
        Note: Klekoon sometimes uses "Montant forfaitaire ou estimé maximum HT Format"
              as the label (the word "Format" is part of the label, not the value).
        """
        for label, value in self._rows_to_pairs(rows):
            for pattern, field in _FINANCIER_LABEL_MAP.items():
                if re.search(pattern, label, re.I):
                    result[field] = value
                    break

    def _parse_operateurs(self, rows, result: dict) -> None:
        """
        Section: "Caractéristiques d'identification des opérateurs économiques"

        Structure:
          Row 0: header row  td="Identifiant"  td="Dénomination sociale"
          Row N: td="SIRET : 34772743000048"   td="INEO RESEAUX CENTRE ATLANTIQUE (79000 NIORT)"

        Each subsequent row is one awardee.
        """
        pairs = self._rows_to_pairs(rows)
        for label, value in pairs:
            # Skip the header row
            if re.search(r"identifiant|d.nomination", label, re.I) and \
               re.search(r"identifiant|d.nomination", value, re.I):
                continue

            awardee: dict = {}

            # Label contains identifier: "SIRET : 34772743000048"
            m_siret = re.search(r"SIRET\s*:\s*([\d\s]+)", label, re.I)
            m_tva   = re.search(r"TVA\s*:\s*(\S+)", label, re.I)
            m_siren = re.search(r"SIREN\s*:\s*(\d+)", label, re.I)

            if m_siret:
                awardee["siret"] = re.sub(r"\s", "", m_siret.group(1))
            elif m_tva:
                awardee["tva"] = m_tva.group(1)
            elif m_siren:
                awardee["siren"] = m_siren.group(1)
            elif label:
                awardee["identifier_raw"] = label

            # Value contains company name and sometimes city/postcode
            # "INEO RESEAUX CENTRE ATLANTIQUE (79000 NIORT)"
            if value:
                m_city = re.search(r"\((\d{5})\s+(.+?)\)\s*$", value)
                if m_city:
                    postcode = m_city.group(1)
                    city     = m_city.group(2)
                    name     = value[:m_city.start()].strip()
                    awardee["name"]     = clean_text(name)
                    awardee["city"]     = clean_text(city)
                    awardee["postcode"] = postcode
                else:
                    awardee["name"] = clean_text(value)

            if awardee.get("name") or awardee.get("siret"):
                result["awardees"].append(awardee)

    def _parse_lots(self, rows, result: dict) -> None:
        """Section: "Lots" — if present."""
        for label, value in self._rows_to_pairs(rows):
            if label or value:
                result["lots"].append({
                    "lot_number":   label,
                    "description_fr": value or label,
                })

    # ── CPV value extractor ───────────────────────────────────────

    def _extract_cpv_values(self, value: str) -> list[dict]:
        """
        Extract one or more CPV entries from a table cell value string.
        Handles both:
          "45317300 - Travaux d'installation électrique..."
          "79212500 - Services de vérification comptable"  (from <li> text)
        """
        items = []
        for m in re.finditer(r"(\d{8})(?:-(\d))?\s*[-–]\s*([^\n\r\d][^\n\r]{0,120})", value):
            code = m.group(1) + ("-" + m.group(2) if m.group(2) else "")
            name = clean_text(m.group(3))
            items.append({"cpv_code": code, "name_fr": name})
        # Also handle bare codes without descriptions
        if not items:
            for m in re.finditer(r"(\d{8})", value):
                items.append({"cpv_code": m.group(1), "name_fr": None})
        return items


# ══════════════════════════════════════════════════════════════════
# PLAYWRIGHT LISTING FETCHER
# ══════════════════════════════════════════════════════════════════

class PlaywrightListingFetcher:

    def __init__(self, keyword: str = "", headless: bool = True):
        self.keyword  = keyword
        self.headless = headless
        self._cookies_dict: dict = {}

    def _save_debug(self, html: str, page_num: int) -> None:
        fname = f"debug_awards_listing_p{page_num}.html"
        with open(fname, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
        logger.info(f"Saved debug: {fname}")

    def fetch_pages(self, max_pages: int | None = None) -> list[str]:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        results: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
                ),
                locale="fr-FR",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()

            logger.info("Playwright: opening listing page …")
            page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)

            # Select "Données essentielles" radio (id="rech-donnees", value="2")
            try:
                radio = page.locator("#rech-donnees")
                if not radio.is_checked():
                    radio.click()
                    page.wait_for_timeout(1200)
                    logger.info("  Clicked #rech-donnees")
                else:
                    logger.info("  #rech-donnees already checked")
            except Exception as e:
                logger.warning(f"  Could not click #rech-donnees: {e}")
                try:
                    page.locator("input[name='type_recherche'][value='2']").click()
                    page.wait_for_timeout(800)
                except Exception:
                    pass

            # Fill keyword
            if self.keyword:
                for sel in ["input#motcle", "input[name='motcle']"]:
                    try:
                        page.fill(sel, self.keyword, timeout=4000)
                        logger.info(f"  Keyword: {self.keyword}")
                        break
                    except Exception:
                        continue

            # Submit form — click button first, JS submit as fallback
            submitted = False
            for btn_sel in ["button[type='submit'].btn-orange", "button[type='submit']", "input[type='submit']"]:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.count() > 0:
                        btn.click(timeout=8000)
                        try:
                            page.wait_for_selector(
                                "a[href*='details-donnees-essentielles']", timeout=25_000
                            )
                        except PWTimeout:
                            page.wait_for_load_state("networkidle", timeout=25_000)
                        page.wait_for_timeout(1500)
                        submitted = True
                        logger.info(f"  Submitted via: {btn_sel}")
                        break
                except Exception:
                    continue

            if not submitted:
                try:
                    page.evaluate("document.getElementById('main-form').submit()")
                    page.wait_for_load_state("networkidle", timeout=25_000)
                    page.wait_for_timeout(1500)
                    logger.info("  Submitted via JS")
                except Exception as e:
                    logger.error(f"  All submission methods failed: {e}")

            # Capture cookies
            self._cookies_dict = {c["name"]: c["value"] for c in context.cookies()}
            logger.info(f"  Cookies: {list(self._cookies_dict.keys())}")

          # ── Detect total pages from hidden input in the form ──
            # The form has: <input id="page-number" name="page" value="414">
            # This value is the LAST page (current page after submit).
            # We need to start from page 1 and go forward.

            def get_total_pages_from_form(pg) -> int:
                """Read the hidden page input — it contains the total page count
                when the form was last submitted at the last page, OR we read
                the max get_page() value from pagination links."""
                try:
                    # Method 1: read the last get_page(N,20) number in pagination
                    links = pg.locator("ul.pagination li a[onclick*='get_page']").all()
                    max_p = 1
                    for lnk in links:
                        oc = lnk.get_attribute("onclick") or ""
                        m  = re.search(r"get_page\((\d+)", oc)
                        if m:
                            max_p = max(max_p, int(m.group(1)))
                    if max_p > 1:
                        return max_p
                except Exception:
                    pass
                return 1

            def submit_page(pg, page_num: int) -> bool:
                """Set the hidden page input and submit the form via JS."""
                try:
                    pg.evaluate(f"""
                        (function() {{
                            var inp = document.getElementById('page-number');
                            if (inp) inp.value = {page_num};
                            var perp = document.getElementById('per-page');
                            if (perp) perp.value = 20;
                            document.getElementById('main-form').submit();
                        }})()
                    """)
                    return True
                except Exception as e:
                    logger.warning(f"  submit_page({page_num}) JS failed: {e}")
                    return False

            # Detect total pages from the already-loaded results page
            total_pages = get_total_pages_from_form(page)
            logger.info(f"  Total pages detected: {total_pages}")
            # max_pages=None means fetch all; max_pages=N means fetch up to N pages
            fetch_up_to = total_pages if max_pages is None else min(total_pages, max_pages)
            logger.info(f"  Will fetch {fetch_up_to} page(s)")

            # Now iterate from page 1 to fetch_up_to
            current_page_num = 1
            while True:
                if current_page_num > fetch_up_to:
                    logger.info(f"  All {fetch_up_to} page(s) captured.")
                    break

                # Submit form for this page number
                logger.info(f"  → submitting page {current_page_num}")
                if not submit_page(page, current_page_num):
                    break

                try:
                    page.wait_for_selector(
                        "a[href*='details-donnees-essentielles']",
                        timeout=25_000,
                    )
                except PWTimeout:
                    try:
                        page.wait_for_load_state("networkidle", timeout=25_000)
                    except Exception:
                        pass

                page.wait_for_timeout(random.uniform(1200, 2000))

                html = page.content()
                self._save_debug(html, current_page_num)
                results.append(html)
                logger.info(f"  Captured page {current_page_num} ({len(html):,} bytes)")

                current_page_num += 1

            browser.close()

        return results

    @property
    def cookies(self) -> dict:
        return self._cookies_dict


# ══════════════════════════════════════════════════════════════════
# curl_cffi session
# ══════════════════════════════════════════════════════════════════

def _best_impersonate_profile() -> str:
    for c in ["chrome146", "chrome145", "chrome142", "chrome136", "chrome131",
              "chrome124", "chrome120", "chrome116", "chrome110"]:
        try:
            curl_requests.Session(impersonate=c)
            logger.info(f"curl_cffi impersonate: {c}")
            return c
        except Exception:
            continue
    return ""


# ══════════════════════════════════════════════════════════════════
# MAIN SCRAPER
# ══════════════════════════════════════════════════════════════════

class KlekoonAwardsScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-GB,en-US;q=0.9,fr;q=0.7",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   LISTING_URL,
    }

    def __init__(self, keyword: str = "", max_pages: int | None = None, headless: bool = True):
        self.keyword   = keyword
        self.max_pages = max_pages
        self.headless  = headless

        imp = _best_impersonate_profile()
        self.session = curl_requests.Session(impersonate=imp) if imp else curl_requests.Session()
        self.session.headers.update(self._HEADERS)

        self._detail_parser      = AwardDetailParser()
        self._debug_detail_saved = False

        mongo_uri   = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri)
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["klekoon_awards"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",  unique=True)
        self.col.create_index("de_id")
        self.col.create_index([("notification_date", -1)])
        logger.info("MongoDB → tender_bharo.klekoon_awards")

        self._scraped_ids: set = set()
        self._load_scraped_ids()

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_klekoon_awards"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
                     7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/AW/{now.year}/{month_map[now.month]}/{seq:08d}"

    def _load_scraped_ids(self):
        try:
            self._scraped_ids = set(str(i) for i in self.col.distinct("de_id"))
            logger.info(f"Resume: {len(self._scraped_ids)} awards already in DB")
        except Exception as e:
            logger.warning(f"Could not load scraped IDs: {e}")

    def _request_with_retry(self, url: str):
        for attempt in range(1, 6):
            try:
                r = self.session.get(url, timeout=45)
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = 2.0 ** attempt
                    logger.warning(f"  HTTP {r.status_code} attempt {attempt}, retry in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                return r
            except Exception as e:
                wait = 2.0 ** attempt
                if attempt == 5:
                    logger.error(f"  All attempts failed for {url}: {e}")
                    return None
                logger.warning(f"  Attempt {attempt} error: {e} — retry in {wait:.0f}s")
                time.sleep(wait)
        return None

    def _fetch_detail(self, de_id: str) -> BeautifulSoup | None:
        url = DETAIL_URL_TPL.format(de_id=de_id)
        r   = self._request_with_retry(url)
        if r is None:
            return None
        if not self._debug_detail_saved:
            with open("debug_detail_awards_klekoon.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)
            logger.info("Saved debug_detail_awards_klekoon.html")
            self._debug_detail_saved = True
        return BeautifulSoup(r.content, "lxml")

    def _inject_cookies(self, cookies: dict) -> None:
        self.session.cookies.update(cookies)
        logger.info(f"Injected {len(cookies)} cookies")

    # ── translate & build doc ─────────────────────────────────────

    def _translate_and_build(self, listing: dict, raw: dict) -> dict:
        """
        Translate FR text fields to EN, then build the final MongoDB document.
        Only English-language values are stored — no *_fr or *_raw fields.
        """
        # Fields that need Google Translate (free text)
        free_text = {
            "title":               raw.get("title_fr"),
            "place_of_performance": raw.get("place_of_performance_fr"),
            "description":         raw.get("description_fr"),
        }
        keys   = list(free_text.keys())
        values = [free_text[k] for k in keys]
        translated = translate_batch(values)
        en = dict(zip(keys, translated))

        # Static label translations
        procedure_type = tr(raw.get("procedure_type_fr"))
        contract_type  = tr(raw.get("contract_nature_fr"))
        price_form     = tr(raw.get("price_form_fr"))

        # Financial
        amount_ht  = parse_amount(raw.get("amount_ht_raw"))
        amount_ttc = parse_amount(raw.get("amount_ttc_raw"))

        # Translate CPV names
        cpv_items = raw.get("cpv_items", [])
        cpv_names = [c.get("name_fr") for c in cpv_items]
        cpv_en    = translate_batch(cpv_names)
        cpv_docs  = [
            {"cpv_code": c.get("cpv_code"), "name": en_name or c.get("name_fr")}
            for c, en_name in zip(cpv_items, cpv_en)
        ]

        # Translate lot descriptions
        lots    = raw.get("lots", [])
        lot_des = [l.get("description_fr") for l in lots]
        lot_en  = translate_batch(lot_des)
        lot_docs = [
            {"lot_number": l.get("lot_number"),
             "description": en_d or l.get("description_fr")}
            for l, en_d in zip(lots, lot_en)
        ]

        # Dates
        de_id      = raw.get("de_id") or listing.get("de_id")
        detail_url = raw.get("detail_url", listing.get("detail_url", ""))

        pub_date   = parse_date(raw.get("pub_date_raw") or listing.get("pub_date_raw"), "pub")
        notif_date = parse_date(raw.get("notification_date_raw"), "notif")
        award_date = parse_date(raw.get("award_date_raw"), "award")

        # Department: prefer parsed code, fall back to listing
        department = raw.get("dept_code") or listing.get("dept_raw")

        return {
            # Identifiers
            "hash_id":     generate_hash(de_id or detail_url),
            "teb_number":  self._teb_id(),
            "de_id":       de_id,
            "uid":         raw.get("uid"),
            "source":      "Klekoon – Données Essentielles",
            "record_type": "Contract Award",
            "source_url":  detail_url,
            "portal_url":  BASE_URL,

            # Core (English)
            "title":           en.get("title") or listing.get("title_raw"),
            "description":     en.get("description"),
            "procedure_type":  procedure_type,
            "contract_type":   contract_type,
            "price_form":      price_form,
            "duration_months": raw.get("duration_months"),
            "tender_reference": raw.get("de_id"),   # Klekoon uses DE_ID as reference

            # Authority
            "authority_name":  raw.get("authority_name") or listing.get("authority_raw"),
            "authority_siret": raw.get("authority_siret"),

            # Location
            "place_of_performance": en.get("place_of_performance"),
            "department":           department,

            # Dates
            "publication_date":  pub_date,
            "notification_date": notif_date,
            "award_date":        award_date,

            # Financial
            "total_amount_ht":  amount_ht,
            "total_amount_ttc": amount_ttc,
            "currency":         "EUR",

            # Awardees
            "awardees": [
                {
                    "name":     a.get("name"),
                    "siret":    a.get("siret"),
                    "city":     a.get("city"),
                    "postcode": a.get("postcode"),
                    "country":  a.get("country"),
                }
                for a in raw.get("awardees", [])
                if a.get("name") or a.get("siret")
            ],

            # Lots
            "lots": lot_docs,

            # CPV
            "cpv_items": cpv_docs,

            # DE reference IDs
            "de_publication_id": raw.get("pub_date_raw"),   # Klekoon doesn't expose a separate pub ID
            "boamp_ref":         raw.get("boamp_ref"),
            "joue_ref":          raw.get("joue_ref"),

            # ETL
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    # ── process one award ─────────────────────────────────────────

    def _process_award(self, listing: dict) -> bool:
        de_id = listing.get("de_id")
        if de_id and de_id in self._scraped_ids:
            logger.info(f"  ↷ skip: {de_id}")
            return False

        soup = self._fetch_detail(de_id)
        if soup is None:
            return False

        url  = DETAIL_URL_TPL.format(de_id=de_id)
        raw  = self._detail_parser.parse(soup, url, de_id)
        doc  = self._translate_and_build(listing, raw)

        try:
            self.col.insert_one(doc)
            if de_id:
                self._scraped_ids.add(de_id)
            logger.info(
                f"  ✓ {de_id} | {doc['teb_number']} "
                f"| {(doc.get('title') or '(no title)')[:60]}"
            )
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": doc["hash_id"]},
                {"$set": {**doc, "updated_at": datetime.now(timezone.utc)}},
            )
            if de_id:
                self._scraped_ids.add(de_id)
            logger.info(f"  ↺ updated: {de_id}")
        except Exception as e:
            logger.error(f"  DB insert failed [{de_id}]: {e}")
            return False

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        return True

    # ── main entry ────────────────────────────────────────────────

    def scrape(self) -> None:
        logger.info("=" * 60)
        logger.info("Klekoon Awards Scraper v2")
        logger.info(f"Keyword : {self.keyword or '(all)'}")
        logger.info("=" * 60)

        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        seen_ids       = set()
        inserted_total = 0

        def submit_page(pg, page_num: int) -> bool:
            try:
                pg.evaluate(f"""
                    (function() {{
                        var inp = document.getElementById('page-number');
                        if (inp) inp.value = {page_num};
                        var perp = document.getElementById('per-page');
                        if (perp) perp.value = 20;
                        document.getElementById('main-form').submit();
                    }})()
                """)
                return True
            except Exception as e:
                logger.warning(f"  submit_page({page_num}) failed: {e}")
                return False

        def get_total_pages(pg) -> int:
            try:
                links = pg.locator("ul.pagination li a[onclick*='get_page']").all()
                max_p = 1
                for lnk in links:
                    oc = lnk.get_attribute("onclick") or ""
                    m  = re.search(r"get_page\((\d+)", oc)
                    if m:
                        max_p = max(max_p, int(m.group(1)))
                return max_p if max_p > 1 else 1
            except Exception:
                return 1

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
                ),
                locale="fr-FR",
                viewport={"width": 1280, "height": 900},
            )
            pw_page = context.new_page()

            # ── Open listing page ──────────────────────────────
            logger.info("Playwright: opening listing page …")
            pw_page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)
            pw_page.wait_for_timeout(2000)

            # ── Select Données essentielles radio ──────────────
            try:
                radio = pw_page.locator("#rech-donnees")
                if not radio.is_checked():
                    radio.click()
                    pw_page.wait_for_timeout(1500)
                    logger.info("  Clicked #rech-donnees")
                else:
                    logger.info("  #rech-donnees already checked")
            except Exception as e:
                logger.warning(f"  Radio click failed: {e}")

            # ── Fill keyword ───────────────────────────────────
            if self.keyword:
                try:
                    pw_page.fill("#motcle", self.keyword, timeout=4000)
                    logger.info(f"  Keyword: {self.keyword}")
                except Exception:
                    pass

            # ── Initial form submit ────────────────────────────
            submitted = False
            try:
                pw_page.evaluate("document.getElementById('main-form').submit()")
                pw_page.wait_for_selector(
                    "a[href*='details-donnees-essentielles']", timeout=30_000
                )
                submitted = True
                logger.info("  Initial form submitted")
            except Exception as e:
                logger.warning(f"  Initial submit failed: {e}")

            if not submitted:
                logger.error("Could not load results page. Aborting.")
                browser.close()
                return

            # ── Inject cookies into curl session ───────────────
            cookies = {c["name"]: c["value"] for c in context.cookies()}
            self._inject_cookies(cookies)

            # ── Detect total pages ─────────────────────────────
            total_pages = get_total_pages(pw_page)
            fetch_up_to = total_pages if self.max_pages is None else min(total_pages, self.max_pages)
            logger.info(f"  Total pages: {total_pages} | Will fetch: {fetch_up_to}")

            # ── Page-by-page: fetch → parse → store ───────────
            for current_page_num in range(1, fetch_up_to + 1):
                logger.info(f"{'='*40}")
                logger.info(f"  Fetching listing page {current_page_num}/{fetch_up_to}")

                # Submit form for this page
                if not submit_page(pw_page, current_page_num):
                    logger.warning(f"  submit_page({current_page_num}) failed, stopping.")
                    break

                try:
                    pw_page.wait_for_selector(
                        "a[href*='details-donnees-essentielles']", timeout=25_000
                    )
                except PWTimeout:
                    try:
                        pw_page.wait_for_load_state("networkidle", timeout=25_000)
                    except Exception:
                        pass

                pw_page.wait_for_timeout(random.uniform(1000, 1800))

                # Save debug HTML for page 1
                if current_page_num == 1:
                    html = pw_page.content()
                    with open("debug_awards_listing_p1.html", "w", encoding="utf-8", errors="replace") as f:
                        f.write(html)
                    logger.info("  Saved debug_awards_listing_p1.html")
                else:
                    html = pw_page.content()

                # Parse cards from this page
                items, _ = parse_listing_page(html)
                logger.info(f"  Page {current_page_num}: {len(items)} cards found")

                # Process each card immediately
                page_inserted = 0
                for idx, listing in enumerate(items, 1):
                    de_id = listing.get("de_id")
                    if de_id in seen_ids:
                        continue
                    seen_ids.add(de_id)

                    logger.info(
                        f"    [{idx}/{len(items)}] DE_ID={de_id} "
                        f"| {(listing.get('title_raw') or '?')[:50]}"
                    )
                    try:
                        if self._process_award(listing):
                            page_inserted += 1
                            inserted_total += 1
                    except Exception as e:
                        logger.error(f"    Error [DE_ID={de_id}]: {e}")

                logger.info(
                    f"  Page {current_page_num} done — "
                    f"inserted: {page_inserted} | total so far: {inserted_total}"
                )

                # Polite delay between pages
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            browser.close()

        logger.info("=" * 60)
        logger.info(f"Done. Inserted/updated: {inserted_total}")
        logger.info("=" * 60)


# ══════════════════════════════════════════════════════════════════
# QUICK SELF-TEST  (run against the debug HTML you already have)
# python klekoon_awards_scraper.py --test-detail debug_detail_awards_klekoon.html
# ══════════════════════════════════════════════════════════════════

def _self_test(path: str) -> None:
    import json
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    soup   = BeautifulSoup(html, "lxml")
    parser = AwardDetailParser()
    raw    = parser.parse(soup, "TEST_URL", "TEST_DE_ID")
    print(json.dumps(raw, indent=2, default=str, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", "-k", default="")
    ap.add_argument("--pages",   "-p", type=int, default=0,
                    help="Pages to scrape; 0 = all (default)")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--test-detail", metavar="HTML_FILE",
                    help="Parse a local detail HTML file and print result (no DB/network)")
    args = ap.parse_args()

    if args.test_detail:
        _self_test(args.test_detail)
    else:
        KlekoonAwardsScraper(
            keyword=args.keyword,
            max_pages=args.pages if args.pages != 0 else None,
            headless=not args.no_headless,
        ).scrape()