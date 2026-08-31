"""
Klekoon.com – Public Procurement Scraper  (FIXED v2)
Source: https://www.klekoon.com/rechercher-une-annonce-ou-un-dce-dematerialise-sur-klekoon

Key fixes vs original
─────────────────────
• Listing parser  – now targets the real card container (div.col.kle-border)
  and reads Emetteur / Source+Procedure / Parution / Clôture from <span> text,
  not from table rows or regex over the full page.
• Detail parser   – now targets div.detail-consultation (class="blok detail-consultation")
  which holds free-text <p> blocks, NOT <table>/<dl>.  Field values are extracted
  by heading-span matching within that block.
• Title           – taken from the tender's own card link/heading, not from <h1>
  (the <h1> is the site-wide banner, not the tender title).
• Dates           – parsed from the real span labels "Parution", "Clôture",
  "Mise en ligne", "Date limite des offres".
• Authority       – "Acheteur public" block: name on the first <p> line,
  address on subsequent lines, phone via regex.
• Description     – "Informations complémentaires" <p> block.
• Lots            – "Liste des lots" table rows (each <td> is a lot description;
  lot numbers are absent in the HTML so we auto-assign 1, 2 …).
• Place           – "Lieu d'exécution" block value.
• Category        – "Catégorie de marché" field inside "Informations générales".
• Procedure       – "Source :" span on both listing cards and detail header bar.
• Reference       – "Référence de la consultation :" line.
• Online date     – "Mise en ligne :" line.
• DCE available   – detected from "DCE complet" text in dce-marche section.
• Documents       – now reads the #liste_dce table (div.dce-marche section),
  not by hunting random <a> tags with extension patterns.

Install
───────
pip install curl_cffi beautifulsoup4 deep-translator python-dotenv pymongo python-dateutil boto3 lxml

Run
───
python klekoon_scraper.py
python klekoon_scraper.py --pages 5
python klekoon_scraper.py --keyword "travaux"
"""

import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

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
logger = logging.getLogger("klekoon")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL    = "https://www.klekoon.com"
LISTING_URL = f"{BASE_URL}/rechercher-une-annonce-ou-un-dce-dematerialise-sur-klekoon"
PER_PAGE    = 20
DELAY_MIN   = 1.0
DELAY_MAX   = 2.5

LABEL_MAP = {
    "Appel d'offres ouvert":           "Open Tender",
    "Appel d'offres restreint":        "Restricted Tender",
    "Procédure adaptée":               "Adapted Procedure (MAPA)",
    "Procédure adaptée (MAPA)":        "Adapted Procedure (MAPA)",
    "Procédure négociée":              "Negotiated Procedure",
    "Dialogue compétitif":             "Competitive Dialogue",
    "Marché de partenariat":           "Partnership Contract",
    "Concession":                      "Concession",
    "Système d'acquisition dynamique": "Dynamic Purchasing System",
    "Accord-cadre":                    "Framework Agreement",
    "En cours":    "Active",
    "Clôturé":     "Closed",
    "Attribué":    "Awarded",
    "Annulé":      "Cancelled",
    "Publié":      "Published",
    "Oui": "Yes", "Non": "No",
    "oui": "Yes", "non": "No",
    "Travaux":     "Works",
    "Fournitures": "Supplies",
    "Services":    "Services",
    "Dématérialisé": "Dematerialised",
    "Papier":        "Paper",
    "Sans objet":    "N/A",
}

