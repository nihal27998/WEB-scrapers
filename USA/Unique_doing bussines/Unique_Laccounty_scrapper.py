

import os
import json
import time
import random
import logging
import hashlib
import argparse
from datetime import datetime, timezone

import requests
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter, Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
    from pymongo import MongoClient, ReturnDocument
    from pymongo.errors import DuplicateKeyError
    STORAGE_LIBS = True
except ImportError:
    STORAGE_LIBS = False
    class DuplicateKeyError(Exception):
        pass


# ══════════════════════════════════════════════════════════════════════════════
class LACountyBidScraper:

    FRONTEND_BASE   = "https://doingbusiness.lacounty.gov"
    SEARCH_PAGE_URL = f"{FRONTEND_BASE}/solicitation-search/"
    # Frontend URL for the closed/awarded view -- used only as a Referer /
    # for prime_session() when scraping closed bids; same SPA shell as the
    # open-bid search page, just a different `type=` query param.
    AWARDED_SEARCH_PAGE_URL = f"{FRONTEND_BASE}/solicitation-search/?type=awdMainSearch&val="

    API_BASE         = "https://lacobids.lacounty.gov/api/v1/opensolicitations"
    CLOSED_API_BASE  = "https://lacobids.lacounty.gov/api/v1/closedsolicitations"

    # Confirmed via captured Network tab data: an empty `search` value
    # returns every open solicitation in one shot (no pagination observed).
    LISTING_URL    = f"{API_BASE}/globalsearch"
    LISTING_PARAMS = {"search": ""}

    # Confirmed via captured Network tab data (same shape as above, plus
    # AwardCount / NIACount fields on each row).
    CLOSED_LISTING_URL    = f"{CLOSED_API_BASE}/globalsearch"
    CLOSED_LISTING_PARAMS = {"search": ""}

    DETAIL_URL_TMPL      = API_BASE + "/{bid_ref_nbr}"
    ATTACHMENTS_URL_TMPL = API_BASE + "/{bid_ref_nbr}/attamend"

    # *** UNVERIFIED *** -- not captured directly, assumed by analogy with
    # the confirmed open-bid endpoints above. See module docstring.
    CLOSED_DETAIL_URL_TMPL      = CLOSED_API_BASE + "/{bid_ref_nbr}"
    CLOSED_ATTACHMENTS_URL_TMPL = CLOSED_API_BASE + "/{bid_ref_nbr}/attamend"

    
    @staticmethod
    def _build_document_url(bid_ref_nbr: str, att: dict, api_base: str = None) -> str | None:
        nbr = (att.get("AttBidNbr") or "").strip() or (att.get("AttFileNbr") or "").strip()
        if not nbr:
            return None
        base = api_base or LACountyBidScraper.API_BASE
        return f"{base}/{bid_ref_nbr}/attachments/{nbr}"

    # ──────────────────────────────────────────────────────────────────────────
    def __init__(self, use_storage=True, debug=False, output_dir="output", max_bids=None,
                 bid_status="open"):
        """
        bid_status: "open"   -> scrape only open solicitations (original behavior)
                    "closed" -> scrape only closed/awarded solicitations
                    "both"   -> scrape both, tagged via bid_status field on each record
        """
        self.debug      = debug
        self.output_dir = output_dir
        self.max_bids   = max_bids

        if bid_status not in ("open", "closed", "both"):
            raise ValueError(f"bid_status must be 'open', 'closed', or 'both' -- got {bid_status!r}")
        self.bid_status = bid_status

        self.session = requests.Session()
        retry = Retry(
            total=4, backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "Accept-Language":  "en-US,en;q=0.9",
            "Origin":           self.FRONTEND_BASE,
            "Referer":          self.FRONTEND_BASE + "/",
            "X-Requested-With": "XMLHttpRequest",
        })

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger = logging.getLogger("LACountyBids")

        if self.debug:
            os.makedirs("debug_json", exist_ok=True)

        self.use_storage = (
            use_storage
            and STORAGE_LIBS
            and bool(os.getenv("LOCAL_MONGO_URI"))
        )
        if use_storage and not self.use_storage:
            self.logger.warning(
                "Storage requested but pymongo/boto3 or LOCAL_MONGO_URI missing — "
                "falling back to local JSON."
            )
        if self.use_storage:
            self._init_storage()

    # ──────────────────────────────────────────────────────────────────────────
    def _init_storage(self):
        self.client    = MongoClient(os.getenv("LOCAL_MONGO_URI"))
        db             = self.client["tender_bharo"]
        self.raw_col   = db["lacounty_ca_tenders"]
        self.meta_col  = db["meta_data"]
        self.raw_col.create_index("hash_id", unique=True)

        self.s3_bucket      = os.getenv("S3_BUCKET_NAME", "")
        self.s3_base_folder = "tender_documents/lacounty_ca_tenders"
        if self.s3_bucket:
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name           = os.getenv("AWS_REGION", "us-east-1"),
            )
            self.logger.info("S3 configured.")
        else:
            self.s3 = None
        self.logger.info("MongoDB connected.")

    # ══════════════════════════════════════════════════════════════════════════
  
    @staticmethod
    def _clean(text):
        return (text or "").strip()

    def _hash(self, bid_ref_nbr, bid_status="open"):
        # bid_status folded into the hash so the same BidRefNbr appearing
        # in both the open and closed listings (e.g. right at the moment
        # it closes) produces two distinct hash_ids instead of colliding.
        return hashlib.md5(f"lacounty_ca_bids_{bid_status}_{bid_ref_nbr}".encode()).hexdigest()

    def _teb(self):
        if not self.use_storage:
            return ""
        counter = self.meta_col.find_one_and_update(
            {"_id": "tb_global_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = counter["seq"]
        now = datetime.now(timezone.utc)
        mm  = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
               7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
        return f"TEB/{now.year}/{mm[now.month]}/{seq:08d}"

    def _parse_date(self, raw):
        """Most date fields here arrive as clean ISO-8601 strings already
        (e.g. '2014-04-01T00:00:00.000Z'), but fall back to dateutil for
        anything looser. The 1900-01-01 sentinel used for 'no amend date'
        on Bid-type attachments is treated as no date at all."""
        if not raw:
            return None
        raw = self._clean(str(raw))
        if raw.startswith("1900-01-01"):
            return None
        try:
            return date_parser.parse(raw)
        except Exception:
            self.logger.warning(f"Cannot parse date: {raw!r}")
            return None

    def _debug_save(self, payload, filename):
        path = os.path.join("debug_json", filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        self.logger.info(f"[debug] saved → {path}")
    def _warn_if_suspiciously_round(self, count: int, label: str):
        round_thresholds = (1000, 1500, 2000, 2500, 3000, 5000, 6000, 10000, 20000)
        if count in round_thresholds:
            self.logger.warning(
                f"  ⚠ {label}: got exactly {count} record(s) — this is a suspiciously "
                f"round number. The API may be silently truncating results server-side. "
                f"Verify against the real total on the site before trusting this is everything."
            )

    # ══════════════════════════════════════════════════════════════════════════
  

    def prime_session(self, bid_status="open"):
        """Hit the human-facing search page first so the session picks up
        the Incapsula visid_incap_/incap_ses_ cookies before we start
        calling the API directly -- same trick used in the Mansfield
        script, and likely more necessary here since this API sits behind
        Imperva (visible in your captured response headers).

        Uses the matching frontend page (open vs awarded search view) as
        Referer, in case Imperva keys the challenge to the originating
        page rather than just the domain."""
        page_url = self.AWARDED_SEARCH_PAGE_URL if bid_status == "closed" else self.SEARCH_PAGE_URL
        self.logger.info(f"Priming session ({bid_status}) …")
        r = self.session.get(page_url, timeout=30)
        r.raise_for_status()
        self.logger.info(f"Session primed. Cookies: {list(self.session.cookies.keys())}")

    # ══════════════════════════════════════════════════════════════════════════
    

    def fetch_listing(self) -> list:
        """Returns the full list of open-solicitation dicts from the
        globalsearch endpoint. Each one already has nearly every field
        the single-bid detail endpoint has (see module docstring), so
        callers can treat these as ready-to-use 'detail' dicts directly
        -- no need to call fetch_detail() per item."""
        if not self.LISTING_URL:
            self.logger.error("LISTING_URL is not configured. Returning an empty list.")
            return []

        self.logger.info("Fetching solicitation listing …")
        r = self.session.get(self.LISTING_URL, params=self.LISTING_PARAMS, timeout=30)
        r.raise_for_status()
        data = r.json()
        if self.debug:
            self._debug_save(data, "listing.json")

        stubs = data if isinstance(data, list) else data.get("results", [])
        self.logger.info(f"{len(stubs)} solicitation(s) found in listing.")
        return stubs

    def fetch_closed_listing(self) -> list:
        """Closed/awarded counterpart of fetch_listing(). Confirmed
        endpoint (captured Network tab data): same shape as the open
        listing, plus AwardCount and NIACount on each row."""
        if not self.CLOSED_LISTING_URL:
            self.logger.error("CLOSED_LISTING_URL is not configured. Returning an empty list.")
            return []

        self.logger.info("Fetching closed/awarded solicitation listing …")
        r = self.session.get(self.CLOSED_LISTING_URL, params=self.CLOSED_LISTING_PARAMS, timeout=30)
        r.raise_for_status()
        data = r.json()
        if self.debug:
            self._debug_save(data, "closed_listing.json")

        stubs = data if isinstance(data, list) else data.get("results", [])
        self.logger.info(f"{len(stubs)} closed/awarded solicitation(s) found in listing.")
        return stubs

    # ══════════════════════════════════════════════════════════════════════════
   

    def fetch_detail(self, bid_ref_nbr: str, bid_status: str = "open") -> dict:
        url_tmpl = self.CLOSED_DETAIL_URL_TMPL if bid_status == "closed" else self.DETAIL_URL_TMPL
        self.logger.info(f"  Detail BidRefNbr={bid_ref_nbr} ({bid_status}) …")
        url = url_tmpl.format(bid_ref_nbr=bid_ref_nbr)
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.error(f"  Detail fetch failed: {e}")
            return {}

        data = r.json()
        if self.debug:
            self._debug_save(data, f"detail_{bid_status}_{bid_ref_nbr}.json")

        if not data:
            return {}
        return data[0] if isinstance(data, list) else data

    # ══════════════════════════════════════════════════════════════════════════
    

    def fetch_attachments(self, bid_ref_nbr: str, bid_status: str = "open") -> list:
        url_tmpl = self.CLOSED_ATTACHMENTS_URL_TMPL if bid_status == "closed" else self.ATTACHMENTS_URL_TMPL
        api_base = self.CLOSED_API_BASE if bid_status == "closed" else self.API_BASE

        self.logger.info(f"  Attachments BidRefNbr={bid_ref_nbr} ({bid_status}) …")
        url = url_tmpl.format(bid_ref_nbr=bid_ref_nbr)
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            self.logger.error(f"  Attachments fetch failed: {e}")
            return []

        data = r.json()
        if self.debug:
            self._debug_save(data, f"attamend_{bid_status}_{bid_ref_nbr}.json")

        if not isinstance(data, list):
            return []

        documents = []
        for att in data:
            documents.append({
                "title":         self._clean(att.get("AttFileDesc") or att.get("Description") or att.get("AttFileName", "")),
                "type":          "Tender_document",
                "original_url":  self._build_document_url(bid_ref_nbr, att, api_base=api_base),
                "s3_path":       None,
                "uploaded_at":   None,
            })
        return documents

    # ══════════════════════════════════════════════════════════════════════════
   

    def _build_record(self, detail: dict, documents: list, teb_number: str, bid_status: str = "open") -> dict:
        bid_ref_nbr = detail.get("BidRefNbr", "")
        search_type = "awdMainSearch" if bid_status == "closed" else "solMainSearch"

        return {
            "hash_id":             self._hash(bid_ref_nbr, bid_status=bid_status),
            "teb_number":          teb_number,
            "etl_status":          "pending",
            "bid_status":          bid_status,          # "open" or "closed"
            "bid_ref_nbr":         bid_ref_nbr,
            "bid_number":          detail.get("BidNumber", ""),
            "bid_type":            detail.get("BidType", ""),
            "title":               detail.get("BidTitle", ""),
            "description":         detail.get("BidDesc", ""),
            "department_number":   detail.get("DeptNbr", ""),
            "department":          detail.get("DeptName", ""),
            "bid_amount":          detail.get("BidAmount", ""),
            "commodity_number":    detail.get("CommNbr", ""),
            "commodity_desc":      detail.get("CommodityDesc", ""),
           "contact_info": {
                "contact_name":    detail.get("ContactName", ""),
                "contact_phone":   detail.get("ContactPhone", ""),
                "contact_email":   detail.get("ContactEmail", ""),
                
            },
            "pub_date":            self._parse_date(detail.get("BidOpenDate")),
            "pub_date_raw":        detail.get("BidOpenDate", ""),
            "closing_raw":         detail.get("BidCloseDate", ""),
            "closing_date":        self._parse_date(detail.get("BidCloseDate")),
            "open_continuous":     detail.get("OpenCont", ""),
            "last_changed_by":     detail.get("Lastchangedby", ""),
            "last_update_raw":     detail.get("LastUpdateDate", ""),
            "last_update_date":    self._parse_date(detail.get("LastUpdateDate")),
            "attachment_count":    detail.get("BidAttCount"),
            "amendment_count":     detail.get("BidAmendCount"),
            "amend_attachment_count": detail.get("BidAmdAttCount"),
            # Only populated on closed/awarded listing rows; absent (None)
            # on open-bid records.
            "award_count":         detail.get("AwardCount"),
            "nia_count":           detail.get("NIACount"),
            "documents":           documents,
            "detail_url": (
                f"{self.FRONTEND_BASE}/solicitation-search/"
                f"?type={search_type}&val={detail.get('BidNumber', '')}"
            ),
            "source":              "Los Angeles County - Doing Business With (Solicitations)",
            "scraped_at":          datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════════════════════════════
    

    def upload_to_s3(self, documents: list, teb_number: str, mongo_id) -> list:
        if not self.s3 or not documents:
            return documents

        folder  = f"{teb_number.replace('/', '_')}_{mongo_id}"
        updated = []

        for d in documents:
            url = d.get("original_url")
            if not url:
                # TODO #2 not filled in yet -- nothing to download.
                updated.append(d)
                continue
            try:
                dl_resp = self.session.get(
                    url, timeout=60, stream=True, allow_redirects=True,
                    headers={"Accept": "application/pdf,application/octet-stream,*/*;q=0.8"},
                )
                if dl_resp.status_code != 200 or not dl_resp.content:
                    self.logger.warning(f"  Doc download failed — skipping: {url}")
                    updated.append(d)
                    continue

                fname = d.get("file_name") or os.path.basename(url) or "attachment.pdf"
                key   = f"{self.s3_base_folder}/{folder}/{fname}"
                ext   = os.path.splitext(fname)[1].lower()
                mime_map = {
                    ".pdf": "application/pdf",
                    ".doc": "application/msword",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".xls": "application/vnd.ms-excel",
                    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
                self.s3.put_object(
                    Bucket      = self.s3_bucket,
                    Key         = key,
                    Body        = dl_resp.content,
                    ContentType = mime_map.get(ext, "application/octet-stream"),
                )
                d["s3_path"]     = f"s3://{self.s3_bucket}/{key}"
                d["uploaded_at"] = datetime.now(timezone.utc).isoformat()
                self.logger.info(f"  S3 ✓ {key}")

            except Exception as e:
                self.logger.error(f"  S3 upload failed {url}: {e}")

            updated.append(d)

        if self.use_storage and mongo_id:
            self.raw_col.update_one({"_id": mongo_id}, {"$set": {"documents": updated}})
        return updated

    def _save_json(self, records: list) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path  = os.path.join(self.output_dir, f"lacounty_ca_bids_{stamp}.json")

        def _serial(o):
            if isinstance(o, datetime):
                return o.isoformat()
            return str(o)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=_serial, ensure_ascii=False)
        self.logger.info(f"Saved {len(records)} record(s) → {path}")
        return path

    # ══════════════════════════════════════════════════════════════════════════
    
    def _scrape_one_status(self, bid_status: str, bid_ref_nbrs_override=None) -> list:
        """Runs the full prime -> listing -> per-bid (attachments, build,
        store) pipeline for a single bid_status ('open' or 'closed').
        Factored out so scrape() can call it once or twice depending on
        self.bid_status."""
        records = []

        self.prime_session(bid_status=bid_status)
        time.sleep(random.uniform(1, 2))

        if bid_ref_nbrs_override:
            stubs          = [{"BidRefNbr": b} for b in bid_ref_nbrs_override]
            stubs_are_full = False
        elif bid_status == "closed":
            stubs          = self.fetch_closed_listing()
            stubs_are_full = True
        else:
            stubs          = self.fetch_listing()
            stubs_are_full = True

        if self.max_bids:
            stubs = stubs[: self.max_bids]

        for idx, stub in enumerate(stubs, 1):
            bid_ref_nbr = stub.get("BidRefNbr")
            if not bid_ref_nbr:
                continue
            hash_id = self._hash(bid_ref_nbr, bid_status=bid_status)

            self.logger.info(f"[{bid_status}] {idx}/{len(stubs)} BidRefNbr={bid_ref_nbr}")

            try:
                if self.use_storage and self.raw_col.find_one({"hash_id": hash_id}):
                    self.logger.info("  Already in DB — skipping.")
                    continue

                if stubs_are_full:
                    detail = stub
                else:
                    detail = self.fetch_detail(bid_ref_nbr, bid_status=bid_status)
                if not detail:
                    self.logger.warning(f"  No detail returned for {bid_ref_nbr} — skipping.")
                    continue

                time.sleep(random.uniform(0.5, 1.0))
                documents = self.fetch_attachments(bid_ref_nbr, bid_status=bid_status)

                teb_no = self._teb()
                record = self._build_record(detail, documents, teb_no, bid_status=bid_status)

                if self.use_storage:
                    try:
                        res = self.raw_col.insert_one(record)
                        self.logger.info(f"  Stored in Mongo. TEB={teb_no} | _id={res.inserted_id}")
                    except DuplicateKeyError:
                        self.logger.info("  Race-condition duplicate — skipping.")
                        continue

                    if record.get("documents"):
                        record["documents"] = self.upload_to_s3(
                            record["documents"], teb_no, res.inserted_id
                        )

                records.append(record)

            except Exception as e:
                self.logger.error(f"  Error BidRefNbr={bid_ref_nbr}: {e}", exc_info=True)

            time.sleep(random.uniform(1.0, 2.0))

        self.logger.info(f"[{bid_status}] subtotal records scraped: {len(records)}")
        return records

    def scrape(self, bid_ref_nbrs_override=None) -> list:
        
        all_records = []

        statuses = ["open", "closed"] if self.bid_status == "both" else [self.bid_status]

        for status in statuses:
            all_records.extend(
                self._scrape_one_status(status, bid_ref_nbrs_override=bid_ref_nbrs_override)
            )

        self.logger.info(f"\nTotal records scraped: {len(all_records)}")

        if not self.use_storage:
            self._save_json(all_records)

        return all_records


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Scrape LA County solicitations/bids.")
    ap.add_argument("--no-db",          action="store_true",
                    help="Skip Mongo/S3, write local JSON instead.")
    ap.add_argument("--debug",          action="store_true",
                    help="Dump raw JSON of every API call to ./debug_json/")
    ap.add_argument("--output-dir",     default="output",
                    help="Directory for local JSON output (default: ./output)")
    ap.add_argument("--max-bids",       type=int, default=None,
                    help="Limit total bids processed per status (handy for quick tests).")
    ap.add_argument("--bid-status",     choices=["open", "closed", "both"], default="open",
                    help="Which solicitations to scrape: 'open' (default), "
                         "'closed' (awarded/closed bids), or 'both'.")
    ap.add_argument("--bid-ref-nbrs",   nargs="+", default=None,
                    help="Scrape these specific BidRefNbr values directly via "
                         "fetch_detail(), bypassing the listing endpoint. "
                         "Handy for testing a single bid, e.g.: "
                         "--bid-ref-nbrs 313145979568")
    args = ap.parse_args()

    scraper = LACountyBidScraper(
        use_storage = not args.no_db,
        debug       = args.debug,
        output_dir  = args.output_dir,
        max_bids    = args.max_bids,
        bid_status  = args.bid_status,
    )
    scraper.scrape(bid_ref_nbrs_override=args.bid_ref_nbrs)


if __name__ == "__main__":
    main()