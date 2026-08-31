import os
import re
import time
import random
import hashlib
import logging
import tempfile
import requests

from datetime import datetime, timezone
from dateutil import parser as dateparser
from urllib.parse import urlparse

import boto3
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table, TableStyle, HRFlowable,
)
from reportlab.lib.enums import TA_CENTER

load_dotenv()


class Scraper:

    BASE_URL     = "https://contracts.ocp.dc.gov"
    SEARCH_API   = f"{BASE_URL}/api/contracts/search"
    DETAIL_API   = f"{BASE_URL}/api/contracts/details"
    DOC_DOWNLOAD = f"{BASE_URL}/api/filedownload"

    # Fiscal years to scrape: 2022 → 2027 (fixed range)
    # Override via CLI: --years 2024 2025 2026
    FISCAL_YEARS = list(range(2022, 2028))   # [2022, 2023, 2024, 2025, 2026, 2027]

    # ─────────────────────────────────────────────────────────────────────
    # INIT
    # ─────────────────────────────────────────────────────────────────────
    def __init__(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("DC_OCP_CONTRACTS")

        # ── Requests session with retry ──────────────────────────────────
        self.SESSION = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 502, 503, 504])
        self.SESSION.mount("https://", HTTPAdapter(max_retries=retries))
        self.SESSION.headers.update({
            "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/149.0.0.0 Safari/537.36",
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "en-US,en;q=0.9",
            "Accept-Encoding":    "gzip, deflate, br, zstd",
            "Content-Type":       "application/json",
            "Origin":             self.BASE_URL,
            "Referer":            f"{self.BASE_URL}/contracts/results",
            "sec-ch-ua":          '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest":     "empty",
            "sec-fetch-mode":     "cors",
            "sec-fetch-site":     "same-origin",
        })

        # Fiscal years — starts as class constant, can be overridden via CLI
        self.fiscal_years = list(self.FISCAL_YEARS)

        # ── Playwright warm-up (CF clearance) ────────────────────────────
        self._warm_up_with_playwright()

        # ── MongoDB ──────────────────────────────────────────────────────
        mongo_uri            = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client          = MongoClient(mongo_uri)
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["dc_ocp_awards"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)
        self.raw_collection.create_index("contract_number")

        # ── S3 ───────────────────────────────────────────────────────────
        self.bucket      = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/dc_ocp_contracts"
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name           = os.getenv("AWS_REGION"),
        )

    # ─────────────────────────────────────────────────────────────────────
    # PLAYWRIGHT WARM-UP
    # ─────────────────────────────────────────────────────────────────────
    def _warm_up_with_playwright(self):
        self.logger.info("Warming up session via Playwright (headless Chrome)…")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/149.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                    timezone_id="America/New_York",
                )
                page = context.new_page()

                self.logger.info("  → [1/2] BASE URL (CF clearance)…")
                page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(4)

                self.logger.info("  → [2/2] /contracts/search…")
                page.goto(
                    f"{self.BASE_URL}/contracts/search",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                time.sleep(5)

                browser_cookies = context.cookies()
                browser.close()

            for c in browser_cookies:
                self.SESSION.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"),
                )

            names = [c["name"] for c in browser_cookies]
            cf_ok = any(c["name"] == "cf_clearance" for c in browser_cookies)
            self.logger.info(f"  → Cookies acquired: {names}")
            self.logger.info(f"  → cf_clearance: {'✓' if cf_ok else '⚠ NOT FOUND'}")
            self.logger.info("Warm-up complete ✓")

        except ImportError:
            self.logger.error(
                "playwright not installed — run:\n"
                "  pip install playwright && playwright install chromium"
            )
            raise
        except Exception as e:
            self.logger.error(f"Playwright warm-up failed: {e}")
            raise

    # ─────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────
    def generate_teb_id(self) -> str:
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_contracts_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1:"A", 2:"B", 3:"C", 4:"D", 5:"E", 6:"F",
            7:"G", 8:"H", 9:"I", 10:"J", 11:"K", 12:"L",
        }
        return f"TEBC/{now.year}/{month_map[now.month]}/{seq:08d}"

    def generate_hash(self, contract_id: str) -> str:
        return hashlib.md5(str(contract_id).encode()).hexdigest()

    def parse_date(self, date_val):
        try:
            if not date_val:
                return None
            dt = dateparser.parse(str(date_val).strip(), dayfirst=False)
            return dt.replace(tzinfo=timezone.utc) if dt else None
        except Exception as e:
            self.logger.error(f"Date parse failed: {date_val} | {e}")
            return None

    def parse_amount(self, amount_val):
        try:
            if not amount_val:
                return None
            cleaned = re.sub(r'[^\d.]', '', str(amount_val))
            return int(float(cleaned)) if cleaned else None
        except Exception as e:
            self.logger.error(f"Amount parse failed: {amount_val} | {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # TENDER NOTICE PDF
    # ─────────────────────────────────────────────────────────────────────
    def generate_tender_notice_pdf(self, data: dict) -> str | None:
        """
        Build a tender-notice PDF from a DC OCP contract payload.
        Returns the path to a temporary PDF file, or None on failure.
        The caller must delete the temp file after upload.
        """
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name

            doc = SimpleDocTemplate(
                tmp_path,
                pagesize=A4,
                leftMargin=20 * mm,
                rightMargin=20 * mm,
                topMargin=20 * mm,
                bottomMargin=20 * mm,
            )

            styles = getSampleStyleSheet()

            # ── Custom paragraph styles ──────────────────────────────────
            style_title = ParagraphStyle(
                "NoticeTitle",
                parent=styles["Title"],
                fontSize=16,
                textColor=colors.HexColor("#1a3c6e"),
                spaceAfter=6,
                alignment=TA_CENTER,
            )
            style_subtitle = ParagraphStyle(
                "NoticeSubtitle",
                parent=styles["Normal"],
                fontSize=10,
                textColor=colors.HexColor("#555555"),
                spaceAfter=4,
                alignment=TA_CENTER,
            )
            style_section = ParagraphStyle(
                "SectionHeader",
                parent=styles["Heading2"],
                fontSize=11,
                textColor=colors.HexColor("#1a3c6e"),
                spaceBefore=10,
                spaceAfter=4,
            )
            style_body = ParagraphStyle(
                "Body",
                parent=styles["Normal"],
                fontSize=9,
                leading=14,
                spaceAfter=4,
            )
            style_label = ParagraphStyle(
                "Label",
                parent=styles["Normal"],
                fontSize=9,
                textColor=colors.HexColor("#333333"),
                fontName="Helvetica-Bold",
            )
            style_value = ParagraphStyle(
                "Value",
                parent=styles["Normal"],
                fontSize=9,
                textColor=colors.HexColor("#111111"),
            )

            # ── Helpers ──────────────────────────────────────────────────
            def row(label: str, value) -> list:
                return [
                    Paragraph(label, style_label),
                    Paragraph(str(value) if value else "N/A", style_value),
                ]

            def make_table(rows: list) -> Table:
                tbl = Table(rows, colWidths=[55 * mm, 115 * mm])
                tbl.setStyle(TableStyle([
                    ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#eef2f7")),
                    ("VALIGN",         (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1),
                        [colors.HexColor("#f7f9fc"), colors.white]),
                    ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                    ("LEFTPADDING",    (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
                    ("TOPPADDING",     (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
                ]))
                return tbl

            def fmt_date(field_name: str) -> str:
                """Use raw string first; fall back to ISO if datetime object."""
                raw = data.get(f"{field_name}_raw")
                if raw:
                    return str(raw)
                dt = data.get(field_name)
                if dt and hasattr(dt, "strftime"):
                    return dt.strftime("%Y-%m-%d")
                return str(dt) if dt else ""

            story = []

            # ── Header ───────────────────────────────────────────────────
            story.append(Paragraph("CONTRACT AWARD NOTICE", style_title))
            story.append(Paragraph(
                "DC Office of Contracting and Procurement (OCP)",
                style_subtitle,
            ))
            story.append(HRFlowable(
                width="100%", thickness=2, color=colors.HexColor("#1a3c6e"),
            ))
            story.append(Spacer(1, 6 * mm))

            # ── Contract Details ─────────────────────────────────────────
            story.append(Paragraph("Contract Details", style_section))
            story.append(HRFlowable(
                width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
            ))
            story.append(Spacer(1, 2 * mm))

            details_rows = [
                row("TEB Number",        data.get("teb_number")),
                row("Contract Number",   data.get("contract_number")),
                row("Contract ID",       data.get("contract_id")),
                row("Fiscal Year",       data.get("fiscal_year")),
                row("Title",             data.get("title")),
                row("Vendor",            data.get("vendor")),
                row("Contract Amount",   data.get("contract_amount")),
                row("Contract Type",     data.get("contract_type")),
                row("Market Type",       data.get("market_type")),
                row("Option Period",     data.get("current_option_period")),
                row("Agency / Agencies", data.get("agency_names")),
                row("Award Date",        fmt_date("award_date")),
                row("Start Date",        fmt_date("start_date")),
                row("End Date",          fmt_date("end_date")),
                row("Record Type",       data.get("record_type")),
                row("Source",            data.get("source")),
            ]
            story.append(make_table(details_rows))
            story.append(Spacer(1, 6 * mm))

            # ── Contracting Contacts ─────────────────────────────────────
            co = data.get("contracting_officer")
            cs = data.get("contracting_specialist")
            if co or cs:
                story.append(Paragraph("Contracting Contacts", style_section))
                story.append(HRFlowable(
                    width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
                ))
                story.append(Spacer(1, 2 * mm))
                contact_rows = []
                if co:
                    contact_rows.append(row("Contracting Officer",    co))
                if cs:
                    contact_rows.append(row("Contracting Specialist", cs))
                story.append(make_table(contact_rows))
                story.append(Spacer(1, 6 * mm))

            # ── Vendor Address ───────────────────────────────────────────
            v_parts = list(filter(None, [
                data.get("vendor_street"),
                data.get("vendor_city"),
                data.get("vendor_state"),
                data.get("vendor_zip"),
            ]))
            if v_parts:
                story.append(Paragraph("Vendor Address", style_section))
                story.append(HRFlowable(
                    width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
                ))
                story.append(Spacer(1, 2 * mm))
                story.append(make_table([row("Address", ", ".join(v_parts))]))
                story.append(Spacer(1, 6 * mm))

            # ── Commodity Codes ──────────────────────────────────────────
            commodity_list = (
                data.get("commodity_codes_expanded")
                or data.get("commodity_codes")
                or []
            )
            if commodity_list:
                story.append(Paragraph("Commodity Codes", style_section))
                story.append(HRFlowable(
                    width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
                ))
                story.append(Spacer(1, 2 * mm))
                for code in commodity_list:
                    story.append(Paragraph(f"• {code}", style_body))
                story.append(Spacer(1, 4 * mm))

            # ── Attached Documents ───────────────────────────────────────
            documents = data.get("documents") or []
            if documents:
                story.append(Paragraph("Attached Documents", style_section))
                story.append(HRFlowable(
                    width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
                ))
                story.append(Spacer(1, 2 * mm))
                for d in documents:
                    title = d.get("title") or d.get("type") or "Document"
                    url   = d.get("original_url") or d.get("s3_path") or ""
                    story.append(Paragraph(f"• {title}", style_body))
                    if url:
                        story.append(Paragraph(
                            f'<font size="8" color="#555555">{url}</font>',
                            style_body,
                        ))
                story.append(Spacer(1, 4 * mm))

            # ── Source URL ───────────────────────────────────────────────
            detail_url = data.get("detail_url")
            if detail_url:
                story.append(Paragraph("Source URL", style_section))
                story.append(HRFlowable(
                    width="100%", thickness=0.5, color=colors.HexColor("#cccccc"),
                ))
                story.append(Spacer(1, 2 * mm))
                story.append(Paragraph(detail_url, style_body))
                story.append(Spacer(1, 4 * mm))

            # ── Footer ───────────────────────────────────────────────────
            story.append(HRFlowable(
                width="100%", thickness=1, color=colors.HexColor("#1a3c6e"),
            ))
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(
                f"Generated by TenderBharo | "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                style_subtitle,
            ))

            doc.build(story)
            self.logger.info(f"Tender notice PDF generated: {tmp_path}")
            return tmp_path

        except Exception as e:
            self.logger.error(f"Tender notice PDF generation failed: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None

    # ─────────────────────────────────────────────────────────────────────
    # TENDER NOTICE — S3 UPLOAD
    # ─────────────────────────────────────────────────────────────────────
    def upload_tender_notice(self, data: dict, mongo_id, teb_number: str) -> str | None:
        """
        Generate a tender-notice PDF, upload it to S3, clean up the temp
        file, and return the s3_path (or None on failure).
        """
        tmp_path = None
        try:
            tmp_path = self.generate_tender_notice_pdf(data)
            if not tmp_path:
                return None

            folder    = teb_number.replace("/", "_")
            file_name = "tender_notice.pdf"
            key       = f"{self.base_folder}/{folder}/{file_name}"

            self.s3.upload_file(
                tmp_path,
                self.bucket,
                key,
                ExtraArgs={"ContentType": "application/pdf"},
            )

            s3_path = f"s3://{self.bucket}/{key}"
            self.logger.info(f"Tender notice uploaded: {key}")
            return s3_path

        except Exception as e:
            self.logger.error(f"Tender notice S3 upload failed: {e}")
            return None

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
                self.logger.debug(f"Temp file deleted: {tmp_path}")

    # ─────────────────────────────────────────────────────────────────────
    # SEARCH API  —  POST /api/contracts/search
    # ─────────────────────────────────────────────────────────────────────
    def fetch_search_page(self, fiscal_years: list, page: int = 1, size: int = 100) -> dict:
        payload = {
            "FilterBy": [
                {
                    "id":    0,
                    "name":  "FiscalYear",
                    "value": fiscal_years,
                }
            ],
            "OrderBy": [],
            "Page":    page,
            "Size":    size,
        }
        self.logger.debug(f"POST {self.SEARCH_API} | years={fiscal_years} | page={page}")
        resp = self.SESSION.post(self.SEARCH_API, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorMessage"):
            self.logger.warning(f"API error page {page}: {data['errorMessage']}")
        return data

    # ─────────────────────────────────────────────────────────────────────
    # DETAIL API  —  GET /api/contracts/details?id=<id>
    # ─────────────────────────────────────────────────────────────────────
    _detail_keys_logged = False

    def fetch_detail(self, contract_id: str) -> dict:
        try:
            resp = self.SESSION.get(
                self.DETAIL_API,
                params={"id": contract_id},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if not Scraper._detail_keys_logged:
                phone_keys = [
                    k for k in data.keys()
                    if "phone" in k.lower() or "tel" in k.lower() or "mobile" in k.lower()
                ]
                self.logger.info(f"  [detail keys — first call] all keys: {list(data.keys())}")
                self.logger.info(f"  [detail keys — phone-like] {phone_keys}")
                Scraper._detail_keys_logged = True

            return data
        except Exception as e:
            self.logger.error(f"Detail fetch failed for id={contract_id}: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────
    # DOCUMENT PARSING
    # ─────────────────────────────────────────────────────────────────────
    def _parse_documents(self, detail: dict) -> list:
        docs = []
        for doc in detail.get("documents", []):
            doc_id = doc.get("id", "")
            if not doc_id:
                continue
            download_url = f"{self.DOC_DOWNLOAD}?id={doc_id}"
            docs.append({
                "type":         "contract_document",
                "title":        doc.get("name", ""),
                "doc_id":       doc_id,
                "original_url": download_url,
                "s3_path":      None,
                "uploaded_at":  None,
            })
        return docs

    # ─────────────────────────────────────────────────────────────────────
    # BUILD MONGODB PAYLOAD
    # ─────────────────────────────────────────────────────────────────────
    def build_payload(
        self,
        row: dict,
        detail: dict,
        teb_no: str,
        hash_id: str,
        fiscal_year: int,
        documents: list,
    ) -> dict:
        contract_id     = row.get("id", "")
        contract_number = detail.get("contractNumber") or row.get("contractNumber", "")
        detail_url      = f"{self.BASE_URL}/contracts/details?id={contract_id}"

        commodity_codes_raw      = row.get("commodityCodes", [])
        commodity_codes_expanded = detail.get("commodityCodes", [])

        return {
            # ── Identity ────────────────────────────────────────────────
            "hash_id":           hash_id,
            "teb_number":        teb_no,
            "record_type":       "contract",
            "source":            "DC OCP Contracts",

            # ── Contract identifiers ────────────────────────────────────
            "contract_id":       contract_id,
            "contract_number":   contract_number,
            "fiscal_year":       fiscal_year,

            # ── Core fields ─────────────────────────────────────────────
            "title":             detail.get("title")              or row.get("title"),
            "vendor":            detail.get("vendor")             or row.get("vendor"),
            "contract_amount":          detail.get("contractAmount")     or row.get("contractAmount"),
            "contract_amount_original":  self.parse_amount(
                                             detail.get("contractAmount") or row.get("contractAmount")
                                         ),
            "contract_type":     detail.get("contractType"),
            "market_type":       detail.get("marketType")         or row.get("marketType"),
            "current_option_period": detail.get("currentOptionPeriod") or row.get("currentOptionPeriod"),

            # ── Agencies ────────────────────────────────────────────────
            "agency_names":      ", ".join(
                                     a for a in (detail.get("agencyNames") or row.get("agencyNames") or [])
                                     if a
                                 ) or None,

            # ── Dates ───────────────────────────────────────────────────
            "start_date":        self.parse_date(detail.get("startDate")  or row.get("startDate")),
            "end_date":          self.parse_date(detail.get("endDate")    or row.get("endDate")),
            "award_date":        self.parse_date(detail.get("awardDate")  or row.get("awardDate")),
            "start_date_raw":    detail.get("startDate")  or row.get("startDate"),
            "end_date_raw":      detail.get("endDate")    or row.get("endDate"),
            "award_date_raw":    detail.get("awardDate")  or row.get("awardDate"),

            # ── Commodity codes ─────────────────────────────────────────
            "commodity_codes":          commodity_codes_raw,
            "commodity_codes_expanded": commodity_codes_expanded,

            # ── Contracting contacts ────────────────────────────────────
            "contracting_officer":   ", ".join(filter(None, [
                                         detail.get("contractingOfficerName"),
                                         detail.get("contractingOfficerEmail"),
                                         detail.get("contractingOfficerPhone"),
                                     ])) or None,
            "contracting_specialist": ", ".join(filter(None, [
                                         detail.get("contractingSpecialistName"),
                                         detail.get("contractingSpecialistEmail"),
                                         detail.get("contractingSpecialistPhone"),
                                     ])) or None,

            # ── Vendor address ──────────────────────────────────────────
            "vendor_street":     detail.get("vendorStreet"),
            "vendor_city":       detail.get("vendorCity"),
            "vendor_state":      detail.get("vendorState"),
            "vendor_zip":        detail.get("vendorZip"),

            # ── Documents ───────────────────────────────────────────────
            "documents":         documents,

            # ── Tender notice ────────────────────────────────────────────
            "tender_notice_s3":  None,

            # ── URLs ────────────────────────────────────────────────────
            "detail_url":        detail_url,
            "source_url":        self.SEARCH_API,

            # ── ETL metadata ─────────────────────────────────────────────
            "etl_status":        "pending",
            "created_at":        datetime.now(timezone.utc),
            "updated_at":        datetime.now(timezone.utc),
        }

   
    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        folder       = teb_number.replace("/", "_")
        updated_docs = []

        for d in documents:
            url = d.get("original_url")
            if not url:
                updated_docs.append(d)
                continue
            try:
                response = self.SESSION.get(
                    url,
                    headers={"Referer": self.BASE_URL},
                    timeout=60,
                )
                if response.status_code != 200:
                    self.logger.warning(
                        f"  Doc download failed ({response.status_code}): {url}"
                    )
                    updated_docs.append(d)
                    continue

                title = d.get("title") or os.path.basename(urlparse(url).path) or "document"
                title = re.sub(r'[^\w\-. ]', '_', title)
                if not any(title.lower().endswith(e) for e in [
                    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.txt'
                ]):
                    ct = response.headers.get("Content-Type", "")
                    if "excel" in ct or "spreadsheet" in ct:
                        title += ".xlsx"
                    elif "word" in ct:
                        title += ".docx"
                    else:
                        title += ".pdf"

                ct_map = {
                    '.xlsx': 'application/vnd.ms-excel',
                    '.xls':  'application/vnd.ms-excel',
                    '.docx': 'application/msword',
                    '.doc':  'application/msword',
                    '.zip':  'application/zip',
                }
                content_type = next(
                    (v for k, v in ct_map.items() if title.lower().endswith(k)),
                    "application/pdf",
                )

                key = f"{self.base_folder}/{folder}/{title}"
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=response.content,
                    ContentType=content_type,
                )
                d["s3_path"]     = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"  ↑ S3: {key}")

            except Exception as e:
                self.logger.error(f"  S3 upload error: {e}")
                d["s3_path"] = d["uploaded_at"] = None

            updated_docs.append(d)

        # Persist updated doc list back to MongoDB
        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}},
        )
        return updated_docs

    def _retry_missing_uploads(self, hash_id: str, contract_id: str):
        existing = self.raw_collection.find_one({"hash_id": hash_id})
        if not existing:
            return
        docs    = existing.get("documents", [])
        pending = [d for d in docs if not d.get("s3_path") and d.get("original_url")]
        if pending:
            self.logger.info(f"  Retry {len(pending)} pending uploads for {contract_id}")
            self.upload_to_s3(pending, existing.get("teb_number", "UNKNOWN"), existing["_id"])
        else:
            self.logger.info(f"  {contract_id}: all docs already uploaded")

    
    def _process_row(self, row: dict, fiscal_year: int) -> bool:
        """
        Fetch detail, store to MongoDB, generate + upload tender notice PDF,
        then upload contract documents to S3.
        Returns True if a new record was inserted.
        """
        contract_id = row.get("id")
        if not contract_id:
            self.logger.warning("Row missing 'id' field, skipping.")
            return False

        hash_id = self.generate_hash(contract_id)

      
        if self.raw_collection.find_one({"hash_id": hash_id}):
            self.logger.info(
                f"  [dup] {row.get('contractNumber', contract_id)} — checking uploads"
            )
            self._retry_missing_uploads(hash_id, contract_id)
            return False

      
        contract_number = row.get("contractNumber", contract_id)
        self.logger.info(f"  [detail] {contract_number}")
        detail    = self.fetch_detail(contract_id)
        documents = self._parse_documents(detail)
        teb_no    = self.generate_teb_id()
        payload   = self.build_payload(row, detail, teb_no, hash_id, fiscal_year, documents)

     
        try:
            result = self.raw_collection.insert_one(payload)
            self.logger.info(f"  [stored] {contract_number}  TEB={teb_no}")
        except DuplicateKeyError:
            self.logger.info(f"  [dup-race] {contract_number}")
            self._retry_missing_uploads(hash_id, contract_id)
            return False

  
        notice_s3 = self.upload_tender_notice(payload, result.inserted_id, teb_no)
        if notice_s3:
            notice_doc = {
                "type":         "tender_notice",
                "title":        "Tender Notice",
                "original_url": payload["detail_url"],
                "s3_path":      notice_s3,
                "uploaded_at":  datetime.now(timezone.utc),
            }
            self.raw_collection.update_one(
                {"_id": result.inserted_id},
                {
                    "$set":  {"tender_notice_s3": notice_s3},
                    "$push": {"documents": notice_doc},
                },
            )
            self.logger.info(f"  [notice] uploaded for TEB={teb_no}")
        else:
            self.logger.warning(f"  [notice] PDF skipped for TEB={teb_no}")

        # ── Upload contract documents to S3 ──────────────────────────────
        if documents:
            self.upload_to_s3(documents, teb_no, result.inserted_id)

        return True

    
    def _scrape_fiscal_years(self, fiscal_years: list) -> int:
        page_num   = 1
        page_size  = 100
        total_new  = 0
        years_str  = ", ".join(str(y) for y in fiscal_years)

        while True:
            self.logger.info(f"[FY {years_str}] ── Page {page_num} ──────────────────────")

            try:
                data = self.fetch_search_page(fiscal_years, page=page_num, size=page_size)
            except Exception as e:
                self.logger.error(f"[FY {years_str}] Search failed page {page_num}: {e}")
                break

            rows = data.get("results") or data.get("Results") or []
            if not rows:
                self.logger.info(f"[FY {years_str}] No rows on page {page_num} — done.")
                break

            total_rows = data.get("totalCount") or data.get("total") or "?"
            self.logger.info(
                f"[FY {years_str}] Page {page_num}: {len(rows)} rows "
                f"(total reported: {total_rows})"
            )

            fy_label = fiscal_years[0] if len(fiscal_years) == 1 else "ALL"
            page_new = 0
            for row in rows:
                try:
                    if self._process_row(row, fy_label):
                        page_new  += 1
                        total_new += 1
                except Exception as e:
                    cid = row.get("contractNumber", row.get("id", "?"))
                    self.logger.error(f"[FY {years_str}] Row error {cid}: {e}")
                time.sleep(random.uniform(0.5, 1.2))

            self.logger.info(
                f"[FY {years_str}] Page {page_num} complete — "
                f"new: {page_new}/{len(rows)}  (running total: {total_new})"
            )

            if len(rows) < page_size:
                self.logger.info(f"[FY {years_str}] Last page reached.")
                break

            page_num += 1
            time.sleep(random.uniform(1.0, 2.0))

        return total_new

  
    def scrape(self, scrape_mode: str = "all_at_once"):
        """
        scrape_mode:
          "all_at_once"  — single API call with all fiscal years (recommended)
          "per_year"     — one API call per fiscal year (useful for targeted re-runs)
        """
        self.logger.info("=" * 60)
        self.logger.info("DC OCP Contracts Scraper starting")
        self.logger.info(
            f"Fiscal years: {self.fiscal_years}  "
            f"({min(self.fiscal_years)}–{max(self.fiscal_years)})"
        )
        self.logger.info(f"Mode: {scrape_mode}")
        self.logger.info("=" * 60)

        grand_total = 0

        if scrape_mode == "per_year":
            for fy in self.fiscal_years:
                self.logger.info(f"\n{'='*60}")
                self.logger.info(f"Scraping Fiscal Year {fy}")
                self.logger.info("=" * 60)
                inserted     = self._scrape_fiscal_years([fy])
                grand_total += inserted
                self.logger.info(f"[FY {fy}] Done. Inserted: {inserted}")
                time.sleep(random.uniform(2.0, 4.0))
        else:  # all_at_once
            grand_total = self._scrape_fiscal_years(self.fiscal_years)

        self.logger.info("=" * 60)
        self.logger.info(f"All done. Total new records inserted: {grand_total}")
        self.logger.info("=" * 60)



if __name__ == "__main__":
    Scraper().scrape()