# ══════════════════════════════════════════════════════════════════
# UTILITY
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
    raw = re.sub(r"\s*[–\-]\s*\d+\s+jours?\s+restants?.*", "", raw, flags=re.I).strip()
    for fmt in (
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
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
    if text is None:
        return None
    return LABEL_MAP.get(str(text).strip(), str(text).strip())


def translate_batch(texts: list) -> list:
    if not texts or not HAVE_TRANSLATOR:
        return texts
    indices      = [i for i, t in enumerate(texts) if t and str(t).strip()]
    to_translate = [str(texts[i]) for i in indices]
    if not to_translate:
        return texts
    try:
        sep        = " ||| "
        joined     = sep.join(to_translate)
        translated = GoogleTranslator(source="fr", target="en").translate(joined)
        parts      = [p.strip() for p in translated.split("|||")]
        result     = list(texts)
        for idx, part in zip(indices, parts):
            result[idx] = part
        return result
    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")
        return texts


# ══════════════════════════════════════════════════════════════════
# LISTING PAGE PARSER  (FIXED)
# ══════════════════════════════════════════════════════════════════

_DETAIL_LINK_RE = re.compile(
    r"/appels-offres/avis/([^?\"'#]+)\?consultation_ID=(\d+)",
    re.I,
)


def parse_listing_page(html: str) -> tuple[list[dict], int]:
    """
    Parse the POST search-results HTML.

    Card structure (confirmed from debug HTML):
    ─────────────────────────────────────────
    <div class="col kle-border">
      <div class="row bg-light-grey ...">                   ← title row
        <a href="/appels-offres/avis/<slug>?consultation_ID=<id>...">TITLE</a>
      </div>
      <div class="row no-gutters bg-light-grey d-none d-lg-flex">  ← desktop meta
        <div class="col-6 ...">
          <span><b>Emetteur :</b> NAME (DEPT_CODE)</span>
          <span><b>Source :</b> Klekoon - PROCEDURE_TYPE</span>
        </div>
        <div class="col-2 ...">
          <span><b>Parution :</b>DD/MM/YYYY</span>
          <span class="text-success"><b>Clôture :</b>DD/MM/YYYY</span>
        </div>
      </div>
    </div>
    """
    soup  = BeautifulSoup(html, "lxml")
    items = []

    # ── Total result count ──────────────────────────────────────
    total = 0
    for tag in soup.find_all(string=re.compile(r"\d[\d\s]*\s+annonces?", re.I)):
        m = re.search(r"([\d\s]+)\s+annonces?", str(tag), re.I)
        if m:
            total = int(re.sub(r"\s", "", m.group(1)))
            break

    # ── Card containers ─────────────────────────────────────────
    # Each tender card is: <div class="col kle-border"> containing the detail link
    seen_ids = set()

    for card in soup.find_all("div", class_=lambda c: c and "kle-border" in c.split() and "col" in c.split()):
        a_tag = card.find("a", href=_DETAIL_LINK_RE)
        if not a_tag:
            continue

        href  = a_tag.get("href", "")
        match = _DETAIL_LINK_RE.search(href)
        if not match:
            continue

        slug            = match.group(1)
        consultation_id = match.group(2)
        if consultation_id in seen_ids:
            continue
        seen_ids.add(consultation_id)

        # Title — text of the canonical link (first anchor with rel=canonical, or the card link)
        canonical = card.find("a", rel="canonical") or a_tag
        title_raw = clean_text(canonical.get_text())

        # Meta row — look inside the card's text spans
        card_text = card.get_text(" ", strip=True)

        # Emetteur: "Emetteur : NAME (20)" — authority name + dept code
        authority_raw = None
        dept_raw      = None
        m_em = re.search(r"Emetteur\s*[:\-]?\s*(.+?)(?:\s*\((\d{2,3})\))?(?:\s+Source\s*:|$)", card_text, re.I)
        if m_em:
            authority_raw = clean_text(m_em.group(1))
            if m_em.group(2):
                dept_raw = m_em.group(2)

        # Source / Procedure: "Source : Klekoon - Procédure adaptée"
        procedure_raw = None
        m_src = re.search(r"Source\s*[:\-]\s*Klekoon\s*[–\-]\s*(.+?)(?:\s+Parution|$)", card_text, re.I)
        if m_src:
            procedure_raw = clean_text(m_src.group(1))

        # Parution (publication date): "Parution : 25/06/2026"
        pub_date_raw = None
        m_pub = re.search(r"Parution\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})", card_text, re.I)
        if m_pub:
            pub_date_raw = m_pub.group(1)

        # Clôture (deadline): "Clôture : 29/07/2026"
        deadline_raw = None
        m_dl = re.search(r"Cl[oô]ture\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2})?)", card_text, re.I)
        if m_dl:
            deadline_raw = m_dl.group(1)

        # Category from procedure or authority text
        category_raw = None
        for cat in ("Travaux", "Fournitures", "Services"):
            if re.search(rf"\b{cat}\b", card_text, re.I):
                category_raw = cat
                break

        detail_url = (
            f"{BASE_URL}/appels-offres/avis/{slug}"
            f"?consultation_ID={consultation_id}&page=1&dce=1"
        )

        items.append({
            "consultation_id":  consultation_id,
            "slug":             slug,
            "title_raw":        title_raw,
            "detail_url":       detail_url,
            "pub_date_raw":     pub_date_raw,
            "deadline_raw":     deadline_raw,
            "authority_raw":    authority_raw,
            "dept_raw":         dept_raw,
            "category_raw":     category_raw,
            "procedure_raw":    procedure_raw,
        })

    logger.info(f"  Parsed {len(items)} cards | total reported = {total}")
    return items, total


# ══════════════════════════════════════════════════════════════════
# DETAIL PAGE PARSER  (FIXED)
# ══════════════════════════════════════════════════════════════════

class DetailParser:
    """
    Parse a single Klekoon tender detail page.

    Page structure (confirmed from debug_detail_klekoon.html):
    ──────────────────────────────────────────────────────────
    The tender content lives in:

    1.  <div class="blok detail-consultation d-none">
          <table class="avis ...">
            <tr><td>
              <!-- "Acheteur public" block -->
              <div class="row pl-3">
                <span>Acheteur public</span>
                <p>NAME<br>ADDRESS<br>Téléphone : PHONE</p>
              </div>
              <!-- "Informations générales" block -->
              <div class="row pl-3">
                <span>Informations générales </span>
                <p>
                  Référence de la consultation : REF<br>
                  Mise en ligne : DATE<br>
                  Mode de passation : PROCEDURE<br>
                  Catégorie de marché : CATEGORY<br>
                  Classe d'activité : ...<br>
                  Département : NAME (CODE)<br>
                  Date limite des candidatures ...<br>
                  Date limite des offres ... : DATE
                </p>
              </div>
              <!-- "Objet de la consultation" block -->
              <div class="row pl-3">
                <span>Objet de la consultation </span>
                <p>TITLE / OBJECT TEXT</p>
              </div>
              <!-- "Liste des lots" block -->
              <div class="row pl-3">
                Liste des lots
                <table>
                  <tr><td>LOT DESCRIPTION</td></tr>...
                </table>
              </div>
              <!-- "Informations complémentaires" block -->
              <div class="row pl-3">
                <span>Informations complémentaires </span>
                <p>FREE-TEXT DESCRIPTION</p>
              </div>
              <!-- "Lieu d'exécution" block -->
              <div class="row pl-3">
                <span>Lieu d'exécution </span>
                <p>LOCATION</p>
              </div>
            </td></tr>
          </table>
        </div>

    2.  Summary bar (top of detail card, always visible):
        <div class="col-md-2 kle-border-right kle-border-bottom bg-light-grey">
          <span><b>Parution : </b>25/06/2026</span>
          <span class="text-success"><b>Clôture : </b>29/07/2026</span>
        </div>
        and
        <span><b>Source : </b>Klekoon - Procédure adaptée</span>

    3.  DCE section:
        <div class="blok dce-marche d-none">
          <table id="liste_dce">...</table>   ← document list
        </div>
    """

    def _block_value(self, detail_block, heading_pattern: str) -> str | None:
        """
        Within detail_block, find a <span> whose text matches heading_pattern,
        then return the text of the next <p> sibling (within the same parent <div>),
        joining <br> tags with newlines so each field stays on its own line.
        """
        for span in detail_block.find_all("span"):
            text = clean_text(span.get_text())
            if text and re.search(heading_pattern, text, re.I):
                parent = span.find_parent("div")
                if parent:
                    p = parent.find("p")
                    if p:
                        return self._p_to_lines_text(p)
        return None

    def _p_to_lines_text(self, p_tag) -> str:
        """Convert a <p> with <br> children to newline-separated text."""
        parts = []
        for child in p_tag.children:
            name = getattr(child, "name", None)
            if name == "br":
                continue   # skip <br> — we already split on it below
            if name is None:
                txt = clean_text(str(child))
                if txt:
                    parts.append(txt)
            else:
                txt = clean_text(child.get_text())
                if txt:
                    parts.append(txt)
        return "\n".join(parts) if parts else ""

    def _p_lines(self, p_tag) -> list[str]:
        """Return non-empty lines from a <p> tag, splitting on <br>."""
        lines = []
        buf   = []
        for child in p_tag.children:
            name = getattr(child, "name", None)
            if name == "br":
                txt = clean_text(" ".join(buf))
                if txt:
                    lines.append(txt)
                buf = []
            elif name is None:
                buf.append(str(child))
            else:
                buf.append(child.get_text())
        txt = clean_text(" ".join(buf))
        if txt:
            lines.append(txt)
        return [l for l in lines if l]

    def _block_element(self, detail_block, heading_pattern: str):
        """Return the parent <div> for a section heading."""
        for span in detail_block.find_all("span"):
            text = clean_text(span.get_text())
            if text and re.search(heading_pattern, text, re.I):
                return span.find_parent("div")
        return None

    def _parse_info_gen(self, detail_block) -> dict:
        """
        Parse the "Informations générales" section by reading line-by-line from
        the actual <p> tag (splitting on <br> so each field stays on its own line).
        """
        result = {}

        # Locate the section div
        section_div = self._block_element(detail_block, r"Informations?\s+g[eé]n[eé]rales?")
        if not section_div:
            return result

        p = section_div.find("p")
        if not p:
            return result

        # Split by <br> tags so each field is on its own line
        lines = self._p_lines(p)

        patterns = {
            "reference":       r"R[ée]f[ée]rence\s+de\s+la\s+consultation\s*:\s*(.+)",
            "online_date_raw": r"Mise\s+en\s+ligne\s*:\s*(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)",
            "procedure_type":  r"Mode\s+de\s+passation\s*:\s*(.+)",
            "category":        r"Cat[ée]gorie\s+de\s+march[ée]\s*:\s*(.+)",
            "activity_class":  r"Classe\s+d.activit[ée]\s*:\s*(.+)",
            "department":      r"D[ée]partement\s*:\s*(.+)",
            "deadline_cand":   r"Date\s+limite\s+des\s+candidatures[^:]*:\s*(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2})?)",
            "deadline_offers": r"Date\s+limite\s+des\s+offres[^:]*:\s*(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2})?)",
        }

        for line in lines:
            if not line:
                continue
            for key, pat in patterns.items():
                if key in result:
                    continue  # already found
                m = re.match(pat, line, re.I)
                if m:
                    val = clean_text(m.group(1))
                    if val:
                        result[key] = val

        # Special case: deadline may be on a standalone line (just the date) right after
        # "Date limite des offres (heure de Paris) :" which may have no trailing value.
        # In that case the <font> line immediately follows.
        if "deadline_offers" not in result:
            for i, line in enumerate(lines):
                if re.search(r"Date\s+limite\s+des\s+offres", line, re.I):
                    # next non-empty line might be the date
                    for j in range(i+1, min(i+3, len(lines))):
                        m = re.match(r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2})?)", lines[j].strip())
                        if m:
                            result["deadline_offers"] = m.group(1)
                            break

        return result

    def _parse_authority(self, detail_block) -> dict:
        """
        Parse the "Acheteur public" block.
        Structure:
          <span>Acheteur public</span>
          <p>
            NAME<br>
            ADDRESS LINE 1<br>
            ADDRESS LINE 2 POSTCODE CITY COUNTRY<br>
            Téléphone : PHONE
          </p>
        """
        result = {}
        section_div = self._block_element(detail_block, r"Acheteur\s+public")
        if not section_div:
            return result

        p = section_div.find("p")
        if not p:
            return result

        lines = self._p_lines(p)

        if lines:
            result["authority_name"] = lines[0]

        address_lines = []
        for line in lines[1:]:
            m_phone = re.match(r"T.{0,4}l.{0,4}phone\s*[:\-]\s*(.+)", line, re.I)
            m_email = re.match(r"[Ee][-.]?mail\s*[:\-]\s*(.+)", line, re.I)
            m_siret = re.match(r"SIRET\s*[:\-]\s*(.+)", line, re.I)
            if m_phone:
                result["authority_phone"] = clean_text(m_phone.group(1))
            elif m_email:
                result["authority_email"] = clean_text(m_email.group(1))
            elif m_siret:
                result["authority_siret"] = clean_text(m_siret.group(1))
            else:
                # Also check if phone is embedded mid-line
                m_emb = re.search(r"T.{0,4}l.{0,4}phone\s*[:\-]\s*([\d\s]+)", line, re.I)
                if m_emb:
                    result["authority_phone"] = clean_text(m_emb.group(1))
                    line = re.sub(r"\s*T.{0,4}l.{0,4}phone\s*[:\-]\s*[\d\s]+", "", line, flags=re.I).strip()
                    if line:
                        address_lines.append(line)
                else:
                    address_lines.append(line)

        if address_lines:
            result["authority_address"] = " | ".join(address_lines)

        return result

    # ── header summary bar ───────────────────────────────────────

    def _parse_summary_bar(self, soup: BeautifulSoup) -> dict:
        """
        Parse the always-visible header summary bar for:
          - Parution (publication date)
          - Clôture  (deadline)
          - Source / Procedure type
        These are in spans within div.col-md-2.kle-border-right or div.col-6
        in the main card header area.
        """
        result = {}
        page_text = soup.get_text(" ", strip=True)

        # Parution
        m = re.search(r"Parution\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})", page_text, re.I)
        if m:
            result["publication_date_raw"] = m.group(1)

        # Clôture
        m = re.search(r"Cl[oô]ture\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{2}:\d{2})?)", page_text, re.I)
        if m:
            result["deadline_raw"] = m.group(1)

        # Source / Procedure — "Source : Klekoon - Procédure adaptée"
        m = re.search(r"Source\s*[:\-]\s*Klekoon\s*[–\-]\s*(.+?)(?:\s+Parution|\s+Cl[oô]ture|$)", page_text, re.I)
        if m:
            result["procedure_type_fr"] = clean_text(m.group(1))

        return result

    # ── lots ─────────────────────────────────────────────────────

    def _extract_lots(self, detail_block) -> list[dict]:
        """
        The lots section:
          <div class="row pl-3 mt-3">
            <div class="col-sm-12">
              Liste des lots
              <table>
                <tbody>
                  <tr><td>Lot : DESCRIPTION</td></tr>
                  <tr><td>FULL DESCRIPTION</td></tr>
                  ...
                </tbody>
              </table>
            </div>
          </div>
        Each lot is one <tbody> with two rows: short title row, long description row.
        """
        lots = []
        # Find the div containing "Liste des lots" text
        lots_div = None
        for div in detail_block.find_all("div"):
            if re.search(r"Liste\s+des\s+lots", div.get_text(), re.I):
                lots_div = div
                break
        if not lots_div:
            return lots

        lot_counter = 0
        for tbody in lots_div.find_all("tbody"):
            rows = [r for r in tbody.find_all("tr") if clean_text(r.get_text())]
            if not rows:
                continue
            # First row is typically "Lot : SHORT_TITLE", second is full description
            first_td  = rows[0].find("td")
            second_td = rows[1].find("td") if len(rows) > 1 else None

            short = clean_text(first_td.get_text()) if first_td else None
            full  = clean_text(second_td.get_text()) if second_td else None

            # Strip leading "Lot : " prefix
            if short:
                short = re.sub(r"^Lot\s*[:\-]?\s*", "", short, flags=re.I).strip() or short

            # Auto-number lots (no explicit numbers in the HTML)
            lot_counter += 1
            lots.append({
                "lot_number":    str(lot_counter),
                "description_fr": full or short,
            })

        return lots

    # ── documents ────────────────────────────────────────────────

    def _extract_documents(self, soup: BeautifulSoup) -> list[dict]:
        """
        Read the DCE document list from #liste_dce table inside div.dce-marche.
        Each document row:
          <tr>
            <td><div>...<span>FILENAME.pdf</span></div></td>
            <td>DD/MM/YYYY HH:MM:SS</td>
            <td>SIZE</td>
          </tr>
        The download link is in a separate <a> row "Télécharger le RC".
        Individual piece downloads require login, so we only record names/links.
        """
        docs = []
        seen = set()

        dce_section = soup.find("div", class_=lambda c: c and "dce-marche" in c.split())
        if not dce_section:
            return docs

        table = dce_section.find("table", id="liste_dce")
        if not table:
            return docs

        for tr in table.find_all("tr"):
            # Download button rows (colspan=3, contain <a> with Télécharger)
            a_tag = tr.find("a", href=re.compile(r"telechargement|telecharger", re.I))
            if a_tag:
                href     = a_tag.get("href", "")
                full_url = href if href.startswith("http") else urljoin(BASE_URL, href)
                name     = clean_text(a_tag.get_text()) or os.path.basename(urlparse(full_url).path)
                if full_url not in seen:
                    docs.append({
                        "name_fr":   name,
                        "name_en":   None,
                        "file_url":  full_url,
                        "file_name": os.path.basename(urlparse(full_url).path) or name,
                        "type":      "RC",   # Règlement de Consultation
                        "s3_path":   None,
                        "uploaded_at": None,
                    })
                    seen.add(full_url)
                continue

            # Piece filename rows: <span>FILENAME</span> + date + size columns
            tds = tr.find_all("td")
            if len(tds) >= 2:
                # Skip checkbox-only rows and header rows
                span = tds[0].find("span")
                if span:
                    fname = clean_text(span.get_text())
                    if fname and fname not in seen and not fname.startswith("DCE complet"):
                        upload_date = clean_text(tds[1].get_text()) if len(tds) > 1 else None
                        size        = clean_text(tds[2].get_text()) if len(tds) > 2 else None
                        docs.append({
                            "name_fr":   fname,
                            "name_en":   None,
                            "file_url":  None,   # only downloadable after login/captcha
                            "file_name": fname,
                            "type":      "DCE",
                            "upload_date": upload_date,
                            "size":        size,
                            "s3_path":     None,
                            "uploaded_at": None,
                        })
                        seen.add(fname)

        return docs

    # ── CPV ──────────────────────────────────────────────────────

    def _extract_cpv(self, page_text: str) -> list[dict]:
        cpv_items  = []
        seen_codes = set()
        for m in re.finditer(r"(\d{8}-\d)\s*[:\-–]?\s*([A-ZÀ-ÿa-z][^\n\r]{3,80})?", page_text):
            code = m.group(1)
            if code in seen_codes:
                continue
            seen_codes.add(code)
            name = clean_text(m.group(2)) if m.group(2) else None
            cpv_items.append({"cpv_code": code, "name_fr": name, "name_en": None})
        return cpv_items

    # ── main parse ───────────────────────────────────────────────

    def parse(self, soup: BeautifulSoup, detail_url: str, consultation_id: str) -> dict:
        result: dict = {
            "detail_url":      detail_url,
            "consultation_id": consultation_id,
        }
        page_text = soup.get_text(" ", strip=True)

        # ── Locate the main detail block ────────────────────────
        detail_block = soup.find(
            "div",
            class_=lambda c: c and "detail-consultation" in c.split(),
        )
        if not detail_block:
            logger.warning(f"[{consultation_id}] detail-consultation block not found; using full page fallback")
            detail_block = soup  # fallback — less precise

        # ── Summary bar (Parution, Clôture, Source/Procedure) ───
        bar = self._parse_summary_bar(soup)
        result.update(bar)

        # ── Title — "Objet de la consultation" block ────────────
        result["title_fr"] = self._block_value(detail_block, r"Objet\s+de\s+la\s+consultation")

        # Fallback: try the canonical link in the page header card
        if not result["title_fr"]:
            a_tag = soup.find("a", href=re.compile(rf"consultation_ID={consultation_id}"), rel="canonical")
            if a_tag:
                result["title_fr"] = clean_text(a_tag.get_text())

        # ── Authority block ─────────────────────────────────────
        auth = self._parse_authority(detail_block)
        result["authority_name_fr"]    = auth.get("authority_name")
        result["authority_address_fr"] = auth.get("authority_address")
        result["authority_phone"]      = auth.get("authority_phone")
        result["authority_email"]      = auth.get("authority_email")

        # ── Informations générales ──────────────────────────────
        info_gen = self._parse_info_gen(detail_block)

        result["tender_number"]       = info_gen.get("reference")
        result["online_date_raw"]     = info_gen.get("online_date_raw")
        result["category_fr"]         = info_gen.get("category")
        result["activity_class"]      = info_gen.get("activity_class")
        result["department"]          = info_gen.get("department")

        # Use deadline from info_gen if not already found in summary bar
        if not result.get("deadline_raw"):
            result["deadline_raw"] = (
                info_gen.get("deadline_offers") or info_gen.get("deadline_cand")
            )

        # Use online date as publication_date_raw fallback
        if not result.get("publication_date_raw") and info_gen.get("online_date_raw"):
            result["publication_date_raw"] = info_gen["online_date_raw"]

        # Procedure type from info_gen if not from bar
        if not result.get("procedure_type_fr") and info_gen.get("procedure_type"):
            result["procedure_type_fr"] = info_gen["procedure_type"]

        # ── Description — "Informations complémentaires" ────────
        result["description_fr"] = self._block_value(
            detail_block, r"Informations?\s+compl[eé]mentaires?"
        )

        # ── Place of performance ─────────────────────────────────
        result["place_of_performance_fr"] = self._block_value(
            detail_block, r"Lieu\s+d.ex[eé]cution"
        )
        # Also scan inside description
        if not result["place_of_performance_fr"] and result.get("description_fr"):
            m = re.search(r"Lieu\s+d.ex[eé]cution\s*[:\-]\s*(.+?)(?:\n|$)",
                          result["description_fr"], re.I)
            if m:
                result["place_of_performance_fr"] = clean_text(m.group(1))

        # ── Lots ────────────────────────────────────────────────
        result["lots"] = self._extract_lots(detail_block)

        # ── CPV codes ───────────────────────────────────────────
        result["cpv_items"] = self._extract_cpv(page_text)

        # ── Documents ───────────────────────────────────────────
        result["documents"] = self._extract_documents(soup)

        # ── DCE available flag ───────────────────────────────────
        result["dce_available"] = bool(
            soup.find("b", string=re.compile(r"DCE complet", re.I))
            or re.search(r"DCE complet", page_text, re.I)
        )

        # ── Subdivision / variants / estimated value ─────────────
        # description_fr is now newline-joined per line (from _p_to_lines_text),
        # so \n is a reliable line boundary.
        desc = result.get("description_fr") or ""
        m_lots = re.search(r"D[eé]coupage\s+en\s+(?:tranches?\s+ou\s+en\s+)?lots?\s*[:\-]\s*([^\n\r]+)", desc, re.I | re.MULTILINE)
        result["subdivision_lots"] = clean_text(m_lots.group(1)) if m_lots else None

        m_var = re.search(r"\bVariantes?\s*[:\-]\s*([^\n\r]+)", desc, re.I | re.MULTILINE)
        result["variants_allowed"] = clean_text(m_var.group(1)) if m_var else None

        m_val = re.search(r"(?:Valeur\s+estim[eé]e|Montant\s+estim[eé]|Valeur\s+totale)\s*[:\-]\s*(.+?)(?:\n|$)", desc, re.I)
        result["estimated_value_raw"] = clean_text(m_val.group(1)) if m_val else None

        # ── Electronic submission ────────────────────────────────
        elec = soup.find("a", href=re.compile(r"repondre|soumettre|deposer|candidature|depot", re.I))
        result["electronic_submission_url"] = (
            urljoin(BASE_URL, elec["href"]) if elec else None
        )

        return result


