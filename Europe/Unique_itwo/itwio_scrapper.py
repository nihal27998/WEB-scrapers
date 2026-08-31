
import asyncio
import hashlib
import json
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
    from playwright.async_api import async_playwright, Page, Response
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")

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
logger = logging.getLogger("rib_playwright")

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════

BASE_URL    = "https://meinauftrag.rib.de"
LISTING_URL = f"{BASE_URL}/public/publications"

LABEL_MAP = {
    "Öffentliche Ausschreibung":    "Open Tendering",
    "Beschränkte Ausschreibung":    "Restricted Tendering",
    "Freihändige Vergabe":          "Negotiated Award",
    "Verhandlungsverfahren":        "Negotiated Procedure",
    "Offenes Verfahren":            "Open Procedure",
    "Nichtoffenes Verfahren":       "Restricted Procedure",
    "Wettbewerblicher Dialog":      "Competitive Dialogue",
    "Innovationspartnerschaft":     "Innovation Partnership",
    "Wettbewerb":                   "Design Contest",
    "Vereinfachtes Verfahren":      "Simplified Procedure",
    "Direktauftrag":                "Direct Award",
    "VOB/A":                        "VOB/A (Construction)",
    "VOL/A":                        "VOL/A (Supplies/Services)",
    "VOF":                          "VOF (Freelance Services)",
    "VgV":                          "VgV (Procurement Regulation)",
    "UVgO":                         "UVgO (Below-threshold Regulation)",
    "SektVO":                       "SektVO (Utilities Sectors Regulation)",
    "VSVgV":                        "VSVgV (Defence Procurement)",
    "KonzVgV":                      "KonzVgV (Concessions)",
    "Ja": "Yes", "Nein": "No", "ja": "Yes", "nein": "No",
    "elektronisch in Textform":     "Electronic (text form)",
    "elektronisch":                 "Electronic",
    "schriftlich":                  "Written",
    "in Papierform":                "Paper form",
    "Offen":   "Open",
    "Geschlossen": "Closed",
    "Vergeben":    "Awarded",
    "Widerrufen":  "Revoked",
    "Eingestellt": "Suspended",
}

# ══════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS  (unchanged from original)
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
    raw = re.sub(r"\s*\d+\s+days? to go.*",        "", raw, flags=re.I).strip()
    raw = re.sub(r"\s*\d+\s+Tag(e)? verbleibend.*", "", raw, flags=re.I).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%b %d, %Y, %I:%M %p", "%Y-%m-%d"):
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
        translated = GoogleTranslator(source="de", target="en").translate(joined)
        parts      = [p.strip() for p in translated.split("|||")]
        result     = list(texts)
        for idx, part in zip(indices, parts):
            result[idx] = part
        return result
    except Exception as e:
        logger.warning(f"Batch translation failed: {e}")
        return texts


# ══════════════════════════════════════════════════════════════════
# HTML PARSER  (shared between Playwright-captured HTML and detail pages)
# ══════════════════════════════════════════════════════════════════

def parse_publication_date(li: BeautifulSoup) -> str | None:
    """
    Extract publication date from the <div class="item-right meta"> block.

    The DOM structure (confirmed from browser DevTools) is:
        <div class="item-right meta">
            <div class="date">25</div>
            <div class="month">June 2026</div>
            <div class="datestring">Publication date</div>
        </div>

    We reconstruct "25 June 2026" and return it as a raw string so that
    parse_date() can turn it into a proper datetime later.
    """
    meta_div = li.find("div", class_=lambda c: c and "item-right" in c.split() and "meta" in c.split())
    if not meta_div:
        for div in li.find_all("div"):
            ds = div.find("div", class_="datestring")
            if ds and re.search(r"publication", ds.get_text(), re.I):
                meta_div = div
                break

    if not meta_div:
        return None

    day_tag   = meta_div.find("div", class_="date")
    month_tag = meta_div.find("div", class_="month")

    day   = clean_text(day_tag.get_text())   if day_tag   else None
    month = clean_text(month_tag.get_text()) if month_tag else None

    if day and month and re.match(r"^\d{1,2}$", day) and re.search(r"\d{4}", month):
        return f"{day} {month}"

    return None

    day_tag   = meta_div.find("div", class_="date")
    month_tag = meta_div.find("div", class_="month")

    day   = clean_text(day_tag.get_text())   if day_tag   else None
    month = clean_text(month_tag.get_text()) if month_tag else None

    if day and month:
        return f"{day} {month}"   # e.g. "25 June 2026"

    # Last-resort: grab all visible text from the meta block, strip the label
    raw = clean_text(meta_div.get_text(" ", strip=True))
    if raw:
        cleaned = re.sub(r"(?i)publication\s*date", "", raw).strip(" ,")
        return cleaned or None

    return None


