import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse, unquote

import boto3
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from requests.adapters import HTTPAdapter, Retry

# Automatically pull credentials and endpoints from local environment files
load_dotenv()


class Scraper:

    # ---------------- INIT ----------------
    def __init__(self):
        self.BASE_URL = "https://www.aai.aero"
        self.START_URL = (
            "https://www.aai.aero/en/tender/tender-search"
            "?field_region_tid=All"
            "&field_airport_tid=All"
            "&term_node_tid_depth=All"
            "&field_tender_status_value=All"
            "&field_tender_last_sale_date_value%5Bvalue%5D%5Bdate%5D="
            "&combine="
        )

        # Portals to skip
        self.SKIP_PORTALS = {"gem", "cppp", "cppp portal", "gem portal"}

        self.SESSION = requests.Session()

        retries = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )

        adapter = HTTPAdapter(max_retries=retries)
        self.SESSION.mount("https://", adapter)
        self.SESSION.mount("http://", adapter)

        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })

        # Warm up session / get cookies securely
        try:
            self.SESSION.get(self.BASE_URL, timeout=30)
        except Exception:
            pass

        # ---------------- DB ----------------
        mongo_uri = os.getenv("LOCAL_MONGO_URI")
        if not mongo_uri:
            raise ValueError("Critical: LOCAL_MONGO_URI is missing from environment configurations.")

        self.client = MongoClient(mongo_uri)
        self.db = self.client["tender_bharo"]
        self.raw_collection = self.db["aai_aero_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        # ---------------- S3 ----------------
        self.bucket = os.getenv("S3_BUCKET_NAME")
        self.base_folder = "tender_documents/aai_aero"
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
        self.logger = logging.getLogger("AAI_Tenders")

    # ---------------- TEB ID ----------------
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

    # ---------------- HASH ----------------
    def generate_hash(self, tender_id):
        return hashlib.md5(str(tender_id).encode()).hexdigest()

    # ---------------- CLEAN TEXT ----------------
    def clean_text(self, text):
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def extract_integer_cost(self, value):
        if not value:
            return None
        value = str(value).strip()
        value = re.sub(r'(?i)(rs\.?|inr|₹)', '', value)
        value = value.replace(',', '').strip()
        match = re.search(r'\d+(?:\.\d+)?', value)
        if not match:
            return None
        return int(float(match.group()))

    # ---------------- PORTAL CHECK ----------------
    def is_gem_or_cppp(self, portal_text):
        if not portal_text:
            return False
        lower = portal_text.lower()
        return any(kw in lower for kw in self.SKIP_PORTALS)

    # ---------------- CORRIGENDUM ----------------
    def fetch_corrigendum(self, tender_id):
        url = f"{self.BASE_URL}/tenders_bipass/{tender_id}"
        try:
            resp = self.SESSION.post(
                url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.START_URL,
                    "adrum": "isAjax:true",
                },
                timeout=30,
            )
            if resp.status_code != 200 or not resp.text.strip():
                return []

            soup = BeautifulSoup(resp.text, "lxml")
            rows = soup.select("table tr")[1:]  # skip header
            results = []

            for row in rows:
                cols = row.select("td")
                if len(cols) < 4:
                    continue

                a_tag = cols[3].select_one("a")
                download_url = ""
                download_title = ""
                if a_tag:
                    download_url = self.clean_text(a_tag.get("href", ""))
                    download_title = self.clean_text(a_tag.get_text())

                results.append({
                    "sr_no": self.clean_text(cols[0].get_text()),
                    "extension_date": self.clean_text(cols[1].get_text()),
                    "details": self.clean_text(cols[2].get_text()),
                    "download_url": download_url,
                    "download_title": download_title,
                })

            return results

        except Exception as e:
            self.logger.error(f"Corrigendum fetch failed for {tender_id}: {e}")
            return []

    # ---------------- IMPORTANT DATES ----------------
    def fetch_important_dates(self, tender_id):
        url = f"{self.BASE_URL}/tender_important_date/{tender_id}"
        try:
            resp = self.SESSION.post(
                url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.START_URL,
                    "adrum": "isAjax:true",
                },
                timeout=30,
            )
            if resp.status_code != 200 or not resp.text.strip():
                return {}

            soup = BeautifulSoup(resp.text, "lxml")
            rows = soup.select("table tr")[1:]  # skip header
            dates = {}

            for row in rows:
                cols = row.select("td")
                if len(cols) < 2:
                    continue
                label = self.clean_text(cols[0].get_text()).rstrip(":")
                value = self.clean_text(cols[1].get_text())
                if label:
                    dates[label] = value

            return dates

        except Exception as e:
            self.logger.error(f"Dates fetch failed for {tender_id}: {e}")
            return {}

    def extract_tender_id(self, li_soup):
        toggle = li_soup.select_one("div[id^='tender_toggle_']")
        if toggle:
            match = re.search(r"tender_toggle_(\d+)", toggle.get("id", ""))
            if match:
                return match.group(1)
        return None

    def parse_tender_row(self, li_soup):
        try:
            portal_div = li_soup.select_one("div.views-field-field-tender-published .field-content")
            portal_text = self.clean_text(portal_div.get_text() if portal_div else "")

            if self.is_gem_or_cppp(portal_text):
                self.logger.info(f"Skipped (portal={portal_text})")
                return None

            tender_id = self.extract_tender_id(li_soup)

            title_div = li_soup.select_one("div.col-md-10")
            title = self.clean_text(title_div.get_text() if title_div else "")

            if not title:
                return None

            general = li_soup.select_one("div.general-info")
            region_airport = ""
            last_sale_date = ""
            department = ""

            if general:
                cols = general.select("div.col-md-4")
                for col in cols:
                    text = self.clean_text(col.get_text(" "))
                    if "Region / Airport" in text:
                        region_airport = text.replace("Region / Airport :", "").strip()
                        region_airport = region_airport.replace("/", " ")
                        region_airport = re.sub(r"\s+", " ", region_airport).strip()
                    elif "Last Sale Date" in text:
                        last_sale_date = text.replace("Last Sale Date :", "").strip()
                    if "Department" in text:
                        department = text.replace("Department :", "").strip()

                if department:
                    department = f"{department} department"

            detail_div = li_soup.select_one("div.tender-info")
            tender_type = ""
            tender_is = ""
            min_support_price = ""
            estimated_cost = ""
            bid_type = ""
            e_bid_no = None
            status = ""
            description = ""

            if detail_div:
                cols = detail_div.select("div[class*='col-md']")
                for col in cols:
                    span = col.select_one("span")
                    if not span:
                        continue
                    label = self.clean_text(span.get_text()).lower()
                    full_text = self.clean_text(col.get_text(" "))
                    value = full_text.replace(self.clean_text(span.get_text()), "").strip().strip(":")

                    if "tender type" in label:
                        tender_type = value
                    elif "tender is" in label:
                        tender_is = value
                    elif "minimum support price" in label:
                        min_support_price = value
                    elif "estimated costs" in label:
                        estimated_cost = value
                    elif "bid type" in label:
                        bid_type = value
                    elif "e-bid no" in label:
                        e_bid_no = value
                    elif "status" in label:
                        status = value

                desc_div = li_soup.select_one("div.tednder_description")
                if desc_div:
                    description = self.clean_text(desc_div.get_text())

            download_url = ""
            download_div = li_soup.select_one("a[href*='/system/files_force/tender/']")
            if download_div:
                raw_href = download_div.get("href", "")
                download_url = urljoin(self.BASE_URL, raw_href)
                parsed = urlparse(download_url)
                download_url = urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
                )

            tender_bidding_type = ""
            if tender_type:
                tender_type_lower = tender_type.strip().lower()
                if "domestic" in tender_type_lower:
                    tender_bidding_type = "National Competitive Bidding (NCB)"
                elif "international" in tender_type_lower:
                    tender_bidding_type = "International Competitive Bidding (ICB)"

            return {
                "tender_id": tender_id,
                "portal": portal_text,
                "title": title,
                "region_airport": region_airport,
                "last_sale_date": last_sale_date,
                "department": department,
                "tender_type": tender_type,
                "tender_bidding_type": tender_bidding_type,
                "tender_is": tender_is,
                "min_support_price": min_support_price,
                "estimated_cost": estimated_cost,
                "bid_type": bid_type,
                "e_bid_no": e_bid_no,
                "status": status,
                "description": description,
                "download_url": download_url,
            }

        except Exception as e:
            self.logger.error(f"Row parse failed: {e}")
            return None

    # ---------------- BUILD DOCUMENTS LIST ----------------
    def build_documents(self, tender_data, corrigendum_list):
        documents = []

        if tender_data.get("download_url"):
            raw_url = tender_data["download_url"]
            filename = unquote(os.path.basename(urlparse(raw_url).path))
            documents.append({
                "type": "tender_document",
                "title": filename or "Tender Document",
                "original_url": raw_url,
                "s3_path": None,
                "uploaded_at": None,
            })

        for corr in corrigendum_list:
            if corr.get("download_url"):
                raw_url = corr["download_url"]
                filename = unquote(os.path.basename(urlparse(raw_url).path))
                documents.append({
                    "type": "corrigendum_document",
                    "title": corr.get("download_title") or filename or "Corrigendum",
                    "original_url": raw_url,
                    "s3_path": None,
                    "uploaded_at": None,
                })

        return documents

    # ---------------- S3 UPLOAD ----------------
    def upload_to_s3(self, doc_record, mongo_id):
        if not self.bucket:
            self.logger.warning("S3 Bucket Name environment context missing. Skipping upload step.")
            return

        folder = f"{doc_record['teb_number'].replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in doc_record.get("documents", []):
            try:
                url = d.get("original_url")
                if not url:
                    updated_docs.append(d)
                    continue

                resp = self.SESSION.get(url, timeout=60)
                if resp.status_code != 200:
                    self.logger.warning(f"Doc download failed ({resp.status_code}): {url}")
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
                elif ext.lower() == ".zip":
                    content_type = "application/zip"

                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=resp.content,
                    ContentType=content_type,
                )

                d["s3_path"] = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"Uploaded: {key}")

            except Exception as e:
                self.logger.error(f"S3 upload error: {e}")
                d["s3_path"] = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}},
        )

    def get_total_pages(self, soup):
        try:
            last_li = soup.select_one("li.pager-last a, .pager__item--last a")
            if last_li:
                href = last_li.get("href", "")
                match = re.search(r"page=(\d+)", href)
                if match:
                    return int(match.group(1)) + 1  

            max_page = 1
            for a in soup.select("ul.pager li.pager-item a, .pager__item a"):
                txt = self.clean_text(a.get_text())
                if txt.isdigit():
                    page_no = int(txt)
                    if page_no > max_page:
                        max_page = page_no
            return max_page

        except Exception as e:
            self.logger.error(f"Pagination parse failed: {e}")
            return 1

    def build_page_url(self, page_no):
        if page_no == 0:
            return self.START_URL
        return self.START_URL + f"&page={page_no}"

    # ---------------- CORE SCRAPE EXECUTION ----------------
    def scrape(self):
        total = 0

        try:
            self.logger.info(f"Starting AAI tender scrape: {self.START_URL}")

            first_response = self.SESSION.get(self.START_URL, timeout=60)
            if first_response.status_code != 200:
                self.logger.error(f"Failed to load first page: {first_response.status_code}")
                return

            first_soup = BeautifulSoup(first_response.text, "lxml")
            total_pages = self.get_total_pages(first_soup)
            self.logger.info(f"Total pages detected: {total_pages}")

            for page_no in range(0, total_pages):
                try:
                    url = self.build_page_url(page_no)
                    self.logger.info(f"Processing page {page_no + 1}/{total_pages}: {url}")

                    if page_no == 0:
                        soup = first_soup
                    else:
                        resp = self.SESSION.get(url, timeout=60)
                        if resp.status_code != 200:
                            self.logger.error(f"Failed page {page_no}: {url}")
                            continue
                        soup = BeautifulSoup(resp.text, "lxml")

                    # Robust multi-layer extraction selector layout patterns
                    tender_rows = soup.select("ul.tender-list li.views-row, .view-tenders .views-row, table.views-table tr")

                    if not tender_rows:
                        self.logger.warning(f"No rows found on page context index {page_no + 1}")
                        continue

                    self.logger.info(f"Rows identified on page {page_no + 1}: {len(tender_rows)}")

                    for li in tender_rows:
                        try:
                            data = self.parse_tender_row(li)
                            if not data:
                                continue  

                            tender_id = data.get("tender_id")
                            if not tender_id:
                                self.logger.warning("No tender_id extracted cleanly, bypassing row record.")
                                continue

                            hash_id = self.generate_hash(tender_id)
                            if self.raw_collection.find_one({"hash_id": hash_id}):
                                self.logger.info(f"Duplicate structural footprint skipped: tender_id={tender_id}")
                                continue

                            time.sleep(random.uniform(0.4, 0.8))
                            corrigendum = self.fetch_corrigendum(tender_id)

                            time.sleep(random.uniform(0.4, 0.8))
                            important_dates = self.fetch_important_dates(tender_id)

                            documents = self.build_documents(data, corrigendum)
                            teb_no = self.generate_teb_number()

                            tender_doc = {
                                "hash_id": hash_id,
                                "teb_number": teb_no,
                                "tender_id": tender_id,
                                "portal": data["portal"],
                                "title": data["title"],
                                "region_airport": data["region_airport"],
                                "last_sale_date": data["last_sale_date"],
                                "department": data["department"],
                                "tender_type": data["tender_type"],
                                "tender_bidding_type": data["tender_bidding_type"],
                                "tender_is": data["tender_is"],
                                "estimated_cost": self.extract_integer_cost(data["estimated_cost"]),
                                "min_support_price": self.extract_integer_cost(data["min_support_price"]),
                                "bid_type": data["bid_type"],
                                "e_bid_no": data["e_bid_no"],
                                "status": data["status"],
                                "description": data["description"],
                                "important_dates": important_dates,
                                "documents": documents,
                                "source_url": url,
                                "etl_status": "pending",
                                "created_at": datetime.now(timezone.utc),
                            }

                            res = self.raw_collection.insert_one(tender_doc)

                            if documents:
                                self.upload_to_s3(tender_doc, res.inserted_id)

                            total += 1
                            self.logger.info(f"Inserted [{teb_no}]: {data['title'][:100]}")

                            time.sleep(random.uniform(0.5, 1.5))

                        except Exception as e:
                            self.logger.error(f"Row processing pipeline fault: {e}")

                    time.sleep(random.uniform(1, 3))

                except Exception as e:
                    self.logger.error(f"Page structural processing dropped at {page_no}: {e}")

            self.logger.info(f"Scrape completed successfully. Total novel inserts committed: {total}")

        except Exception as e:
            self.logger.error(f"Scraper runtime engine critical structural exception: {e}")


if __name__ == "__main__":
    Scraper().scrape()