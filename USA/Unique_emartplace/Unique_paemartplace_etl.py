from datetime import datetime, timezone
from dateutil import parser
from typing import Dict, Any
from pymongo import MongoClient, ReturnDocument
import os
import logging
import sys
import re

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("PA_Marketplace_tender_processor.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class ETL:
    def __init__(self,
                 tender_issuing_entity="Pennsylvania Department of General Services (DGS)",
                 source_website="https://www.emarketplace.state.pa.us/",
                 db_name: str = "tender_bharo",
                 source_collection_name: str = "pa_emarketplace_tenders",
                 target_collection_name: str = "main_tender_collection8"):

        self.source_website = source_website
        self.tender_issuing_entity = tender_issuing_entity

        uri = os.getenv("LOCAL_MONGO_URI")
        if not uri:
            raise ValueError("LOCAL_MONGO_URI not found in environment variables")

        client = MongoClient(uri)
        self.db = client[db_name]

        if not source_collection_name:
            raise ValueError("source_collection_name cannot be None")

        self.raw_collection = self.db[source_collection_name]
        self.etl_collection = self.db[target_collection_name]

        self.raw_collection.create_index("etl_status")
        logger.info("ETL Initialized Successfully")

    # ---------------- DATE ----------------
    def parse_date(self, date_str):
        if not date_str:
            return None
        try:
            cleaned = re.sub(r"\s+", " ", str(date_str)).strip()
            try:
                dt = datetime.strptime(cleaned, "%m/%d/%Y %I:%M:%S %p")
            except ValueError:
                dt = parser.parse(cleaned)

            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception as e:
            logger.error(f"Date parse failed for '{date_str}': {e}")
            return None

    # ---------------- TRANSFORM ----------------
    def transform(self, source_doc: Dict[str, Any]) -> Dict[str, Any]:
        documents = source_doc.get("documents") or []
        clean_documents = []

        for doc in documents:
            if doc and isinstance(doc, dict):
                clean_documents.append({
                    "type": doc.get("type"),
                    "title": doc.get("title"),
                    "original_url": doc.get("original_url"),
                    "s3_path": doc.get("s3_path"),
                    "uploaded_at": doc.get("uploaded_at")
                })

        subject = source_doc.get("solicitation_title", "")

        target_doc = {
            "teb_number": source_doc.get("teb_number"),
            "tender_number": source_doc.get("solicitation_number"),
            "tender_id":None,
            "tender_ra_number": None,
            "tender_title": subject.lower().capitalize(),
            "tender_description":source_doc.get("description"),
            "tender_category": None,
            "tender_bidding_type": "International Competitive Bidding (ICB)",
            "tender_type": "OPEN",
            "tender_financier": "Self Financier",
            "tender_item_quantity": None,
            "tender_purchaser_ownership": "Public",
            "tender_value": None,
            "tender_emd": None,
            "tender_document_fees": None,
            "tender_pincode": None,
            "tender_purchaser_address": source_doc.get("delivery_location"),
            "tender_start_date": self.parse_date(source_doc.get("start_date")),
            "tender_end_date": self.parse_date(source_doc.get("due_date")),
            "tender_publishing_date": self.parse_date(source_doc.get("start_date")),
            "tender_organisation": "Pennsylvania Department of General Services (DGS)",
            "tender_documents_path": clean_documents if clean_documents else None,
            "is_active": True,
            "bid_number": None,
            "version": 0,
            "created_at": source_doc.get("created_at"),
            "created_by": "Nihal",
            "bid_document_links_extracted": None,
            "embedding_generated": False,
            "opensearch_index_generated": False,
            "llm_status": "pending",
            "source_tag": "PA-EMARKETPLACE",
            "source_id": self.source_website,
            "tender_city": None,
            "tender_state": None,
            "tender_country": "USA"
        }

        target_doc["llm_extracted_data"] = self._build_llm_data(source_doc)
        target_doc["slug"] = self.generate_slug(subject, str(source_doc.get("_id")))

        return target_doc

    # ---------------- LLM ----------------
    def _build_llm_data(self, source_doc: Dict) -> Dict[str, Any]:
        return {
            "basic_info": {
                "generated_title": source_doc.get("solicitation_title"),
                "summary": source_doc.get("description"),
                "tender_type": "OPEN",
                "main_category": None,
                "sub_category": None,
                "total_quantity": None,
                "evaluation_method": None,
                "msme_exemption_for_yoe_and_turnover": None,
                "startup_exemption_for_yoe_and_turnover": None,
                "past_performance_percentage": None
            },
            "organization": {
                "ministry": None,
                "department": None,
                "organisation_name": "Pennsylvania Department of General Services (DGS)",
                "office_name": None
            },
            "timeline": {
                "bid_end_datetime": self.parse_date(source_doc.get("due_date")),
                "bid_open_datetime": self.parse_date(source_doc.get("due_date")),
                "bid_offer_validity_days": None,
                "contract_period": None,
                "delivery_days": None
            },
            "commercial": {
                "type_of_bid": None,
                "bid_to_ra_enabled": None,
                "ra_qualification_rule": None,
                "arbitration_clause": None,
                "mediation_clause": None,
                "evaluation_method": None
            },
            "financial": {
                "estimated_bid_value": None,
                "emd_details": None,
                "epbg_details": None
            },
            "eligibility": {
                "minimum_turnover": None,
                "past_experience_required": None,
                "documents_required": [],
                "technical_documents_required": [],
            },
            "preferences": {
                "mii_purchase_preference": None,
                "mii_percentage": None,
                "mse_purchase_preference": None,
                "mse_percentage": None,
                "class_1_2_local_supplier_only": None
            },
            "schedules": None,
            "compliance": {
                "bis_required": None,
                "certifications_required": None,
                "test_reports_required": None
            },
            "declarations": None
        }

    # ---------------- SLUG ----------------
    def generate_slug(self, title, doc_id):
        if not title:
            title = "tender_sdbuynet"
        slug = title.lower().replace("/", "-")
        slug = re.sub(r'[^a-z0-9\s-]', '-', slug)
        slug = re.sub(r'\s+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-')[:50]
        return f"{slug}-{doc_id}"

    # ---------------- RUN ----------------
    def run(self):
        logger.info("ETL Run Started...")
        while True:
            logger.info("Checking for pending documents...")
            doc = self.raw_collection.find_one_and_update(
                {"etl_status": {"$in": ["pending", "Processing"]}},
                {"$set": {"etl_status": "Processing"}},
                return_document=ReturnDocument.AFTER
            )

            if not doc:
                logger.info("No more documents to process.")
                break

            doc_id = doc.get("_id")
            try:
                transformed_doc = self.transform(doc)
                self.etl_collection.insert_one(transformed_doc)
                self.raw_collection.update_one(
                    {"_id": doc_id},
                    {"$set": {"etl_status": "done"}}
                )
                logger.info(f"Processed document: {doc_id}")
            except Exception as e:
                logger.exception(f"Error processing document {doc_id}")
                self.raw_collection.update_one(
                    {"_id": doc_id},
                    {"$set": {"etl_status": "Failed", "error": str(e)}}
                )

if __name__ == "__main__":
    ETL().run()