def parse_stream_html(html: str) -> list[dict]:
    """
    Parse tender cards from either:
      - the full listing page HTML  (<ul class="stream"> containing <li> items)
      - a raw HTML fragment of <li> items returned by the AJAX endpoint
    """
    soup  = BeautifulSoup(html, "html.parser")
    items = []

    stream = soup.find("ul", class_="stream")
    lis    = stream.find_all("li", recursive=False) if stream else soup.find_all("li")

    for li in lis:
        link = li.find("a", href=re.compile(r"/public/publications/\d+"))
        if not link:
            continue
        href      = link["href"]
        tender_id = re.search(r"/public/publications/(\d+)", href)
        tender_id = tender_id.group(1) if tender_id else None
        title_raw = clean_text(link.get_text())

        platform = None
        img = li.find("img")
        if img:
            platform = clean_text(img.get("alt") or img.get("title"))

        li_text      = li.get_text(" ", strip=True)
        deadline_raw = None
        m = re.search(
            r"\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},?\s+[\d:]+\s*[AP]M"
            r"|\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{2}:\d{2})?)\b",
            li_text,
        )
        if m:
            deadline_raw = m.group(1)

        # ── Publication date from the blue "item-right meta" tile ──
        publication_date_raw = parse_publication_date(li)

        items.append({
            "tender_id":            tender_id,
            "title_raw":            title_raw,
            "detail_url":           urljoin(BASE_URL, href),
            "platform":             platform,
            "deadline_raw":         deadline_raw,
            "publication_date_raw": publication_date_raw,
        })

    return items


# ══════════════════════════════════════════════════════════════════
# PLAYWRIGHT SCROLL COLLECTOR
# ══════════════════════════════════════════════════════════════════

