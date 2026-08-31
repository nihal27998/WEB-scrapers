import os
import re
import sys
import pytz
import logging

from dateutil import parser as date_parser
from pymongo import MongoClient
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

# ============================================================
# LOGGER
# ============================================================

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("KLEKOON_ETL.log"),
        logging.StreamHandler(sys.stdout)])

logger = logging.getLogger(__name__)

# ============================================================
# ETL CLASS
# ============================================================

class TenderETL:

    CONTINENT = "NA"
    COUNTRY = "FRANCE" 

    def __init__(self,
        source_collection: str = "klekoon_awards",
        source_website: str = "https://www.klekoon.com/rechercher-une-annonce-ou-un-dce-dematerialise-sur-klekoon",
        db_name: str = "tender_bharo",
        target_collection_name: str = "main_tender_collection8"):

        self.source_collection = source_collection
        self.source_website = source_website
        uri = os.getenv("LOCAL_MONGO_URI")

        if not uri:
            raise ValueError("LOCAL_MONGO_URI not found")

        client = MongoClient(uri)
        self.db = client[db_name]
        self.raw_collection = self.db[source_collection]
        self.etl_collection = self.db[target_collection_name]
        logger.info(" KLEKOON CA ETL Initialized Successfully")

    # ========================================================
    # DATE PARSER
    # ========================================================
    def parse_date(self, val):
        if not val:
            return None

        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val

        try:
            dt = date_parser.parse(str(val).strip(), dayfirst=False)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt

        except Exception as e:
            logger.warning(f"Date parse failed: {val} | {e}")
            return None

    # ========================================================
    # SAFE FLOAT
    # ========================================================
    def safe_float(self, value):
        try:
            if value is None:
                return None
            cleaned = re.sub(r"[^\d.]","",str(value))
            if cleaned == "":
                return None
            return float(cleaned)
        except Exception:
            return None

    # ========================================================
    # GENERATE SLUG
    # ========================================================
    def generate_slug(self,title: str,doc_id: str,year: int):
        if not title:
            title = "tender"
        slug_base = title.lower()
        slug_base = ''.join(c if c.isalnum() or c.isspace()
            else '-'
            for c in slug_base)

        slug_base = '-'.join(slug_base.split())
        slug_base = re.sub(r'-+','-',slug_base)
        slug_base = slug_base.strip('-')
        slug_base = slug_base[:50]
        new_doc_id = str(doc_id)[9:]
        continent = self.CONTINENT
        country = self.COUNTRY.replace(" ","-")
        return (f"{continent}-{country}-{year}-{slug_base}-{new_doc_id}")

    # ========================================================
    # DOCUMENT LINKS
    # ========================================================
    def extract_document_links(self,documents):
        links = []
        if not documents:
            return links
        for doc in documents:
            url = doc.get("document_url")
            if url:
                links.append(url)
        return links

    # ========================================================
    # DOCUMENT PATHS
    # ========================================================
    def extract_document_paths(self,documents):
        paths = []
        if not documents:
            return paths
        for doc in documents:
            s3_path = doc.get("s3_path")
            if s3_path:
                paths.append(s3_path)
        return paths

    # ========================================================
    # TRANSFORM
    # ========================================================
    def transform(self, raw_data):
        import re
        
        year = datetime.now().year
        slug = self.generate_slug(raw_data.get("title"),str(raw_data.get("_id")),year)
        detail= raw_data.get("awardees",{})
        suplier_data = raw_data.get("supplier_details",{})
                
        return {
            "teb_number":raw_data.get("teb_number"),
            "tender_number":raw_data.get("de_id"),
            "tender_id":None,
            "tender_ra_number":None,

            "tender_title":raw_data.get("title"),
            "tender_description":raw_data.get("title"),
            "tender_category":None,
            "tender_bidding_type":"International Competitive Bidding (ICB)",

            "tender_type":"Contract Award",
            "tender_city":None,
            "tender_state":None,
            "tender_pincode":None,
            "tender_country":"FRANCE",

            "tender_organisation":raw_data.get("authority_name"),
            "tender_financier":"Self Financier",
            "tender_item_quantity":None,
            "tender_purchaser_ownership":"Public",
            "tender_value":raw_data.get("total_amount_ht"),
            "tender_emd":None,
            "tender_document_fees":None,
            "tender_purchaser_address": f"{raw_data.get('authority_name', '')}, {raw_data.get('authority_siret', '')}, {raw_data.get('place_of_performance', '')}",

            "tender_start_date":self.parse_date(raw_data.get("publication_date")),
            "tender_end_date":None,
            "tender_publishing_date":self.parse_date(raw_data.get("publication_date")),

            # "tender_documents_path": self.extract_document_paths(documents),
            "tender_documents_path": raw_data.get("documents"),
            "bid_document_links_extracted":None,
            "created_at":datetime.now(timezone.utc),

            "slug":slug,
            "source_tag":"KLEKOON",
            "source_id":self.source_website,
            "created_by":"Nihal",
            "scrapping_type":"Contract Award",

            "embedding_generated":False,
            "opensearch_index_generated":False,

            "bid_awarded_result": {
                "bid_details": {
                    "bid_number":raw_data.get("de_id"),
                    "status":"AWARDED",
                    "quantity":None,
                    "validity":None,
                    "start_date":self.parse_date(raw_data.get("publication_date")),
                    "end_date":None,
                    "opening_date":self.parse_date(raw_data.get("publication_date")),
                    "buyer_name": raw_data.get("authority_name"),
                    "buyer_address": "FRANCE ",
                    "ministry":None,
                    "department":None,
                    "organisation":None,
                    "office":None
                },

            "technical_evaluation": [
                {
                    "serial_no": None,
                    "seller_name":raw_data.get("awardees", [{}])[0].get("name"),
                    "offered_item":None,
                    "participated_on": None,
                    "status": "Awarded",
                    "emd_status": None,
                    "mii_status": None,
                    "mse_status": None,
                    "mii_verified": None,
                    "mse_verified": None,
                    "mse_mii_info": []
                }
            ],

            "financial_evaluation": 
                [
                    {
                        "serial_no": 1,
                        "seller_name": raw_data.get("awardees", [{}])[0].get("name"),
                        "offered_item": None,
                        "participated_on": None,
                        "total_price": raw_data.get("total_amount_ht"),
                        "rank": "L1",
                        "is_winner": "winner",
                        "status": "L1",
                        "currency": "EUR",
                        "seller_address":"FRANCE",
                        "seller_contact_number":"",
                        "seller_email_id":None,
                        "seller_country":"FRANCE",
                        "seller_registration_number":None,
                        "seller_tax_uid": None,
                        "seller_category": None,
                        "seller_gender": None,
                        "s3_contract_path": None
                    }
                ],
            "financial_evaluation_meta": {"alert": ""}
            },

            "llm_status":"pending",
            "llm_extracted_data": {
                "basic_info": {
                    "generated_title": raw_data.get("title"),
                    "summary":raw_data.get("title"),
                    "tender_type":None,
                    "main_category":None,
                    "sub_category":None,
                    "total_quantity":None,
                    "evaluation_method":None
                },

                "organization": {
                    "ministry": None,
                    "department": None,
                    "organisation_name":raw_data.get("authority_name"),
                    "office_name": None
                },

                "timeline": {
                    "bid_end_datetime":None,
                    "bid_open_datetime":None,
                    "bid_offer_validity_days":None,
                    "contract_period":None,
                    "delivery_days":None
                },

                "commercial": {
                    "type_of_bid":None,
                    "bid_to_ra_enabled":None,
                    "ra_qualification_rule":None,
                    "arbitration_clause":None,
                    "mediation_clause":None,
                    "evaluation_method":None
                },

                "financial": {
                    "estimated_bid_value": {
                        "amount": raw_data.get("total_amount_ht"),
                        "currency": "EUR",
                        "raw_text": None
                    },
                    "emd_details": [
                        {
                            "advisory_bank": None,
                            "schedule_reference": None,
                            "amount": {
                                "amount": None,
                                "currency": "EUR",
                                "raw_text": None
                            }
                        }
                    ],
                    "epbg_details": {
                        "advisory_bank": None,
                        "percentage": None,
                        "duration_months": None
                    }
                },
                "eligibility": {
                    "minimum_turnover": None,
                    "past_experience_required": None,
                    "documents_required": None,
                    "technical_documents_required": None
                },

                "preferences": {
                    "mii_purchase_preference": None,
                    "mii_percentage": None,
                    "mse_purchase_preference": None,
                    "mse_percentage": None,
                    "class_1_2_local_supplier_only": None
                },

                "schedules": [
                    {
                        "schedule_number": None,
                        "estimated_value": None,
                        "item_name": None,
                        "quantity": None,
                        "specifications":None,

                        "consignee_details": [
                            {
                                "reporting_officer": None,
                                "address": None,
                                "quantity": None,
                                "delivery_days": None
                            }
                        ]
                    }
                ],

                "compliance": {
                    "bis_required": None,
                    "certifications_required": None,
                    "test_reports_required": None
                },
                "declarations": None
            },
            "is_active":True,
            "bid_number":raw_data.get("de_id"),
            "version":0
        }

    # ========================================================
    # RUN ETL
    # ========================================================

    def run(self):
        raw_collection = self.db["klekoon_awards"]
        etl_collection = self.db["main_tender_collection8"]
        total_docs = (raw_collection.count_documents({}))

        pending_docs = (
            raw_collection.count_documents(
                {
                    "sent_to_main": {
                        "$ne": True
                    },
                    "etl_status":
                        "pending"
                }
            )
        )

        logger.info(f"TOTAL RAW DOCS: {total_docs}")
        logger.info(f"PENDING DOCS: {pending_docs}")
        docs = raw_collection.find(
            {
                "sent_to_main": {
                    "$ne": True
                },
                "etl_status":
                    "pending"
            }
        )

        logger.info("Starting ETL transformation")
        count = 0

        for raw_doc in docs:
            raw_id = raw_doc.get("_id")
            try:
                etl_doc = self.transform(raw_doc)
                result = (etl_collection.insert_one(etl_doc))

                if result.inserted_id:
                    raw_collection.update_one(
                        {
                            "_id": raw_id
                        },
                        {
                            "$set": {
                                "sent_to_main":
                                    True,
                                "etl_status":
                                    "completed"
                            }
                        }
                    )

                    count += 1
                    logger.info(f"ETL SUCCESS | {etl_doc.get('contract_number')}")

            except Exception as e:
                logger.error(f"ETL ERROR | ID: {raw_id} | {e}")

        logger.info(f"ETL completed | inserted: {count}")

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    TenderETL().run()