# ══════════════════════════════════════════════════════════════════
# CURL_CFFI PROFILE HELPER
# ══════════════════════════════════════════════════════════════════

def _best_impersonate_profile() -> str:
    candidates = [
        "chrome146", "chrome145", "chrome142", "chrome136",
        "chrome133a", "chrome131", "chrome124", "chrome123",
        "chrome120", "chrome119", "chrome116", "chrome110",
        "chrome107", "chrome104", "chrome101", "chrome100", "chrome99",
    ]
    try:
        from curl_cffi.requests import BrowserType
        supported = {b.value for b in BrowserType}
        for c in candidates:
            if c in supported:
                return c
    except Exception:
        pass
    for c in candidates:
        try:
            curl_requests.Session(impersonate=c)
            return c
        except Exception:
            continue
    return ""


# ══════════════════════════════════════════════════════════════════
# MAIN SCRAPER
# ══════════════════════════════════════════════════════════════════

class KlekoonScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language":           "en-GB,en-US;q=0.9,en;q=0.8,fr;q=0.7",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   LISTING_URL,
    }

    _BASE_PAYLOAD = {
            "list_region":       "",
            "per_page":          "20",
            "selection_list":    "",
            "retour":            "0",
            "id_retour":         "",
            "type_recherche":    "1",
            "motcle":            "",
            "critere_emetteur":  "0",
            "source":            "0",
        }

    def __init__(
        self,
        keyword:   str = "",
        max_pages: int | None = None,
        regions:   list[str] | None = None,
    ):
        self.keyword   = keyword
        self.max_pages = max_pages
        self.regions   = regions or []

        _impersonate = _best_impersonate_profile()
        logger.info(f"curl_cffi impersonate profile: {_impersonate}")
        self.session = curl_requests.Session(impersonate=_impersonate)
        self.session.headers.update(self._HEADERS)

        self._detail_parser       = DetailParser()
        self._debug_detail_saved  = False
        self._debug_listing_saved = False

        mongo_uri   = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri)
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["klekoon_tenders"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",         unique=True)
        self.col.create_index("consultation_id")
        self.col.create_index([("bid_status", 1), ("deadline", -1)])
        logger.info("MongoDB connected → tender_bharo.klekoon_tenders")

        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/klekoon"
        if HAVE_BOTO3 and self.bucket:
            import boto3 as _boto3
            self.s3 = _boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
            logger.info(f"S3 configured: bucket={self.bucket}")
        else:
            self.s3 = None

        self._scraped_ids: set = set()
        self._load_scraped_ids()

    # ── TEB ID ───────────────────────────────────────────────────

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_klekoon"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
                     7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    def _load_scraped_ids(self):
        try:
            ids = self.col.distinct("consultation_id")
            self._scraped_ids = set(str(i) for i in ids)
            logger.info(f"Resume: {len(self._scraped_ids)} tenders already in DB")
        except Exception as e:
            logger.warning(f"Could not load scraped IDs: {e}")

    # ── retry ────────────────────────────────────────────────────

    def _request_with_retry(self, method: str, url: str, **kwargs):
        max_retries = 5
        backoff     = 2.0
        for attempt in range(1, max_retries + 1):
            try:
                fn = getattr(self.session, method)
                r  = fn(url, timeout=45, **kwargs)
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = backoff ** attempt
                    logger.warning(f"  HTTP {r.status_code} attempt {attempt}, retry in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                return r
            except Exception as e:
                wait = backoff ** attempt
                if attempt == max_retries:
                    logger.error(f"  All {max_retries} attempts failed for {url}: {e}")
                    return None
                logger.warning(f"  Error attempt {attempt}: {e} — retry in {wait:.0f}s")
                time.sleep(wait)
        return None

    def _init_session(self):
        logger.info("Initialising session (GET listing page) …")
        r = self._request_with_retry("get", LISTING_URL)
        if r:
            logger.info(f"  Cookies: {dict(self.session.cookies)}")
        else:
            logger.warning("Session init failed — will still attempt POST requests")

    # ── fetch listing ─────────────────────────────────────────────

    def _fetch_listing_page(self, page_num: int) -> str | None:
        # On first page, do a GET first to get session cookie + CSRF state
        if page_num == 1:
            self._request_with_retry("get", LISTING_URL)

        payload = dict(self._BASE_PAYLOAD)
        payload["page"]   = str(page_num)
        payload["motcle"] = self.keyword
        if self.regions:
            payload["list_region"] = ",".join(self.regions)

        logger.info(f"Fetching listing page {page_num} (POST) …")
        r = self._request_with_retry(
            "post",
            LISTING_URL,
            data=payload,
            headers={
                "Content-Type":   "application/x-www-form-urlencoded",
                "Origin":         BASE_URL,
                "Cache-Control":  "max-age=0",
                "Referer":        LISTING_URL,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
                "sec-fetch-user": "?1",
            },
        )
        if r is None:
            return None

        # Check if we got real results (cards with consultation links)
        if "consultation_ID=" not in r.text:
            logger.warning(f"  Page {page_num}: no tender cards in response — possible session issue")

        if not self._debug_listing_saved:
            with open("debug_listing_klekoon.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)
            logger.info("Saved debug_listing_klekoon.html")
            self._debug_listing_saved = True

        return r.text
        if r is None:
            return None

        if not self._debug_listing_saved:
            with open("debug_listing_klekoon.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)
            logger.info("Saved debug_listing_klekoon.html")
            self._debug_listing_saved = True

        return r.text

    # ── fetch detail ──────────────────────────────────────────────

    def _fetch_detail(self, listing: dict) -> BeautifulSoup | None:
        url = listing["detail_url"]
        r   = self._request_with_retry("get", url)
        if r is None:
            return None

        if not self._debug_detail_saved:
            with open("debug_detail_klekoon.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)
            logger.info("Saved debug_detail_klekoon.html")
            self._debug_detail_saved = True

        return BeautifulSoup(r.content, "lxml")

    # ── translation ───────────────────────────────────────────────

    def _translate_detail(self, raw: dict) -> dict:
        fr_fields = [
            "title_fr", "description_fr", "procedure_type_fr",
            "category_fr", "authority_name_fr", "authority_address_fr",
            "place_of_performance_fr",
        ]
        values     = [raw.get(f) for f in fr_fields]
        translated = translate_batch(values)
        d          = dict(raw)
        for field, trans in zip(fr_fields, translated):
            en_key = field.replace("_fr", "_en")
            static = tr(raw.get(field))
            d[en_key] = static if (static != raw.get(field)) else trans

        for bool_field in ("subdivision_lots", "variants_allowed"):
            d[bool_field] = tr(raw.get(bool_field)) or raw.get(bool_field)

        cpv_names = [c.get("name_fr") for c in raw.get("cpv_items", [])]
        for item, name_en in zip(d.get("cpv_items", []), translate_batch(cpv_names)):
            item["name_en"] = name_en

        doc_names = [doc.get("name_fr") for doc in raw.get("documents", [])]
        for doc, name_en in zip(d.get("documents", []), translate_batch(doc_names)):
            doc["name_en"] = name_en

        lot_descs = [lot.get("description_fr") for lot in raw.get("lots", [])]
        for lot, desc_en in zip(d.get("lots", []), translate_batch(lot_descs)):
            lot["description_en"] = desc_en

        return d

    # ── bid status ────────────────────────────────────────────────

    def _bid_status(self, detail: dict, listing: dict) -> str:
        deadline_raw = detail.get("deadline_raw") or listing.get("deadline_raw")
        deadline_dt  = parse_date(deadline_raw, "deadline")
        if deadline_dt:
            return "Open" if deadline_dt > datetime.now(timezone.utc) else "Closed"
        return "Open"

    # ── build Mongo document ──────────────────────────────────────

    def _build_doc(self, listing: dict, detail: dict) -> dict:
        cid        = detail.get("consultation_id") or listing.get("consultation_id")
        detail_url = detail.get("detail_url", listing.get("detail_url", ""))

        def en(fr_key: str) -> str | None:
            en_key = fr_key.replace("_fr", "_en")
            return detail.get(en_key) or None

        pub_raw = detail.get("publication_date_raw") or listing.get("pub_date_raw")
        dl_raw  = detail.get("deadline_raw") or listing.get("deadline_raw")

        return {
            "hash_id":         generate_hash(cid or detail_url),
            "teb_number":      self._teb_id(),
            "consultation_id": cid,
            "tender_number":   detail.get("tender_number"),
            "bid_status":      self._bid_status(detail, listing),
            "source":          "Klekoon",
            "source_url":      detail_url,
            "portal_url":      BASE_URL,
            "title":           en("title_fr") or listing.get("title_raw"),
            "description":     en("description_fr"),
            "procedure_type":  (
                en("procedure_type_fr")
                or tr(listing.get("procedure_raw"))
                or listing.get("procedure_raw")
            ),
            "category":        en("category_fr") or tr(listing.get("category_raw")),
            "activity_class":  detail.get("activity_class"),
            "authority_name":    en("authority_name_fr") or listing.get("authority_raw"),
            "authority_address": en("authority_address_fr"),
            "authority_email":   detail.get("authority_email"),
            "authority_phone":   detail.get("authority_phone"),
            "authority_siret":   detail.get("authority_siret"),
            "place_of_performance": (
                en("place_of_performance_fr")
                or detail.get("department")
                or listing.get("dept_raw")
            ),
            "department":          detail.get("department") or listing.get("dept_raw"),
            "subdivision_lots":    tr(detail.get("subdivision_lots")),
            "variants_allowed":    tr(detail.get("variants_allowed")),
            "estimated_value_raw": detail.get("estimated_value_raw"),
            "dce_available":       detail.get("dce_available", False),
            "electronic_submission_url": detail.get("electronic_submission_url"),
            # Dates
            "publication_date_raw": pub_raw,
            "publication_date":     parse_date(pub_raw, "publication_date"),
            "deadline_raw":  dl_raw,
            "deadline":      parse_date(dl_raw, "deadline"),
            "online_date_raw": detail.get("online_date_raw"),
            "online_date":     parse_date(detail.get("online_date_raw"), "online_date"),
            # Structured
            "lots": [
                {
                    "lot_number":  lot.get("lot_number"),
                    "description": lot.get("description_en") or lot.get("description_fr"),
                }
                for lot in detail.get("lots", [])
            ],
            "cpv_items": [
                {"cpv_code": i.get("cpv_code"), "name": i.get("name_en")}
                for i in detail.get("cpv_items", [])
            ],
            "documents": [
                {
                    "name":         doc.get("name_en") or doc.get("name_fr"),
                    "original_url": doc.get("file_url"),
                    "title":        doc.get("name_en") or doc.get("file_name"),
                    "type":         doc.get("type", "DCE"),
                    "upload_date":  doc.get("upload_date"),
                    "size":         doc.get("size"),
                    "s3_path":      doc.get("s3_path"),
                    "uploaded_at":  doc.get("uploaded_at"),
                }
                for doc in detail.get("documents", [])
            ],
            "etl_status": "pending",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    # ── S3 upload ─────────────────────────────────────────────────

    def _upload_docs_s3(self, doc: dict, mongo_id) -> None:
        if not self.s3:
            return
        folder  = f"{doc.get('consultation_id', 'unknown')}_{mongo_id}"
        updated = []
        ext_map = {
            "application/pdf":   ".pdf",
            "application/zip":   ".zip",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        }
        for att in doc.get("documents", []):
            url = att.get("file_url") or att.get("original_url")
            if not url:
                updated.append(att)
                continue
            try:
                r = self._request_with_retry("get", url)
                if not r:
                    updated.append(att)
                    continue
                ct    = r.headers.get("content-type", "application/octet-stream").split(";")[0]
                fname = att.get("file_name") or os.path.basename(urlparse(url).path) or "document"
                if not os.path.splitext(fname)[1]:
                    fname += ext_map.get(ct, ".bin")
                key = f"{self.s3_folder}/{folder}/{fname}"
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=r.content, ContentType=ct)
                att["s3_path"]     = f"s3://{self.bucket}/{key}"
                att["uploaded_at"] = datetime.now(timezone.utc)
                logger.info(f"    S3 ✓ {fname}")
            except Exception as e:
                logger.warning(f"    S3 failed {url}: {e}")
            updated.append(att)
            time.sleep(random.uniform(0.8, 1.5))
        self.col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})

    # ── process one tender ────────────────────────────────────────

    def _process_tender(self, listing: dict) -> bool:
        cid = listing.get("consultation_id")
        if cid and cid in self._scraped_ids:
            logger.info(f"  ↷ skip (already in DB): {cid}")
            return False

        soup = self._fetch_detail(listing)
        if soup is None:
            return False

        raw    = self._detail_parser.parse(soup, listing["detail_url"], cid)
        detail = self._translate_detail(raw)

        # Merge any listing-level procedure that detail didn't find
        if not detail.get("procedure_type_fr") and listing.get("procedure_raw"):
            detail["procedure_type_fr"] = listing["procedure_raw"]

        final_doc = self._build_doc(listing, detail)

        try:
            result = self.col.insert_one(final_doc)
            if cid:
                self._scraped_ids.add(cid)
            logger.info(
                f"  ✓ {cid} | {final_doc['teb_number']} "
                f"| {final_doc['bid_status']} "
                f"| {(final_doc.get('title') or '(no title)')[:60]}"
            )
            if final_doc.get("documents") and self.s3:
                self._upload_docs_s3(final_doc, result.inserted_id)
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": final_doc["hash_id"]},
                {"$set": {**final_doc, "updated_at": datetime.now(timezone.utc)}},
            )
            if cid:
                self._scraped_ids.add(cid)
            logger.info(f"  ↺ {cid} updated")
        except Exception as e:
            logger.error(f"  DB insert failed [{cid}]: {e}")
            return False

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        return True

    # ── main entry point ──────────────────────────────────────────

    def scrape(self) -> None:
        logger.info("=" * 60)
        logger.info("Starting Klekoon scraper (v2 – fixed parsers)")
        logger.info(f"Source:  {LISTING_URL}")
        logger.info(f"Keyword: {self.keyword or '(all)'}")
        logger.info(f"Regions: {self.regions or '(all)'}")
        logger.info("=" * 60)

       # Session initialised automatically on first listing page fetch

        seen_cids:     set = set()
        page           = 1
        total          = None
        inserted_total = 0

        while True:
            if self.max_pages and page > self.max_pages:
                logger.info(f"Reached max_pages={self.max_pages}, stopping.")
                break

            html = self._fetch_listing_page(page)
            if not html:
                logger.warning(f"Empty response on page {page}, stopping.")
                break

            items, reported_total = parse_listing_page(html)
            if total is None and reported_total:
                total = reported_total
                logger.info(f"Total results reported by site: {total}")

            if not items:
                logger.info(f"No items on page {page}, pagination complete.")
                break

            page_listings = []
            for item in items:
                cid = item.get("consultation_id")
                if cid and cid not in seen_cids:
                    seen_cids.add(cid)
                    page_listings.append(item)

            logger.info(
                f"Page {page}: {len(page_listings)} new tenders"
                + (f" | site total={total}" if total else "")
            )

            for idx, listing in enumerate(page_listings, 1):
                logger.info(
                    f"  [{idx}/{len(page_listings)}] "
                    f"id={listing.get('consultation_id')} "
                    f"| {(listing.get('title_raw') or '?')[:60]}"
                )
                try:
                    if self._process_tender(listing):
                        inserted_total += 1
                except Exception as e:
                    logger.error(f"  Unhandled error [{listing.get('detail_url')}]: {e}")

            logger.info(f"Page {page} complete — inserted/updated so far: {inserted_total}")

            if total and len(seen_cids) >= total:
                logger.info("Collected all reported results.")
                break

            page += 1
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        logger.info("=" * 60)
        logger.info(f"DONE. Inserted/updated this run: {inserted_total}")
        logger.info(f"Unique tenders in DB: {len(self._scraped_ids)}")
        logger.info("=" * 60)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Klekoon.com public procurement scraper v2")
    ap.add_argument("--keyword", "-k", default="",
                    help="Search keyword (motcle). Default: empty = all tenders.")
    ap.add_argument("--pages",   "-p", type=int, default=0,
                    help="Number of listing pages to fetch. 0 = all pages (default).")
    ap.add_argument("--regions", "-r", nargs="*", default=[],
                    help="Region IDs to filter (space-separated). Default: all.")
    ap.add_argument("--no-headless", action="store_true",
                    help="(Ignored – kept for compatibility)")
    args = ap.parse_args()

    KlekoonScraper(
        keyword=args.keyword,
        max_pages=args.pages if args.pages != 0 else None,
        regions=args.regions,
    ).scrape()