async def collect_all_ids_playwright(headless: bool = True) -> list[dict]:
    """
    Opens the listing page in a Playwright browser, intercepts every
    /public/nextPublications AJAX response, and scrolls until all
    tenders have loaded.

    Returns a list of dicts: {tender_id, title_raw, detail_url, platform, deadline_raw}
    """
    if not HAVE_PLAYWRIGHT:
        raise RuntimeError("Playwright is not installed.")

    all_items: list[dict] = []
    seen_ids:  set        = set()
    ajax_queue: asyncio.Queue = asyncio.Queue()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            viewport={"width": 1280, "height": 900},
        )

        # Accept cookie consent via header so no popup blocks scrolling
        await context.add_cookies([{
            "name":   "CookieConsent",
            "value":  '{"necessary":true,"preferences":false,"statistics":false,"marketing":false}',
            "domain": "meinauftrag.rib.de",
            "path":   "/",
        }])

        page: Page = await context.new_page()

        # ── Intercept AJAX responses ──────────────────────────────
        async def on_response(response: Response):
            if "nextPublications" in response.url:
                try:
                    body = await response.json()
                    await ajax_queue.put(body)
                    logger.debug(f"Intercepted nextPublications → queued payload")
                except Exception as e:
                    logger.warning(f"Could not parse nextPublications response: {e}")

        page.on("response", on_response)

        # ── Navigate to listing ───────────────────────────────────
        logger.info(f"Opening {LISTING_URL} …")
        await page.goto(LISTING_URL, wait_until="networkidle", timeout=60_000)

        # Save debug snapshot
        with open("debug_listing_playwright.html", "w", encoding="utf-8") as f:
            f.write(await page.content())
        logger.info("Saved debug_listing_playwright.html")

        # ── Seed from initial page HTML ───────────────────────────
        initial_html = await page.content()
        for item in parse_stream_html(initial_html):
            if item["tender_id"] and item["tender_id"] not in seen_ids:
                seen_ids.add(item["tender_id"])
                all_items.append(item)
        logger.info(f"Initial page: {len(all_items)} tenders seeded")

        # ── Read totalEntries from page JS ────────────────────────
        total_entries = await page.evaluate(
            "() => typeof totalEntries !== 'undefined' ? totalEntries : 9999"
        )
        logger.info(f"totalEntries = {total_entries}")

        # ── Scroll loop ───────────────────────────────────────────
        stall_count = 0
        MAX_STALLS  = 5   # stop if no new items after this many scrolls

        while len(all_items) < total_entries and stall_count < MAX_STALLS:
            prev_count = len(all_items)

            # Scroll to the very bottom to trigger infinite-scroll JS
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            logger.debug("Scrolled to bottom — waiting for AJAX …")

            # Wait up to 8 s for a new AJAX payload to arrive
            try:
                payload = await asyncio.wait_for(ajax_queue.get(), timeout=8.0)
            except asyncio.TimeoutError:
                # No payload arrived — check if spinner is still visible
                spinner_visible = await page.is_visible("#spinner")
                if spinner_visible:
                    logger.debug("Spinner still visible, waiting more …")
                    await page.wait_for_timeout(2000)
                    continue
                else:
                    stall_count += 1
                    logger.info(
                        f"No AJAX response (stall {stall_count}/{MAX_STALLS}) "
                        f"— {len(all_items)}/{total_entries} collected"
                    )
                    await page.wait_for_timeout(1000)
                    continue

            # Parse the items HTML fragment from the JSON payload
            raw_html = payload.get("items") or payload.get("html") or ""
            if not raw_html.strip():
                stall_count += 1
                logger.info(f"Empty AJAX payload (stall {stall_count}/{MAX_STALLS})")
                continue

            new_count = 0
            for item in parse_stream_html(raw_html):
                if item["tender_id"] and item["tender_id"] not in seen_ids:
                    seen_ids.add(item["tender_id"])
                    all_items.append(item)
                    new_count += 1

            if new_count == 0:
                stall_count += 1
            else:
                stall_count = 0  # reset stall counter on progress

            logger.info(
                f"Scroll → +{new_count} new | "
                f"total={len(all_items)}/{total_entries} | "
                f"stalls={stall_count}"
            )

            # Small pause to mimic human scrolling behaviour
            await page.wait_for_timeout(random.randint(600, 1200))

        await browser.close()

    logger.info(f"Playwright collection complete: {len(all_items)} unique tenders")
    return all_items


# ══════════════════════════════════════════════════════════════════
# DETAIL PAGE PARSER  (unchanged from original)
# ══════════════════════════════════════════════════════════════════

