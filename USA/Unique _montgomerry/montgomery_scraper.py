import os
import logging
import requests
import time
import random
import boto3
import re
import hashlib
import tempfile

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
from datetime import datetime, timezone
from pymongo import MongoClient, ReturnDocument
from requests.adapters import HTTPAdapter, Retry
from playwright.sync_api import sync_playwright
# FIND this line:
from datetime import datetime, timezone

# REPLACE with:
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

BASE_URL = "https://apps.montgomerycountymd.gov/prosolicitation/"
LIST_URL = f"{BASE_URL}SolicitationsAndBids.aspx?type=Formal"
DETAIL_URL = f"{BASE_URL}SolicitationsAndBidsDetails.aspx"
SOLDESC_URL = f"{BASE_URL}soldesc.asp"

DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rtf")

EXT_FROM_CONTENT_TYPE = {
    "pdf": ".pdf",
    "msword": ".doc",
    "vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "vnd.ms-excel": ".xls",
    "vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "zip": ".zip",
    "rtf": ".rtf",
}


class Scraper:

    def __init__(self):

        self.SESSION = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        self.SESSION.mount("https://", adapter)
        self.SESSION.mount("http://", adapter)
        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "upgrade-insecure-requests": "1",
        })

        # ---------------- DB ----------------
        self.client          = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["montgomery_county_md_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        # ---------------- S3 ----------------
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/montgomery_county_md"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION"),
        )

        # ---------------- LOG ----------------
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("MONTGOMERY_COUNTY_MD")

        # warm up the session so cf cookies / ASP.NET_SessionId are set
        self._warm_up()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _warm_up(self):
        try:
            self.SESSION.get("https://www.montgomerycountymd.gov/office-procurement/solicitations-contracts/formal-solicitations",
                              timeout=30)
            self.SESSION.get(LIST_URL, timeout=30, headers={"Referer": "https://www.montgomerycountymd.gov/"})
        except Exception as exc:
            self.logger.warning(f"Warm up failed (continuing anyway): {exc}")

    def generate_teb_number(self):
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1: "A", 2: "B", 3: "C", 4: "D",
            5: "E", 6: "F", 7: "G", 8: "H",
            9: "I", 10: "J", 11: "K", 12: "L",
        }
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    def generate_hash(self, key: str) -> str:
        return hashlib.md5(str(key).encode()).hexdigest()

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _fetch(self, url: str, stream: bool = False, referer: str = None):
        try:
            headers = {"Referer": referer} if referer else {}
            resp = self.SESSION.get(url, timeout=60, stream=stream,
                                     allow_redirects=True, headers=headers)
            if resp.status_code != 200:
                self.logger.warning(f"HTTP {resp.status_code}: {url}")
                return None
            return resp
        except Exception as exc:
            self.logger.error(f"Fetch failed {url}: {exc}")
            return None

    def _ext_from_response(self, url: str, content_type: str, content_disposition: str) -> str:
        if content_disposition:
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
            if m:
                ext = os.path.splitext(m.group(1))[1].lower()
                if ext:
                    return ext

        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in DOC_EXTENSIONS:
            return ext

        for key, ext in EXT_FROM_CONTENT_TYPE.items():
            if key in content_type:
                return ext

        return ".pdf"

    # ------------------------------------------------------------------ #
    # Listing page
    # ------------------------------------------------------------------ #

    def fetch_listing(self) -> list:
        resp = self._fetch(LIST_URL, referer="https://www.montgomerycountymd.gov/")
        if not resp:
            return []
        return self._parse_listing(resp.text)

    def _parse_listing(self, html: str) -> list:
        soup  = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="myTable")
        if not table:
            self.logger.error("Solicitations table not found.")
            return []

        rows   = []
        all_tr = table.find_all("tr")

        for tr in all_tr[1:]:  # skip header row
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue

            # --- column 0: solicitation number + detail link ---
            link_tag = cells[0].find("a", href=True)
            if not link_tag:
                continue

            href = link_tag["href"].strip()
            detail_url = urljoin(BASE_URL, href)
            qs = parse_qs(urlparse(detail_url).query)
            sol_id = qs.get("id", [None])[0]
            if not sol_id:
                continue

            solicitation_no = self.clean_text(link_tag.get_text())

            # --- column 1: description block ---
            description = self.clean_text(cells[1].get_text(" ", strip=True))
