import os
import logging
import requests
import time
import random
import boto3
import re
import hashlib

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlencode
from datetime import datetime, timezone
from dateutil import parser as dateparser
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from requests.adapters import HTTPAdapter, Retry

from dotenv import load_dotenv
load_dotenv()


class PAEMarketplaceScraper:

    # ─────────────────────────────────────────
    
    def __init__(self):
        self.BASE_URL    = "https://www.emarketplace.state.pa.us"
        self.SEARCH_URL  = f"{self.BASE_URL}/Search.aspx"
        self.DETAIL_URL  = f"{self.BASE_URL}/Solicitations.aspx"
        self.DOWNLOAD_URL = f"{self.BASE_URL}/FileDownload.aspx"

        # 0 = Current Records, 1 = Archived Records
        self.RECORD_TYPES = [("0", "Current"), ("1", "Archived")]

        # ── requests Session ──────────────────
        self.SESSION = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504])
        self.SESSION.mount("https://", HTTPAdapter(max_retries=retries))
        self.SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language":  "en-US,en;q=0.9",
            "Accept-Encoding":  "gzip, deflate, br",
            "Connection":       "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })

        # ── MongoDB ──────────────────────────
        mongo_uri = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017")
        self.client          = MongoClient(mongo_uri)
        self.db              = self.client["tender_bharo"]
        self.raw_collection  = self.db["pa_emarketplace_tenders"]
        self.meta_collection = self.db["meta_data"]
        self.raw_collection.create_index("hash_id", unique=True)

        

        # ── Logging ──────────────────────────
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s"
        )
        self.logger = logging.getLogger("PA_EMarketplace")

    # ─────────────────────────────────────────
    
    def generate_teb_id(self):
        counter = self.meta_collection.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        month_map = {
            1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
            7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"
        }
        return f"TEB/{now.year}/{month_map[now.month]}/{seq:08d}"

    # ─────────────────────────────────────────
   
    def generate_hash(self, sid: str) -> str:
        return hashlib.md5(str(sid).encode()).hexdigest()

    # ─────────────────────────────────────────
 
    def parse_date(self, date_val):
        try:
            if not date_val:
                return None
            dt = dateparser.parse(str(date_val).strip(), dayfirst=False)
            return dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            self.logger.error(f"Date parse failed: {date_val} | {e}")
            return None

    # ─────────────────────────────────────────
    
    def prime_session(self):
        self.logger.info("Priming session via GET /Search.aspx ...")
        resp = self.SESSION.get(self.SEARCH_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        fields = self._extract_hidden_fields(soup)
        self.logger.info(
            f"Session primed. Cookies: {list(self.SESSION.cookies.keys())} "
            f"| Hidden fields: {list(fields.keys())}"
        )
        return fields

    # ─────────────────────────────────────────
    
    def _extract_hidden_fields(self, soup: BeautifulSoup) -> dict:
        fields = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name  = inp.get("name")
            value = inp.get("value", "")
            if name:
                fields[name] = value
        return fields

    # ─────────────────────────────────────────
    
    def fetch_search_page(
        self,
        record_type: str,
        hidden_fields: dict,
        page_num: int = 1,
        rows_per_page: int = 100,
        event_target: str = "",
        event_argument: str = "",
    ) -> BeautifulSoup:
        """
        Sends a POST to Search.aspx.
        - First page: btnSearch click with rdoArch = record_type
        - Subsequent pages: __doPostBack with Page$N
        """
        data = {
            "__LASTFOCUS":        hidden_fields.get("__LASTFOCUS", ""),
            "__EVENTTARGET":      event_target,
            "__EVENTARGUMENT":    event_argument,
            "__VIEWSTATE":        hidden_fields.get("__VIEWSTATE", ""),
            "__VIEWSTATEGENERATOR": hidden_fields.get("__VIEWSTATEGENERATOR", ""),
            "__SCROLLPOSITIONX":  hidden_fields.get("__SCROLLPOSITIONX", "0"),
            "__SCROLLPOSITIONY":  hidden_fields.get("__SCROLLPOSITIONY", "0"),
            "__VIEWSTATEENCRYPTED": hidden_fields.get("__VIEWSTATEENCRYPTED", ""),
            "__EVENTVALIDATION":  hidden_fields.get("__EVENTVALIDATION", ""),

            # Search form fields (blank = all)
            "ctl00$MainBody$txtBidNo":    "",
            "ctl00$MainBody$txtBidTitle": "",
            "ctl00$MainBody$ddlAgency":   "0",
            "ctl00$MainBody$ddlCounty":   "0",
            "ctl00$MainBody$ddlTypes":    "",
            "ctl00$MainBody$rdolAdTypes": "11",   # All advertisement types
            "ctl00$MainBody$txtbOpenDate": "",
            "ctl00$MainBody$txtDatePre":  "",
            "ctl00$MainBody$ddlRows":     str(rows_per_page),
            "ctl00$MainBody$rdoArch":     record_type,   # 0=Current, 1=Archived
            "ctl00$MainBody$hdnFromPage": "3",
        }

        # First page: trigger the Search button
        if page_num == 1 and not event_target:
            data["ctl00$MainBody$btnSearch"] = "Search"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      self.SEARCH_URL,
            "Origin":       self.BASE_URL,
        }

        resp = self.SESSION.post(
            self.SEARCH_URL, data=data, headers=headers, timeout=60
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    # ─────────────────────────────────────────
 
    def parse_search_results(self, soup: BeautifulSoup) -> list:
        results = []
        grid = soup.find("table", {"id": "ctl00_MainBody_grdResults"})
        if not grid:
            return results

        rows = grid.find_all("tr", class_=["GridItem", "GridAltItem"])
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 11:
                continue

            # Solicitation # (link)
            link_tag = cells[0].find("a")
            if not link_tag:
                continue
            sid      = link_tag.get_text(strip=True)
            href     = link_tag.get("href", "")
            detail_url = urljoin(self.BASE_URL, href) if href else None

            # Full description from tooltip div
            desc_div = cells[3].find("div")
            full_desc = desc_div.get_text(strip=True) if desc_div else cells[3].get_text(strip=True)

            results.append({
                "solicitation_number": sid,
                "detail_url":          detail_url,
                "bid_type":            cells[1].get_text(strip=True),
                "title":               cells[2].get_text(strip=True),
                "description":         full_desc,
                "agency":              cells[4].get_text(strip=True),
                "county":              cells[5].get_text(strip=True),
                "amended_date":        cells[6].get_text(strip=True),
                "start_date":          cells[7].get_text(strip=True),
                "due_date":            cells[8].get_text(strip=True),
                "bid_opening_date":    cells[9].get_text(strip=True),
                "status":              cells[10].get_text(strip=True),
                "contact_person":      cells[11].get_text(strip=True) if len(cells) > 11 else "",
            })
        return results

    # ─────────────────────────────────────────
    
    def get_total_pages(self, soup: BeautifulSoup) -> int:
        pager = soup.find("tr", class_="GridPager")
        if not pager:
            return 1
        links = pager.find_all("a")
        max_page = 1
        for a in links:
            txt = a.get_text(strip=True)
            if txt == "...":
                # Last visible link before "..." gives a hint; we'll discover dynamically
                continue
            try:
                n = int(txt)
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        # Also check the postback argument in href for the "..." link
        for a in links:
            href = a.get("href", "")
            m = re.search(r"Page\$(\d+)", href)
            if m:
                n = int(m.group(1))
                if n > max_page:
                    max_page = n
        return max_page

    # ─────────────────────────────────────────
 
    def fetch_all_results(self, record_type: str, record_label: str) -> list:
        all_results = []

        # Prime and get first page
        hidden_fields = self.prime_session()
        self.logger.info(f"[{record_label}] Fetching page 1 ...")
        soup = self.fetch_search_page(
            record_type=record_type,
            hidden_fields=hidden_fields,
            page_num=1,
        )

        rows = self.parse_search_results(soup)
        all_results.extend(rows)
        self.logger.info(f"[{record_label}] Page 1: {len(rows)} records")

        # Update hidden fields from the response (ViewState changes after each POST)
        hidden_fields = self._extract_hidden_fields(soup)

        # Check how many pages exist
        total_pages = self.get_total_pages(soup)

        # Discover more pages: if pager has "..." we navigate until no more data
        page = 2
        while True:
            self.logger.info(f"[{record_label}] Fetching page {page} ...")
            try:
                soup = self.fetch_search_page(
                    record_type=record_type,
                    hidden_fields=hidden_fields,
                    page_num=page,
                    event_target="ctl00$MainBody$grdResults",
                    event_argument=f"Page${page}",
                )
                hidden_fields = self._extract_hidden_fields(soup)
                rows = self.parse_search_results(soup)

                if not rows:
                    self.logger.info(f"[{record_label}] No more rows on page {page}, stopping.")
                    break

                all_results.extend(rows)
                self.logger.info(
                    f"[{record_label}] Page {page}: {len(rows)} records "
                    f"(cumulative: {len(all_results)})"
                )

                # Check if next page exists in pager
                new_total = self.get_total_pages(soup)
                if page >= new_total:
                    
                    pass

                page += 1
                time.sleep(random.uniform(0.8, 1.5))

            except Exception as e:
                self.logger.error(f"[{record_label}] Failed fetching page {page}: {e}")
                break

        self.logger.info(f"[{record_label}] Total records scraped: {len(all_results)}")
        return all_results

    # ─────────────────────────────────────────
  
    def fetch_detail(self, sid: str) -> dict:
        try:
            url = f"{self.DETAIL_URL}?SID={sid}"
            resp = self.SESSION.get(url, timeout=30)
            resp.raise_for_status()
            return self._parse_detail(resp.text, sid)
        except Exception as e:
            self.logger.error(f"Failed fetching detail for SID {sid}: {e}")
            return {}

    def _parse_detail(self, html: str, sid: str) -> dict:
        soup = BeautifulSoup(html, "lxml")

        def get_span(span_id):
            tag = soup.find("span", {"id": span_id})
            return tag.get_text(strip=True) if tag else None

        detail = {
            "detail_url":           f"{self.DETAIL_URL}?SID={sid}",
            "department":           get_span("ctl00_MainBody_lblSolicitationDept"),
            "date_prepared":        get_span("ctl00_MainBody_lblDatePre"),
            "bid_type":             get_span("ctl00_MainBody_lblTypes"),
            "solicitation_number":  get_span("ctl00_MainBody_lblBidNo"),
            "solicitation_title":   get_span("ctl00_MainBody_lblBidTitle"),
            "description":          get_span("ctl00_MainBody_lblDesc"),
            "agency":               get_span("ctl00_MainBody_lblAgency"),
            "delivery_location":    get_span("ctl00_MainBody_lblLocation"),
            "county":               get_span("ctl00_MainBody_lblCounty"),
            "duration":             get_span("ctl00_MainBody_lblDuration"),
            "contact_first_name":   get_span("ctl00_MainBody_lblFName"),
            "contact_last_name":    get_span("ctl00_MainBody_lblLName"),
            "contact_phone":        get_span("ctl00_MainBody_lblPhone"),
            "contact_email":        get_span("ctl00_MainBody_lblEmail"),
            "start_date":           get_span("ctl00_MainBody_lblStartDate"),
            "due_date":             get_span("ctl00_MainBody_lblEndDate"),
            "due_time":             get_span("ctl00_MainBody_lblDueTime"),
            "opening_date":         get_span("ctl00_MainBody_lblOpenDate"),
            "opening_time":         get_span("ctl00_MainBody_lblOpenTime"),
            "opening_location":     get_span("ctl00_MainBody_lblLoc"),
            "num_addendums":        get_span("ctl00_MainBody_lblAdd"),
            "amended_date":         get_span("ctl00_MainBody_lblAmendedDt"),
        }

        # Advertisement type (checked radio)
        ad_type_table = soup.find("table", {"id": "ctl00_MainBody_rdolAdTypes"})
        if ad_type_table:
            checked = ad_type_table.find("input", {"checked": True})
            if checked:
                label = ad_type_table.find("label", {"for": checked.get("id")})
                detail["advertisement_type"] = label.get_text(strip=True) if label else None

        # Tabulations / Awards / Contracts links or messages
        detail["tabulation_info"] = get_span("ctl00_MainBody_lblTabLink")
        detail["awards_info"]     = get_span("ctl00_MainBody_lblBidLink")
        detail["contracts_info"]  = get_span("ctl00_MainBody_lblContLink")

        # Documents
        detail["documents"] = self._parse_documents(soup, sid)

        return detail

    # ─────────────────────────────────────────
   
    def _parse_documents(self, soup: BeautifulSoup, sid: str) -> list:
        documents = []

        # Original Files table
        orig_table = soup.find("table", {"id": "ctl00_MainBody_dgFileList"})
        if orig_table:
            for a in orig_table.find_all("a", href=True):
                href  = a["href"].strip()
                title = a.get_text(strip=True)
                full_url = urljoin(self.BASE_URL, href)
                documents.append({
                    "type":         "Tender_document",
                    "title":        title,
                    "original_url": full_url,
                    "s3_path":      None,
                    "uploaded_at":  None,
                })

        # Flyers / Addendums table
        addendum_table = soup.find("table", {"id": "ctl00_MainBody_dgFileList2"})
        if addendum_table:
            for a in addendum_table.find_all("a", href=True):
                href  = a["href"].strip()
                title = a.get_text(strip=True)
                full_url = urljoin(self.BASE_URL, href)
                documents.append({
                    "type":         "Tender_addendum",
                    "title":        title,
                    "original_url": full_url,
                    "s3_path":      None,
                    "uploaded_at":  None,
                })

        return documents

    # ─────────────────────────────────────────
 
    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        folder       = f"{teb_number.replace('/', '_')}_{mongo_id}"
        updated_docs = []

        for d in documents:
            try:
                url = d.get("original_url")
                if not url:
                    updated_docs.append(d)
                    continue

                response = self.SESSION.get(
                    url,
                    headers={"Referer": self.BASE_URL},
                    timeout=60,
                )
                if response.status_code != 200:
                    self.logger.warning(f"Could not download document: {url}")
                    updated_docs.append(d)
                    continue

                title = d.get("title") or os.path.basename(urlparse(url).path) or "document"
                title = re.sub(r'[^\w\-. ]', '_', title)

                # Infer extension from URL or content-type if missing
                if not any(title.lower().endswith(ext) for ext in
                           ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.txt']):
                    ct = response.headers.get("Content-Type", "")
                    if "excel" in ct or "spreadsheet" in ct:
                        title += ".xlsx"
                    else:
                        title += ".pdf"

                key = f"{self.base_folder}/{folder}/{title}"
                content_type = "application/pdf"
                if title.lower().endswith(('.xls', '.xlsx')):
                    content_type = "application/vnd.ms-excel"
                elif title.lower().endswith(('.doc', '.docx')):
                    content_type = "application/msword"
                elif title.lower().endswith('.zip'):
                    content_type = "application/zip"

                self.s3.put_object(
                    Bucket=self.bucket, Key=key,
                    Body=response.content, ContentType=content_type,
                )
                d["s3_path"]     = f"s3://{self.bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc)
                self.logger.info(f"Uploaded to S3: {key}")

            except Exception as e:
                self.logger.error(f"S3 upload failed for {d.get('original_url')}: {e}")
                d["s3_path"]     = None
                d["uploaded_at"] = None

            updated_docs.append(d)

        # Persist updated docs back to Mongo
        self.raw_collection.update_one(
            {"_id": mongo_id},
            {"$set": {"documents": updated_docs}},
        )
        return updated_docs

    # ─────────────────────────────────────────
  
    def _retry_missing_uploads(self, hash_id: str, sid: str):
        existing = self.raw_collection.find_one({"hash_id": hash_id})
        if not existing:
            return

        docs    = existing.get("documents", [])
        pending = [d for d in docs if not d.get("s3_path") and d.get("original_url")]

        if pending:
            self.logger.info(
                f"Duplicate SID {sid}: {len(pending)} un-uploaded doc(s) — uploading."
            )
            uploaded     = self.upload_to_s3(
                pending,
                teb_number=existing.get("teb_number", f"UNKNOWN_{sid}"),
                mongo_id=existing["_id"],
            )
            uploaded_map = {d["original_url"]: d for d in uploaded if d.get("original_url")}
            merged = [uploaded_map.get(d.get("original_url"), d) for d in docs]
            self.raw_collection.update_one(
                {"_id": existing["_id"]},
                {"$set": {"documents": merged}},
            )
        else:
            self.logger.info(f"Duplicate SID {sid}: all docs already uploaded.")

    # ─────────────────────────────────────────
    
    def scrape(self):
        global_total = 0

        for record_value, record_label in self.RECORD_TYPES:
            self.logger.info("=" * 60)
            self.logger.info(f"Starting scrape: [{record_label} Records]")
            self.logger.info("=" * 60)

            search_rows = self.fetch_all_results(record_value, record_label)
            self.logger.info(
                f"[{record_label}] Total rows from search: {len(search_rows)}"
            )

            status_total = 0

            for row in search_rows:
                sid = row.get("solicitation_number")
                if not sid:
                    continue

                hash_id = self.generate_hash(sid)

                # Duplicate pre-check
                if self.raw_collection.find_one({"hash_id": hash_id}):
                    self.logger.info(
                        f"[{record_label}] Duplicate – checking uploads: {sid}"
                    )
                    self._retry_missing_uploads(hash_id, sid)
                    continue

                try:
                    self.logger.info(f"[{record_label}] Fetching detail: {sid}")
                    detail = self.fetch_detail(sid)
                    teb_no = self.generate_teb_id()

                    # Merge search-row data with detail data
                    # (detail takes precedence for overlapping fields)
                    payload = {
                        "hash_id":              hash_id,
                        "teb_number":           teb_no,
                        "record_type":          record_label,
                        "solicitation_number":  sid,
                        "solicitation_title":   detail.get("solicitation_title") or row.get("title"),
                        "description":          detail.get("description") or row.get("description"),
                        "department":           detail.get("department"),
                        "bid_type":             detail.get("bid_type") or row.get("bid_type"),
                        "advertisement_type":   detail.get("advertisement_type"),
                        "agency":               detail.get("agency") or row.get("agency"),
                        "county":               detail.get("county") or row.get("county"),
                        "delivery_location":    detail.get("delivery_location"),
                        "duration":             detail.get("duration"),
                        "start_date":           self.parse_date(detail.get("start_date") or row.get("start_date")),
                        "due_date":             self.parse_date(detail.get("due_date") or row.get("due_date")),
                        "due_time":             detail.get("due_time"),
                        "opening_date":         self.parse_date(detail.get("opening_date") or row.get("bid_opening_date")),
                        "opening_time":         detail.get("opening_time"),
                        "opening_location":     detail.get("opening_location"),
                        "amended_date":         self.parse_date(detail.get("amended_date") or row.get("amended_date")),
                        "date_prepared":        self.parse_date(detail.get("date_prepared")),
                        "num_addendums":        detail.get("num_addendums"),
                        "status":               row.get("status"),
                        "contact_person":       row.get("contact_person"),
                        "contact_first_name":   detail.get("contact_first_name"),
                        "contact_last_name":    detail.get("contact_last_name"),
                        "contact_phone":        detail.get("contact_phone"),
                        "contact_email":        detail.get("contact_email"),
                        "tabulation_info":      detail.get("tabulation_info"),
                        "awards_info":          detail.get("awards_info"),
                        "contracts_info":       detail.get("contracts_info"),
                        "detail_url":           detail.get("detail_url") or row.get("detail_url"),
                        "documents":            detail.get("documents", []),
                        "source":               "PA eMarketplace",
                        "source_url":           self.SEARCH_URL,
                        "etl_status":           "pending",
                        "created_at":           datetime.now(timezone.utc),
                    }

                    try:
                        res = self.raw_collection.insert_one(payload)
                        self.logger.info(
                            f"[{record_label}] Stored: {sid} (TEB: {teb_no})"
                        )
                    except DuplicateKeyError:
                        self.logger.info(
                            f"[{record_label}] Race-condition duplicate – checking uploads: {sid}"
                        )
                        self._retry_missing_uploads(hash_id, sid)
                        continue

                    # Upload documents to S3
                    if payload.get("documents"):
                        self.upload_to_s3(
                            payload["documents"],
                            teb_number=teb_no,
                            mongo_id=res.inserted_id,
                        )

                    status_total += 1
                    global_total += 1

                except Exception as e:
                    self.logger.error(
                        f"[{record_label}] Failed processing SID {sid}: {e}"
                    )

                time.sleep(random.uniform(1.0, 2.0))

            self.logger.info(
                f"[{record_label}] Completed. Records inserted: {status_total}"
            )

        self.logger.info(f"All runs complete. Total records inserted: {global_total}")


if __name__ == "__main__":
    PAEMarketplaceScraper().scrape()