class DetailParser:
    """Stateless helper — parses a single detail page soup into a raw dict."""

    def section_lines(self, soup: BeautifulSoup, heading_pattern: str) -> list[str]:
        from bs4 import NavigableString
        for tag in soup.find_all(["h3", "h4", "h5", "h6", "strong", "b"]):
            if re.search(heading_pattern, tag.get_text(), re.I):
                lines, collecting = [], False
                parent = tag.parent
                for child in parent.children:
                    if child == tag:
                        collecting = True
                        continue
                    if not collecting:
                        continue
                    if hasattr(child, "name") and child.name in ("h3","h4","h5","h6","hr"):
                        break
                    if hasattr(child, "name") and child.name in ("script","style"):
                        continue
                    if isinstance(child, NavigableString):
                        t = str(child).strip()
                        if t:
                            lines.append(t)
                    elif hasattr(child, "get_text"):
                        for br in child.find_all("br"):
                            br.replace_with("\n")
                        t = child.get_text("\n", strip=True)
                        lines.extend(l for l in t.split("\n") if l.strip())
                return lines
        return []

    def extract_labeled(self, lines: list[str], label_patterns: dict) -> dict:
        result   = {k: None for k in label_patterns}
        compiled = {
            k: re.compile(rf"^\s*(?:{p})\s*:?\s*$", re.I)
            for k, p in label_patterns.items()
        }
        n, i = len(lines), 0
        while i < n:
            matched_key = next(
                (k for k, rx in compiled.items() if rx.match(lines[i])), None
            )
            if matched_key:
                j, value_lines = i + 1, []
                while j < n and not any(rx.match(lines[j]) for rx in compiled.values()):
                    value_lines.append(lines[j])
                    j += 1
                value = clean_text(" ".join(value_lines))
                if value:
                    result[matched_key] = value
                i = j
            else:
                i += 1
        if not any(result.values()) and lines:
            flat       = " ".join(lines)
            all_labels = "|".join(f"(?:{p})" for p in label_patterns.values())
            for k, p in label_patterns.items():
                m = re.search(rf"(?:{p})\s*:?\s*(.*?)(?=(?:{all_labels})|$)", flat, re.I)
                if m:
                    val = clean_text(m.group(1))
                    if val:
                        result[k] = val
        return result

    def extract_js_address(self, soup: BeautifulSoup, heading_pattern: str) -> str | None:
        for tag in soup.find_all(["h3","h4","h5","h6","strong","b"]):
            if re.search(heading_pattern, tag.get_text(), re.I):
                for sib in tag.find_next_siblings():
                    if sib.name in ("h3","h4","h5","h6","hr"):
                        break
                    if sib.name == "script":
                        text = sib.string or sib.get_text()
                        m    = re.search(r'const\s+address\s*=\s*["\']([^"\']+)["\']', text)
                        if m:
                            return clean_text(m.group(1))
        return None

    def parse_js_documents(self, soup: BeautifulSoup) -> list[dict]:
        docs      = []
        var_names = (
            "documentsNotices", "documentsAttachments",
            "documentsApplicationForm", "documentsAttachmentsInfo",
        )

        def extract_link(value_html: str):
            m_a = re.search(r'href="([^"]+)"[^>]*>([^<]*)<', value_html)
            return (m_a.group(1), clean_text(m_a.group(2))) if m_a else None

        def walk(rows):
            for row in rows or []:
                data = row.get("data") or []
                if data and isinstance(data[0], dict):
                    value_html = data[0].get("value") or ""
                    link = extract_link(value_html)
                    if link:
                        url_raw, display_name = link
                        name = display_name or os.path.basename(urlparse(url_raw).path) or "document"
                        docs.append({
                            "name_de":   name,
                            "name_en":   None,
                            "file_url":  url_raw,
                            "file_name": name,
                            "type":      "Tender_document",
                            "s3_path":   None,
                            "uploaded_at": None,
                        })
                if row.get("rows"):
                    walk(row["rows"])

        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            if not any(name in text for name in var_names):
                continue
            for var_name in var_names:
                m = re.search(rf"var\s+{var_name}\s*=\s*(\[.*?\]|null)\s*;", text, re.S)
                if not m or m.group(1) == "null":
                    continue
                try:
                    walk(json.loads(m.group(1)))
                except json.JSONDecodeError as e:
                    logger.debug(f"Could not JSON-parse {var_name}: {e}")

        seen, unique = set(), []
        for d in docs:
            if d["file_url"] not in seen:
                seen.add(d["file_url"])
                unique.append(d)
        return unique

    def parse(self, soup: BeautifulSoup, detail_url: str, tender_id: str) -> dict:
        result: dict = {"detail_url": detail_url, "tender_id": tender_id}

        h4 = soup.find("h4")
        if h4:
            full  = clean_text(h4.get_text())
            m_num = re.match(r"^([\w\-\/]+(?:\d{4}[\w\-\/]*))\s+(.+)$", full or "")
            if m_num:
                result["tender_number"] = m_num.group(1)
                result["title_de"]      = m_num.group(2)
            else:
                result["tender_number"] = None
                result["title_de"]      = full
        else:
            result["tender_number"] = None
            result["title_de"]      = None

        action_tag = soup.find(
            lambda t: t.name in ("h4","h5","h6")
            and re.match(r"\s*Action\s*:", t.get_text(), re.I)
        )
        if not action_tag:
            action_tag = soup.find("h6")
        result["action_de"] = clean_text(
            re.sub(r"^Action\s*:\s*", "", clean_text(action_tag.get_text()) or "", flags=re.I)
        ) if action_tag else None

        desc_lines = self.section_lines(
            soup,
            r"Brief\s+Description|Kurze\s+Beschreibung|Stückliste|"
            r"Beschreibung|Leistungsbeschreibung|Aufgabenbeschreibung",
        )
        result["description_de"] = clean_text(" ".join(desc_lines)) or None

        date_lines  = self.section_lines(soup, r"Dates and deadlines|Termine|Fristen")
        date_values = self.extract_labeled(date_lines, {
            "period_raw":                    r"Period|Zeitraum",
            "deadline_raw":                  r"Expiration time|Angebotsfrist|Ablauf der (?:Angebots|Bewerbungs)frist",
            "opening_date_raw":              r"Opening Date|Eröffnungstermin",
            "award_period_raw":              r"Award period|Zuschlags-?\s*und Bindefrist|Bindefrist",
            "bidders_requests_deadline_raw": r"Bidders requests|Bieteranfragen",
        })
        result.update(date_values)

        # ── Publication date — detail page version ─────────────────
        # Strategy 1: look for a meta tile (same structure as the listing card)
       # ── Publication date — detail page version ─────────────────
        # Only use Strategy 3 (regex on raw text) — it's the only one
        # that cannot accidentally match the browser-warning banner,
        # because it requires the label word AND a date pattern together.
        pub_date_raw = None
        page_text = soup.get_text(" ")
        m_pub = re.search(
            r"(?:Publikationsdatum|Veröffentlichungsdatum)"
            r"[\s:–\-]*(\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+\w+\s+\d{4})",
            page_text, re.I,
        )
        if m_pub:
            val = clean_text(m_pub.group(1))
            if val and re.match(r"^\d", val):
                pub_date_raw = val

        result["publication_date_raw"] = pub_date_raw

        auth_lines = self.section_lines(soup, r"Contracting Authority|Auftraggeber|Vergabestelle")
        if auth_lines:
            result["authority_name_de"] = auth_lines[0]
            email, address_lines        = None, []
            for line in auth_lines[1:]:
                if re.match(r"^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$", line, re.I):
                    email = line
                else:
                    address_lines.append(line)
            result["authority_address_de"] = clean_text(" ".join(address_lines)) or None
            result["authority_email"]      = email
            addr_lines = [
                l for l in auth_lines
                if not re.match(r"^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$", l, re.I)
            ]
            result["place_of_performance_de"] = clean_text(", ".join(addr_lines)) or None
        else:
            result["authority_name_de"]       = None
            result["authority_address_de"]    = None
            result["authority_email"]         = None
            result["place_of_performance_de"] = self.extract_js_address(
                soup, r"Execution place|Ausführungsort|Erfüllungsort"
            )

        awarded_lines  = self.section_lines(soup, r"^Awarded$|Vergabe(?:verfahren)?")
        awarded_values = self.extract_labeled(awarded_lines, {
            "regulation_de":        r"Regulation|Regelwerk",
            "tender_procedure_de":  r"Tender Procedures?|Verfahrensart|Vergabeart",
            "subdivision_lots":     r"Subdivision into lots|Losaufteilung",
            "side_offers":          r"Side.?offers(?:\s+allowed)?|Nebenangebote",
            "multiple_main_offers": r"Several main offers(?:\s+allowed\.?)?|Mehrere Hauptangebote",
        })
        result.update(awarded_values)

        delivery_lines         = self.section_lines(soup, r"Delivery form|Lieferform|Einreichungsart")
        result["delivery_form_de"] = clean_text(" ".join(delivery_lines)) or None

        cpv_lines = self.section_lines(soup, r"CPV Codes|CPV")
        cpv_items = []
        for m in re.finditer(r"(\d{8}-\d)\s+(.+?)(?=\d{8}-\d|$)", " ".join(cpv_lines)):
            cpv_items.append({
                "cpv_code": m.group(1).strip(),
                "name_de":  clean_text(m.group(2)),
                "name_en":  None,
            })
        result["cpv_items"] = cpv_items

        documents, existing_urls = [], set()
        doc_heading = None
        for tag in soup.find_all(["h4","h5","h6","strong","b"]):
            if re.search(r"Documents|Dokumente|Unterlagen", tag.get_text(), re.I):
                doc_heading = tag
                break
        if doc_heading:
            for sib in doc_heading.find_next_siblings():
                if sib.name in ("h3","h4","h5","h6","hr"):
                    break
                for a in (sib.find_all("a") if hasattr(sib, "find_all") else []):
                    href_doc = a.get("href", "")
                    doc_name = clean_text(a.get_text())
                    if href_doc and doc_name:
                        full_url = href_doc if href_doc.startswith("http") else urljoin(BASE_URL, href_doc)
                        documents.append({
                            "name_de":   doc_name,
                            "name_en":   None,
                            "file_url":  full_url,
                            "file_name": doc_name,
                            "type":      "Tender_document",
                            "s3_path":   None,
                            "uploaded_at": None,
                        })
                        existing_urls.add(full_url)

        for d in self.parse_js_documents(soup):
            if d["file_url"] not in existing_urls:
                documents.append(d)
                existing_urls.add(d["file_url"])

        reg_link = soup.find("a", href=re.compile(r"/public/RegisterCompany/item/\d+"))
        if reg_link:
            reg_url = urljoin(BASE_URL, reg_link.get("href", ""))
            result["electronic_tender_url"] = reg_url
            if reg_url not in existing_urls:
                documents.append({
                    "name_de":   "Elektronische Vergabe – Jetzt registrieren",
                    "name_en":   "Electronic Tender – Register now",
                    "file_url":  reg_url,
                    "file_name": "electronic_tender_registration",
                    "type":      "Tender_Document",
                    "s3_path":   None,
                    "uploaded_at": None,
                })
        else:
            result["electronic_tender_url"] = None

        result["documents"] = documents
        return result