# --- column 2 (hidden td): scope text + solicitation doc link ---
            hidden_cell = cells[2]
            scope_summary = self.clean_text(hidden_cell.get_text(" ", strip=True))

            solicitation_doc_url = None
            for a in hidden_cell.find_all("a", href=True):
                if "soldesc.asp" in a["href"] or self.clean_text(a.get_text()).lower() == "download solicitation":
                    solicitation_doc_url = urljoin(BASE_URL, a["href"].strip())
                    break

            # --- last column: bid opening / closing date ---
            closing_raw = self.clean_text(cells[-1].get_text(" ", strip=True))

            rows.append({
                "solicitation_id":      sol_id,
                "solicitation_no":      solicitation_no,
                "description":          description,
                "scope_summary":        scope_summary,
                "closing_raw":          closing_raw,
                "detail_url":           detail_url,
                "solicitation_doc_url": solicitation_doc_url,
            })

        self.logger.info(f"Listing parsed: {len(rows)} rows")
        return rows

    # ------------------------------------------------------------------ #
    # Detail page
    # ------------------------------------------------------------------ #

    def fetch_detail(self, detail_url: str, sol_id: str) -> dict:
        resp = self._fetch(detail_url, referer=LIST_URL)
        if not resp:
            return {}
        return self._parse_detail(resp.text, sol_id)

    def _div_text(self, soup: BeautifulSoup, div_id: str) -> str:
        tag = soup.find(id=div_id)
        if not tag:
            return ""
        return self.clean_text(tag.get_text(" ", strip=True))

    def _div_html(self, soup: BeautifulSoup, div_id: str):
        return soup.find(id=div_id)

    def _parse_detail(self, html: str, sol_id: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        sol_number   = self._div_text(soup, "divCellFormalSolicitiationNumber")
        closing_date = self._div_text(soup, "divCellFormalBidOpeningClosingDate")
        title        = self._div_text(soup, "divCellFormalTitle")
        amendments   = self._div_text(soup, "divCellFormalAmmendments")
        pre_conf     = self._div_text(soup, "divCellFormalPresubmissionConference")
        contacts_raw = self._div_text(soup, "divCellFormalContacts")
        scope        = self._div_text(soup, "divCellFormalScope")

        # de-obfuscate cloudflare email protection if present
        contacts_div = self._div_html(soup, "divCellFormalContacts")
        contact_emails = []
        if contacts_div:
            for a in contacts_div.find_all("a", href=True):
                if "/cdn-cgi/l/email-protection#" in a["href"]:
                    cf_email_span = a.find("span", class_="__cf_email__")
                    if cf_email_span and cf_email_span.get("data-cfemail"):
                        decoded = self._decode_cf_email(cf_email_span["data-cfemail"])
                        if decoded:
                            contact_emails.append(decoded)

        # ---- find "Download Solicitation" link (this is the document we store) ----
        solicitation_doc_url = None
        for a in soup.find_all("a", href=True):
            label = self.clean_text(a.get_text())
            if label.lower() == "download solicitation":
                solicitation_doc_url = urljoin(BASE_URL, a["href"].strip())
                break

        # ---- "Link to Solicitation" (external bidnet link etc, informational only) ----
        external_link = None
        link_div = self._div_html(soup, "divCellFormalDownloadOption")
        if link_div:
            a = link_div.find("a", href=True)
            if a:
                external_link = a["href"].strip()
        if not external_link:
            # fallback: the plain (non-hidden) "Link to Solicitation" div right after it
            for tr in soup.find_all("tr"):
                td0 = tr.find("td")
                if td0 and "Link to Solicitation" in td0.get_text():
                    a = tr.find_all("td")[1].find("a", href=True) if len(tr.find_all("td")) > 1 else None
                    if a:
                        external_link = a["href"].strip()
                    break

        return {
            "solicitation_number": sol_number,
            "closing_date_raw":    closing_date,
            "title":               title,
            "amendments":          amendments,
            "pre_submission_conf": pre_conf,
            "contacts_text":       contacts_raw,
            "contact_emails":      contact_emails,
            "scope_text":          scope,
            "solicitation_doc_url": solicitation_doc_url,
            "external_link":       external_link,
            "detail_html":         html,
        }

    def _decode_cf_email(self, encoded: str) -> str:
        try:
            r = int(encoded[:2], 16)
            email = "".join(
                chr(int(encoded[i:i + 2], 16) ^ r)
                for i in range(2, len(encoded), 2)
            )
            return email
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # Notice PDF generation (Tanzania pattern: detail content -> A4 HTML -> PDF via CDP)
    # ------------------------------------------------------------------ #

    def _build_notice_html(self, listing_item: dict, detail: dict) -> str:
        title           = detail.get("title") or listing_item.get("description") or ""
        sol_number      = detail.get("solicitation_number") or listing_item.get("solicitation_no") or ""
        closing_date    = detail.get("closing_date_raw") or listing_item.get("closing_raw") or ""
        amendments      = detail.get("amendments") or ""
        pre_conf        = detail.get("pre_submission_conf") or ""
        contacts_text   = detail.get("contacts_text") or ""
        scope_text      = detail.get("scope_text") or listing_item.get("scope_summary") or ""
        external_link   = detail.get("external_link") or ""

        def row(label, value):
            if not value:
                return ""
            return f"""
            <tr>
                <td class="label">{label}</td>
                <td class="value">{value}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
    @page {{ size: A4; margin: 20mm 18mm; }}
    body {{
        font-family: Arial, Helvetica, sans-serif;
        color: #1a1a1a;
        font-size: 12px;
        line-height: 1.5;
    }}
    h1 {{
        font-size: 18px;
        color: #7B1113;
        border-bottom: 2px solid #7B1113;
        padding-bottom: 8px;
        margin-bottom: 4px;
    }}
    .subtitle {{
        font-size: 11px;
        color: #555;
        margin-bottom: 20px;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 16px;
    }}
    td {{
        padding: 8px 6px;
        border-bottom: 1px solid #ddd;
        vertical-align: top;
    }}
    td.label {{
        width: 180px;
        font-weight: bold;
        background-color: #eeeeee;
    }}
    td.value {{
        white-space: pre-wrap;
    }}
    .section-title {{
        font-size: 13px;
        font-weight: bold;
        color: #7B1113;
        margin-top: 20px;
        margin-bottom: 8px;
        border-bottom: 1px solid #ccc;
        padding-bottom: 4px;
    }}
    .footer {{
        margin-top: 30px;
        font-size: 9px;
        color: #888;
        border-top: 1px solid #ccc;
        padding-top: 8px;
    }}
</style>
</head>
<body>
    <h1>Montgomery County, Maryland &mdash; Formal Solicitation Notice</h1>
    <div class="subtitle">Office of Procurement</div>

    <table>
        {row("Solicitation Number", sol_number)}
        {row("Title", title)}
        {row("Bid Opening / RFP Closing", closing_date)}
        {row("Amendments", amendments)}
        {row("Pre-Submission Conference", pre_conf)}
    </table>

    <div class="section-title">Scope</div>
    <table>
        {row("Scope Summary", scope_text)}
    </table>

    <div class="section-title">Contacts</div>
    <table>
        {row("Contact Details", contacts_text)}
    </table>

    {f'<div class="section-title">Solicitation Link</div><table>{row("Link", external_link)}</table>' if external_link else ""}

    <div class="footer">
        Generated by TenderBharo &middot; Source: apps.montgomerycountymd.gov/prosolicitation
    </div>
</body>
</html>"""
        return html

    def generate_notice_pdf(self, listing_item: dict, detail: dict, out_path: str) -> bool:
        html_content = self._build_notice_html(listing_item, detail)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content(html_content, wait_until="load")
                page.pdf(
                    path=out_path,
                    format="A4",
                    print_background=True,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                )
                browser.close()
            return True
        except Exception as exc:
            self.logger.error(f"Notice PDF generation failed: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # S3 upload
    # ------------------------------------------------------------------ #

    def _upload_local_file_to_s3(self, local_path: str, folder: str, file_name: str) -> str | None:
        try:
            ext = os.path.splitext(local_path)[1].lower()
            content_type_map = {
                ".pdf":  "application/pdf",
                ".zip":  "application/zip",
                ".doc":  "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xls":  "application/vnd.ms-excel",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".rtf":  "application/rtf",
            }
            content_type = content_type_map.get(ext, "application/octet-stream")

            safe_name = re.sub(r"[^\w\-. ]", "_", file_name)
            if not safe_name.lower().endswith(ext):
                safe_name += ext
            key = f"{self.base_folder}/{folder}/{safe_name}"

            self.s3.upload_file(local_path, self.bucket, key,
                                 ExtraArgs={"ContentType": content_type})

            s3_path = f"s3://{self.bucket}/{key}"
            self.logger.info(f"Uploaded: {key}")
            return s3_path
        except Exception as exc:
            self.logger.error(f"Upload failed {local_path}: {exc}")
            return None

    def _upload_remote_file_to_s3(self, url: str, folder: str, file_name: str, referer: str = None) -> tuple[str | None, str]:
        """Downloads a remote document and uploads to S3. Returns (s3_path, ext)."""
        tmp_path = None
        try:
            resp = self._fetch(url, stream=True, referer=referer)
            if not resp:
                return None, ".pdf"

            ext = self._ext_from_response(resp.url, resp.headers.get("Content-Type", "").lower(),
                                           resp.headers.get("Content-Disposition", "").lower())

            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        tmp.write(chunk)
                tmp_path = tmp.name

            s3_path = self._upload_local_file_to_s3(tmp_path, folder, file_name)
            return s3_path, ext
        except Exception as exc:
            self.logger.error(f"Remote upload failed {url}: {exc}")
            return None, ".pdf"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def upload_documents(self, tender: dict, mongo_id) -> None:
        folder       = f"{tender['teb_number'].replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in tender.get("documents", []):
            if d.get("s3_path"):
                # already uploaded (e.g. notice pdf was uploaded inline at creation time)
                updated_docs.append(d)
                continue

            url = d.get("original_url")
            if not url:
                updated_docs.append(d)
                continue

            file_name = d.get("title") or "Tender_document"
            s3_path, _ext = self._upload_remote_file_to_s3(url, folder, file_name, referer=tender.get("source_url"))
            d["s3_path"]    = s3_path
            d["uploaded_at"] = datetime.now(timezone.utc) if s3_path else None
            updated_docs.append(d)

        self.raw_collection.update_one(
            {"_id": mongo_id}, {"$set": {"documents": updated_docs}}
        )

    # ------------------------------------------------------------------ #
    # Status helper
    # ------------------------------------------------------------------ #

    def _parse_status_and_date(self, closing_raw: str):
        closing_raw = (closing_raw or "").strip()

        if not closing_raw:
            return "unknown", None

        if "postponed" in closing_raw.lower():
            return "postponed", closing_raw

        # format example: "Jun 22 2026  2:00PM"
        try:
            cleaned = re.sub(r"\s+", " ", closing_raw).strip()
            dt = datetime.strptime(cleaned, "%b %d %Y %I:%M%p")
            status = "closed" if dt < datetime.now() else "open"
            return status, dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return "unknown", closing_raw
        # FIND this block (end of _parse_status_and_date):
        except ValueError:
            return "unknown", closing_raw

# ADD this new method RIGHT AFTER it:
    def _calculate_publication_date(self, closing_date_raw: str, days_before: int = 20) -> str | None:
        if not closing_date_raw:
            return None
        try:
            cleaned = re.sub(r"\s+", " ", closing_date_raw).strip()
            closing_dt = datetime.strptime(cleaned, "%b %d %Y %I:%M%p")
            pub_dt = closing_dt - timedelta(days=days_before)
            return pub_dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # Main scrape loop
    # ------------------------------------------------------------------ #

    def scrape(self):
        total = 0

        try:
            self.logger.info("Starting Montgomery County MD scraper")

            rows = self.fetch_listing()
            if not rows:
                self.logger.error("No rows from listing page. Aborting.")
                return

            self.logger.info(f"Processing {len(rows)} solicitations")

            for item in rows:
                sol_id = item["solicitation_id"]
                sol_no = item["solicitation_no"]
                detail_url = item["detail_url"]

                if not sol_id:
                    self.logger.warning(f"No solicitation id, skipping: {item}")
                    continue

                try:
                    hash_id = self.generate_hash(sol_id)

                    if self.raw_collection.find_one({"hash_id": hash_id}):
                        self.logger.info(f"Skipped duplicate: id={sol_id} ({sol_no})")
                        continue

                    self.logger.info(f"Fetching detail for id={sol_id} ({sol_no})")
                    detail = self.fetch_detail(detail_url, sol_id)
                    if not detail:
                        self.logger.warning(f"Could not fetch detail page for id={sol_id}, skipping")
                        continue

                    # FIND these two lines:
                    status, closing_parsed = self._parse_status_and_date(
                        detail.get("closing_date_raw") or item["closing_raw"]
                    )

# ADD one line directly below them:
                    status, closing_parsed = self._parse_status_and_date(
                        detail.get("closing_date_raw") or item["closing_raw"]
                    )
                    publication_date = self._calculate_publication_date(
                        detail.get("closing_date_raw") or item["closing_raw"]
                    )

                    teb_no = self.generate_teb_number()

                    documents = []

                    # ---- doc 1: solicitation document ("Download Solicitation" link) ----
                    sol_doc_url = detail.get("solicitation_doc_url") or item.get("solicitation_doc_url")
                    if sol_doc_url:
                        documents.append({
                            "type":         "Tender_document",
                            "title":        f"Solicitation_{sol_no.replace(' ', '_').replace('#', '')}",
                            "original_url": sol_doc_url,
                            "s3_path":      None,
                            "uploaded_at":  None,
                        })
                    else:
                        self.logger.warning(f"No 'Download Solicitation' link found for id={sol_id}")

                    tender = {
                        "hash_id":            hash_id,
                        "teb_number":         teb_no,
                        "solicitation_id":    sol_id,
                        "solicitation_no":    detail.get("solicitation_number") or sol_no,
                        "tender_subject":     detail.get("title") or item["description"],
                        "scope_summary":      detail.get("scope_text") or item["scope_summary"],
                        "amendments":         detail.get("amendments"),
                        "pre_submission_conf": detail.get("pre_submission_conf"),
                        "contacts_text":      detail.get("contacts_text"),
                        "contact_emails":     detail.get("contact_emails"),
                        "external_link":      detail.get("external_link"),
                        "bid_closing_raw":    detail.get("closing_date_raw") or item["closing_raw"],
                        "publication_date":   publication_date,
                        "bid_closing_date":   closing_parsed,
                        "status":             status,
                        "source_url":         detail_url,
                        "documents":          documents,
                        "etl_status":         "pending",
                        "created_at":         datetime.now(timezone.utc),
                    }

                    res      = self.raw_collection.insert_one(tender)
                    mongo_id = res.inserted_id

                    # ---- generate + upload notice pdf ----
                    notice_folder = f"{teb_no.replace('/', '_')}_{mongo_id}"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                        notice_path = tmp_pdf.name

                    notice_doc = None
                    if self.generate_notice_pdf(item, detail, notice_path):
                        notice_s3_path = self._upload_local_file_to_s3(
                            notice_path, notice_folder,
                            f"Notice_{sol_no.replace(' ', '_').replace('#', '')}"
                        )
                        if notice_s3_path:
                            notice_doc = {
                                "type":         "notice",
                                "title":        f"Notice_{sol_no.replace(' ', '_').replace('#', '')}.pdf",
                                "original_url": detail_url,
                                "s3_path":      notice_s3_path,
                                "uploaded_at":  datetime.now(timezone.utc),
                            }
                    if os.path.exists(notice_path):
                        os.unlink(notice_path)

                    if notice_doc:
                        self.raw_collection.update_one(
                            {"_id": mongo_id},
                            {"$push": {"documents": notice_doc}}
                        )

                    # ---- upload remaining (solicitation) documents ----
                    fresh_tender = self.raw_collection.find_one({"_id": mongo_id})
                    if fresh_tender and fresh_tender.get("documents"):
                        self.upload_documents(fresh_tender, mongo_id)

                    total += 1
                    self.logger.info(
                        f"Inserted [{teb_no}] id={sol_id} ({sol_no}): "
                        f"{(detail.get('title') or item['description'])[:70]}"
                    )

                    time.sleep(random.uniform(1.0, 2.0))

                except Exception as exc:
                    self.logger.error(f"Failed id={sol_id} ({sol_no}): {exc}")

            self.logger.info(f"Total scraped: {total}")

        except Exception as exc:
            self.logger.error(f"Scraper failed: {exc}")


if __name__ == "__main__":
    Scraper().scrape()