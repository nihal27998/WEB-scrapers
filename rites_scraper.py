import hashlib
import logging
import os
import random
import re
import ssl
import time

import boto3
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from html import unescape
from pymongo import MongoClient, ReturnDocument
from requests.adapters import HTTPAdapter, Retry
from urllib.parse import urljoin, urlparse, parse_qs
from urllib3.poolmanager import PoolManager
from datetime import datetime, timezone

# from dotenv import load_dotenv
# load_dotenv()

class Scraper:

    # ---------------- INIT ----------------
    def __init__(self):

        self.BASE_URL = "https://www.rites.com"
        self.START_URL = "https://www.rites.com/tender"
        self.API_URL = urljoin(self.BASE_URL, "/Home/GetTenderForPublic")

        self.SESSION = requests.Session()

        ctx = ssl.create_default_context()
        ctx.options |= 0x4

        retries = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )

        adapter = HTTPAdapter(max_retries=retries)

        adapter.poolmanager = PoolManager(
            num_pools=10,
            maxsize=10,
            block=False,
            ssl_context=ctx
        )

        self.SESSION.mount("https://", adapter)
        self.SESSION.mount("http://", adapter)

        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.START_URL
        })

        # ---------------- DB ----------------
        self.client = MongoClient(os.getenv("LOCAL_MONGO_URI"))

        self.db = self.client["tender_bharo"]

        self.raw_collection = self.db["rites_tenders"]
        self.meta_collection = self.db["meta_data"]

        self.raw_collection.create_index("hash_id", unique=True)

        # ---------------- S3 ----------------
        self.bucket = os.getenv("S3_BUCKET_NAME")

        self.base_folder = "tender_documents/rites"

        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION")
        )

        # ---------------- LOG ----------------
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s"
        )

        self.logger = logging.getLogger("RITES")

    # ---------------- TEB ID ----------------
    def generate_teb_number(self):

        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )

        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1: "A", 2: "B", 3: "C", 4: "D",
            5: "E", 6: "F", 7: "G", 8: "H",
            9: "I", 10: "J", 11: "K", 12: "L"
        }

        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # ---------------- HASH ----------------
    def generate_hash(self, tender_number, tender_reference_number, summary):

        raw = "|".join([
            self.clean_text(tender_number),
            self.clean_text(tender_reference_number),
            self.clean_text(summary)
        ])

        return hashlib.md5(raw.encode()).hexdigest()

    # ---------------- CLEAN TEXT ----------------
    def clean_text(self, text):

        if not text:
            return ""

        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ---------------- DATE ----------------
    def parse_date(self, value):

        value = self.clean_text(value)

        if not value:
            return ""

        try:
            return parser.parse(value, dayfirst=True)

        except Exception:
            return value

    # ---------------- AMOUNT ----------------
    def parse_amount(self, value):

        value = self.clean_text(value)

        if not value:
            return 0

        amount = re.sub(r"[^\d.]", "", value)

        if not amount:
            return 0

        try:
            return int(float(amount))

        except Exception:
            return 0

    # ---------------- DESCRIPTION SPLIT ----------------
    def split_description(self, description):

        description = self.clean_text(description)

        tender_reference_number = ""
        summary = description
        tender_id = ""

        ref_match = re.search(
            r"Tender\s*Ref\.?\s*No\s*:\s*(.*?)(?=\s{2,}|Tender\s+Id\s*:|$)",
            description,
            flags=re.IGNORECASE
        )

        if ref_match:
            after_ref = ref_match.group(1).strip()

            ref_patterns = [
                r"^(GEM/\d{4}/B/\d+)\s*(.*)$",
                r"^([A-Z0-9][A-Z0-9/_().& -]*?/\d{4})\s+(.*)$",
                r"^([A-Z0-9][A-Z0-9/_().& -]*?\d{4})\s+(.*)$"
            ]

            for pattern in ref_patterns:
                match = re.match(pattern, after_ref, flags=re.IGNORECASE)

                if match:
                    tender_reference_number = self.clean_text(match.group(1))
                    summary = self.clean_text(match.group(2))
                    break

            if not tender_reference_number:
                tender_reference_number = after_ref
                summary = ""

        tender_id_match = re.search(
            r"Tender\s+Id\s*:\s*([^\s]+)",
            description,
            flags=re.IGNORECASE
        )

        if tender_id_match:
            tender_id = self.clean_text(tender_id_match.group(1))
            summary = re.sub(
                r"Tender\s+Id\s*:\s*[^\s]+",
                "",
                summary,
                flags=re.IGNORECASE
            )

        summary = re.sub(
            r"^Tender\s*Ref\.?\s*No\s*:\s*",
            "",
            summary,
            flags=re.IGNORECASE
        )

        summary = self.clean_text(summary)

        return tender_reference_number, summary, tender_id

    # ---------------- S3 ----------------
    def upload_to_s3(self, doc, mongo_id):

        if not self.bucket:
            self.logger.warning("S3_BUCKET_NAME not configured, skipping upload")
            return

        folder = f"{doc['teb_number'].replace('/', '_')}_{mongo_id}"

        updated_docs = []

        for d in doc.get("documents", []):

            try:
                url = d.get("original_url")

                if not url:
                    updated_docs.append(d)
                    continue

                response = self.SESSION.get(url, timeout=60)

                if response.status_code != 200:
                    updated_docs.append(d)
                    continue

                title = d.get("title") or os.path.basename(urlparse(url).path)

                title = re.sub(r"[^\w\-. ]", "_", title)

                ext = os.path.splitext(urlparse(url).path)[1]

                if ext and not title.lower().endswith(ext.lower()):
                    title += ext

                key = f"{self.base_folder}/{folder}/{title}"

                content_type = "application/octet-stream"

                if ext.lower() == ".pdf":
                    content_type = "application/pdf"

                elif ext.lower() in [".xls", ".xlsx"]:
                    content_type = "application/vnd.ms-excel"

                elif ext.lower() == ".zip":
                    content_type = "application/zip"

                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=response.content,
                    ContentType=content_type
                )

                d["s3_path"] = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)

                self.logger.info(f"Uploaded: {key}")

            except Exception as e:

                self.logger.error(f"S3 upload failed: {e}")

                d["s3_path"] = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}}
        )

    # ---------------- EXTRACT DOCUMENTS ----------------
    def extract_documents_from_html(self, html):

        documents = []

        try:
            html = unescape(html or "")
            soup = BeautifulSoup(html, "lxml")
            current_title = ""

            for child in soup.select("div.row"):

                text = self.clean_text(child.get_text(" ", strip=True))

                link = child.select_one("a[href]")

                if link:
                    href = link.get("href", "").strip()

                    if not href:
                        continue

                    doc_url = urljoin(self.BASE_URL, href)
                    file_name = os.path.basename(urlparse(doc_url).path)

                    doc_type = current_title or "Tender Document"

                    documents.append({
                        "type": "tender document",
                        "title": self.clean_text(doc_type) or file_name ,
                        "original_url": doc_url,
                        "s3_path": None,
                        "uploaded_at": None
                    })

                    current_title = ""

                elif text and "view" not in text.lower() and "download" not in text.lower():
                    current_title = text

        except Exception as e:

            self.logger.error(f"Document extraction failed: {e}")

        return documents

    def extract_documents(self, cell):

        return self.extract_documents_from_html(str(cell))

    def get_download_url(self, cell, view_url):

        try:
            parsed_view = urlparse(view_url)

            for link in cell.select("a[href*='TenderDownload']"):
                return urljoin(self.BASE_URL, link.get("href", "").strip())

            query = parse_qs(parsed_view.query)
            tender_id = query.get("tid", [""])[0]

            if tender_id:
                return urljoin(self.BASE_URL, f"/Home/TenderDownload?tid={tender_id}")

        except Exception:
            pass

        return ""

    # ---------------- API REQUEST ----------------
    def build_datatable_payload(self, start=0, length=100, category_id="", tender_id="", assigned_tender=""):

        payload = {
            "draw": 1,
            "start": start,
            "length": length,
            "search[value]": "",
            "search[regex]": "false",
            "order[0][column]": 1,
            "order[0][dir]": "desc",
            "CatId": category_id,
            "TenderId": tender_id,
            "AssignedTender": assigned_tender
        }

        columns = ["", "tenderId", "", "recievingDate", "lastDateForSubmission", "", ""]

        for index, column in enumerate(columns):
            payload[f"columns[{index}][data]"] = column
            payload[f"columns[{index}][name]"] = ""
            payload[f"columns[{index}][searchable]"] = "true"
            payload[f"columns[{index}][orderable]"] = "true"
            payload[f"columns[{index}][search][value]"] = ""
            payload[f"columns[{index}][search][regex]"] = "false"

        return payload

    def fetch_tender_api_page(self, start=0, length=100, category_id="", assigned_tender=""):

        payload = self.build_datatable_payload(
            start=start,
            length=length,
            category_id=category_id,
            assigned_tender=assigned_tender
        )

        response = self.SESSION.post(
            self.API_URL,
            data=payload,
            timeout=60
        )

        if response.status_code != 200:
            self.logger.error(
                f"API request failed: {response.status_code} | {response.text[:300]}"
            )
            return None

        try:
            return response.json()

        except Exception as e:
            self.logger.error(f"API JSON parse failed: {e} | {response.text[:300]}")
            return None

    def fetch_all_tenders_from_api(self, category_id="", assigned_tender=""):

        tenders = []
        start = 0
        length = 100
        records_total = None

        while True:
            page = self.fetch_tender_api_page(
                start=start,
                length=length,
                category_id=category_id,
                assigned_tender=assigned_tender
            )

            if not page:
                break

            records_total = page.get("recordsFiltered") or page.get("recordsTotal") or records_total
            rows = page.get("data") or []

            self.logger.info(f"API rows found: {len(rows)} at start={start}")

            if not rows:
                break

            for index, item in enumerate(rows, start=start + 1):
                tender = self.parse_api_tender(item, index)

                if tender:
                    tenders.append(tender)

            start += len(rows)

            if records_total is not None and start >= int(records_total):
                break

            if len(rows) < length:
                break

        return tenders

    # ---------------- API ROW PARSE ----------------
    def parse_api_tender(self, item, serial_number):

        tender_number = self.clean_text(str(item.get("tenderId") or ""))
        tender_reference_number = self.clean_text(item.get("tenderRefNo"))
        summary = self.clean_text(item.get("tenderTitle"))
        tender_id = self.clean_text(str(item.get("tenderidnew") or ""))
        publish_date = self.clean_text(item.get("recievingDate"))
        submission_date = self.clean_text(item.get("lastDateForSubmission"))
        cost_raw = self.clean_text(
            f"{item.get('tenderValueType') or ''} {item.get('tenderValue') or ''}"
        )

        description = self.clean_text(
            f"Tender Ref. No: {tender_reference_number} {summary} Tender Id :{tender_id}"
        )

        document_html = ""

        for key in [
            "tenderDocument",
            "tenderDocument1",
            "tenderDocument2",
            "tenderDocument3",
            "tenderDocument4",
            "tenderDocument5",
            "tenderDocument6",
            "tenderDocument7",
            "download"
        ]:
            document_html += item.get(key) or ""

        documents = self.extract_documents_from_html(document_html)

        return {
            "serial_number": serial_number,
            "tender_number": tender_number,
            "tender_reference_number": tender_reference_number,
            "tender_subject": summary,
            "summary": summary,
            "tender_id": tender_id,
            "description": description,
            "publish_date": publish_date,
            "publish_date_parsed": self.parse_date(publish_date),
            "end_date": submission_date,
            "submission_date": submission_date,
            "submission_date_parsed": self.parse_date(submission_date),
            "cost": self.parse_amount(cost_raw),
            "cost_raw": cost_raw,
            "category": self.clean_text(item.get("categoryName")),
            "documents": documents,
            "raw_api_data": item
        }

    # ---------------- ROW PARSE ----------------
    def parse_row(self, row):

        cells = row.select("td")

        if len(cells) < 7:
            return None

        serial_number = self.clean_text(cells[0].get_text(" ", strip=True))
        tender_number = self.clean_text(cells[1].get_text(" ", strip=True))
        description = self.clean_text(cells[2].get_text(" ", strip=True))
        publish_date = self.clean_text(cells[3].get_text(" ", strip=True))
        submission_date = self.clean_text(cells[4].get_text(" ", strip=True))
        cost_raw = self.clean_text(cells[5].get_text(" ", strip=True))

        tender_reference_number, summary, tender_id = self.split_description(description)

        documents = self.extract_documents(cells[6])

        return {
            "serial_number": serial_number,
            "tender_number": tender_number,
            "tender_reference_number": tender_reference_number,
            "tender_subject": summary,
            "summary": summary,
            "tender_id": tender_id,
            "description": description,
            "publish_date": publish_date,
            "publish_date_parsed": self.parse_date(publish_date),
            "end_date": submission_date,
            "submission_date": submission_date,
            "submission_date_parsed": self.parse_date(submission_date),
            "cost": self.parse_amount(cost_raw),
            "cost_raw": cost_raw,
            "documents": documents
        }

    # ---------------- EXTRACT TABLE ----------------
    def extract_tenders(self, soup):

        tenders = []

        rows = soup.select("table#datatable1 tbody tr")

        self.logger.info(f"Rows found: {len(rows)}")

        for row in rows:
            tender = self.parse_row(row)

            if tender:
                tenders.append(tender)

        return tenders

    # ---------------- TOTAL PAGES ----------------
    def get_total_pages(self, soup):

        max_page = 1

        try:
            for link in soup.select("#datatable1_paginate a.paginate_button"):
                text = self.clean_text(link.get_text(" ", strip=True))

                if text.isdigit():
                    max_page = max(max_page, int(text))

        except Exception as e:
            self.logger.error(f"Pagination parse failed: {e}")

        return max_page

    # ---------------- PAGE REQUEST ----------------
    def fetch_page(self, page_no=1):

        response = self.SESSION.get(self.START_URL, timeout=60)

        if response.status_code != 200:
            self.logger.error(f"Failed page {page_no}: {response.status_code}")
            return None

        return BeautifulSoup(response.text, "lxml")

    # ---------------- SCRAPER ----------------
    def scrape(self):

        total = 0

        try:
            self.logger.info(f"Scraping: {self.START_URL}")

            self.fetch_page()

            tenders = self.fetch_all_tenders_from_api()

            if not tenders:
                self.logger.warning("No tenders found")
                return

            for data in tenders:

                try:
                    hash_id = self.generate_hash(
                        data["tender_number"],
                        data["tender_reference_number"],
                        data["summary"]
                    )

                    if self.raw_collection.find_one({"hash_id": hash_id}):
                        self.logger.info(
                            f"Skipped duplicate: {data['tender_number']}"
                        )
                        continue

                    teb_no = self.generate_teb_number()

                    tender = {
                        "hash_id": hash_id,
                        "teb_number": teb_no,
                        "source": "RITES",
                        "tender_number": data["tender_number"],
                        "tender_reference_number": data["tender_reference_number"],
                        "tender_subject": data["tender_subject"],
                        "summary": data["summary"],
                        "tender_id": data["tender_id"],
                        "description": data["description"],
                        "publish_date": data["publish_date"],
                        "end_date": data["end_date"],
                        "submission_date": data["submission_date"],
                        "cost": data["cost"],
                        "documents": data["documents"],
                        "etl_status": "pending",
                        "created_at": datetime.now(timezone.utc)
                    }

                    res = self.raw_collection.insert_one(tender)

                    if tender.get("documents"):
                        self.upload_to_s3(tender, res.inserted_id)

                    total += 1

                    self.logger.info(
                        f"Inserted: {data['tender_number']} | {data['summary'][:100]}"
                    )

                    time.sleep(random.uniform(0.5, 1.5))

                except Exception as e:

                    self.logger.error(f"Tender processing failed: {e}")

            self.logger.info(f"Total scraped: {total}")

        except Exception as e:

            self.logger.error(f"Scraper failed: {e}")


# ---------------- RUN ----------------
# if __name__ == "__main__":
#     Scraper().scrape()