# ══════════════════════════════════════════════════════════════════
# MAIN SCRAPER  (MongoDB + S3 + requests for detail pages)
# ══════════════════════════════════════════════════════════════════

class RibPlaywrightScraper:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self):
        # HTTP session for detail-page fetching
        self.session = requests.Session()
        retry   = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update(self._HEADERS)

        self._detail_parser       = DetailParser()
        self._debug_detail_saved  = False

        # MongoDB
        mongo_uri  = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_uri)
        self.db     = self.client["tender_bharo"]
        self.col    = self.db["rib_meinauftrag_tenders"]
        self.meta   = self.db["meta_data"]
        self.col.create_index("hash_id",  unique=True)
        self.col.create_index("tender_id")
        self.col.create_index([("bid_status", 1), ("deadline", -1)])
        logger.info("MongoDB connected → tender_bharo.rib_meinauftrag_tenders")

        # S3
        self.bucket    = os.getenv("S3_BUCKET_NAME")
        self.s3_folder = "tender_documents/rib_meinauftrag"
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

        # Resume support
        self._scraped_ids: set = set()
        self._load_scraped_ids()

    # ── TEB ID ────────────────────────────────────────────────────

    def _teb_id(self) -> str:
        counter = self.meta.find_one_and_update(
            {"_id": "tb_global_id_rib_meinauftrag"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        m   = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{m[now.month]}/{seq:08d}"

    def _load_scraped_ids(self):
        try:
            ids = self.col.distinct("tender_id")
            self._scraped_ids = set(str(i) for i in ids)
            logger.info(f"Resume: {len(self._scraped_ids)} tenders already in DB")
        except Exception as e:
            logger.warning(f"Could not load scraped IDs: {e}")

    # ── Translation ───────────────────────────────────────────────

    def _translate_detail(self, raw: dict) -> dict:
        text_fields = [
            "title_de", "action_de", "description_de",
            "authority_name_de", "authority_address_de",
            "place_of_performance_de", "regulation_de",
            "tender_procedure_de", "delivery_form_de",
        ]
        values     = [raw.get(f) for f in text_fields]
        translated = translate_batch(values)
        d          = dict(raw)
        for field, trans in zip(text_fields, translated):
            en_key = field.replace("_de", "_en")
            static = tr(raw.get(field))
            d[en_key] = static if (static and static != raw.get(field)) else trans

        for bool_field in ("subdivision_lots", "side_offers", "multiple_main_offers"):
            d[bool_field] = tr(raw.get(bool_field)) or raw.get(bool_field)

        cpv_names = [c.get("name_de") for c in raw.get("cpv_items", [])]
        for item, name_en in zip(d.get("cpv_items", []), translate_batch(cpv_names)):
            item["name_en"] = name_en

        doc_names = [doc.get("name_de") for doc in raw.get("documents", [])]
        for doc, name_en in zip(d.get("documents", []), translate_batch(doc_names)):
            doc["name_en"] = name_en

        return d

    # ── Bid status ────────────────────────────────────────────────

    def _bid_status(self, detail: dict, listing: dict) -> str:
        deadline_raw = detail.get("deadline_raw") or listing.get("deadline_raw")
        deadline_dt  = parse_date(deadline_raw, "deadline")
        if deadline_dt:
            return "Open" if deadline_dt > datetime.now(timezone.utc) else "Closed"
        return "Open"

    # ── Build Mongo document ──────────────────────────────────────

    def _build_doc(self, listing: dict, detail: dict) -> dict:
        tender_id  = detail.get("tender_id") or listing.get("tender_id")
        detail_url = detail.get("detail_url", listing.get("detail_url", ""))

        def en(de_key: str) -> str | None:
            en_key = de_key.replace("_de", "_en")
            return detail.get(en_key) or detail.get(de_key)

        return {
            "hash_id":        generate_hash(tender_id or detail_url),
            "teb_number":     self._teb_id(),
            "tender_id":      tender_id,
            "tender_number":  detail.get("tender_number"),
            "bid_status":     self._bid_status(detail, listing),
            "source":         "RIB meinauftrag.rib.de",
            "source_url":     detail_url,
            "portal_url":     BASE_URL,
            "platform":       listing.get("platform"),
            "title":          en("title_de") or listing.get("title_raw"),
            "action":         en("action_de"),
            "description":    en("description_de"),
            "regulation":     en("regulation_de"),
            "tender_procedure":         en("tender_procedure_de"),
            "delivery_form":            en("delivery_form_de"),
            "subdivision_into_lots":    detail.get("subdivision_lots"),
            "side_offers_allowed":      detail.get("side_offers"),
            "multiple_main_offers":     detail.get("multiple_main_offers"),
            "period_raw":               detail.get("period_raw"),
            "deadline":                 parse_date(detail.get("deadline_raw"),     "deadline"),
            "deadline_raw":             detail.get("deadline_raw") or listing.get("deadline_raw"),
            "opening_date":             parse_date(detail.get("opening_date_raw"), "opening_date"),
            "opening_date_raw":         detail.get("opening_date_raw"),
            "award_period":             parse_date(detail.get("award_period_raw"), "award_period"),
            "award_period_raw":         detail.get("award_period_raw"),
            "bidders_requests_deadline":     parse_date(
                detail.get("bidders_requests_deadline_raw"), "bidders_requests"
            ),
            "bidders_requests_deadline_raw": detail.get("bidders_requests_deadline_raw"),
            # Publication date — prefer detail page value, fall back to listing card
            "publication_date_raw": (
                detail.get("publication_date_raw")
                or listing.get("publication_date_raw")
            ),
            "publication_date": parse_date(
                detail.get("publication_date_raw") or listing.get("publication_date_raw"),
                "publication_date",
            ),
            "authority_name":    en("authority_name_de"),
            "authority_address": en("authority_address_de"),
            "authority_email":   detail.get("authority_email"),
            "place_of_performance": en("place_of_performance_de"),
            "cpv_items": [
                {"cpv_code": i.get("cpv_code"), "name": i.get("name_en") or i.get("name_de")}
                for i in detail.get("cpv_items", [])
            ],
           "documents": [
                {
                    "name":         doc.get("name_en") or doc.get("name_de"),
                    "original_url": doc.get("file_url"),
                    "title":        doc.get("name_en") or doc.get("name_de") or doc.get("file_name"),
                    "type":         doc.get("type", "Tender_document"),
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
        folder  = f"{doc.get('tender_id', 'unknown')}_{mongo_id}"
        updated = []
        ext_map = {
            "application/pdf":   ".pdf",
            "application/zip":   ".zip",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        }
        for att in doc.get("documents", []):
            url = att.get("file_url")
            if not url:
                updated.append(att)
                continue
            try:
                r     = self.session.get(url, timeout=120)
                r.raise_for_status()
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

    # ── Process one tender ────────────────────────────────────────

    def _process_tender(self, listing: dict) -> bool:
        tender_id  = listing.get("tender_id")
        detail_url = listing.get("detail_url")

        if tender_id and tender_id in self._scraped_ids:
            logger.info(f"  ↷ skip (already in DB): {tender_id}")
            return False

        try:
            r    = self.session.get(detail_url, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")

            if not self._debug_detail_saved:
                with open("debug_detail.html", "w", encoding="utf-8") as f:
                    f.write(soup.prettify())
                logger.info("Saved debug_detail.html")
                self._debug_detail_saved = True

            raw    = self._detail_parser.parse(soup, detail_url, tender_id)
            detail = self._translate_detail(raw)
        except Exception as e:
            logger.error(f"  Detail failed [{detail_url}]: {e}")
            return False

        final_doc = self._build_doc(listing, detail)

        try:
            result = self.col.insert_one(final_doc)
            if tender_id:
                self._scraped_ids.add(tender_id)
            logger.info(
                f"  ✓ {tender_id} | {final_doc['teb_number']} "
                f"| {final_doc['bid_status']} "
                f"| {final_doc.get('title') or '(no title)'}"
            )
            if final_doc.get("documents") and self.s3:
                self._upload_docs_s3(final_doc, result.inserted_id)
        except DuplicateKeyError:
            self.col.update_one(
                {"hash_id": final_doc["hash_id"]},
                {"$set": {**final_doc, "updated_at": datetime.now(timezone.utc)}},
            )
            if tender_id:
                self._scraped_ids.add(tender_id)
            logger.info(f"  ↺ {tender_id} updated")

        time.sleep(random.uniform(0.8, 1.8))
        return True

    # ── Main entry point ──────────────────────────────────────────

    def scrape(self, headless: bool = True) -> None:
        logger.info("=" * 60)
        logger.info("Starting RIB meinauftrag Playwright scraper")
        logger.info(f"Source: {LISTING_URL}")
        logger.info("=" * 60)

        # Phase 1: collect all tender IDs via Playwright scroll
        all_items = asyncio.run(collect_all_ids_playwright(headless=headless))
        logger.info(f"Total tenders to process: {len(all_items)}")

        # Phase 2: fetch + parse each detail page with requests
        inserted_total = 0
        for idx, listing in enumerate(all_items, 1):
            logger.info(
                f"── [{idx}/{len(all_items)}] id={listing.get('tender_id')} "
                f"| {listing.get('title_raw', '?')[:60]}"
            )
            try:
                if self._process_tender(listing):
                    inserted_total += 1
            except Exception as e:
                logger.error(f"  Unhandled error [{listing.get('detail_url')}]: {e}")

        logger.info("=" * 60)
        logger.info(f"DONE. Inserted/updated this run: {inserted_total}")
        logger.info(f"Unique tenders in DB: {len(self._scraped_ids)}")
        logger.info("=" * 60)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RIB meinauftrag Playwright scraper")
    ap.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible (non-headless) mode — useful for debugging",
    )
    args = ap.parse_args()

    RibPlaywrightScraper().scrape(headless=not args.no_headless)