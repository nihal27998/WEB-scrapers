# Tender Bharo — Multi-Source Tender Scrapers

A scalable web-scraping and ETL system for collecting **tender and procurement data from multiple government and public procurement portals** across the USA and Europe.

The project automates the process of discovering tender opportunities, extracting structured information, cleaning the data, classifying tender records, and preparing them for storage and downstream analytics.

---

## 🚀 Overview

Government tender information is distributed across numerous procurement portals, each with different:

* Website structures
* Search mechanisms
* Pagination systems
* Data formats
* Authentication requirements
* Anti-bot protections
* Tender detail pages

**Tender Bharo** provides a collection of specialized scrapers designed to handle these differences while producing a standardized tender data structure.

### Current Scrapers

| Region      | Scrapers |
| ----------- | -------: |
| 🇺🇸 USA    |       22 |
| 🇪🇺 Europe |        7 |
| **Total**   |   **29** |

The scrapers can be executed independently or integrated into an ETL pipeline.

---

## 🏗️ Architecture

```text
                ┌─────────────────────┐
                │ Procurement Portals│
                │  USA + Europe      │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │     Web Scrapers    │
                │                     │
                │ BeautifulSoup       │
                │ Selenium            │
                │ Playwright          │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │   Data Extraction   │
                │                     │
                │ Tender ID           │
                │ Title               │
                │ Organization        │
                │ Location            │
                │ Deadline            │
                │ Value               │
                │ Description         │
                │ URL                 │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Data Cleaning & ETL │
                │                     │
                │ Validation          │
                │ Normalization       │
                │ Deduplication       │
                │ Classification      │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │      MongoDB        │
                │    Data Storage     │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Analytics /         │
                │ Tender Intelligence │
                │ Dashboard            │
                └─────────────────────┘
```

---

## ✨ Features

* 🌎 Multi-country tender scraping
* 🇺🇸 22 USA procurement scrapers
* 🇪🇺 7 Europe procurement scrapers
* 🔄 Automated pagination handling
* 📄 Tender-list and tender-detail extraction
* 🧹 Data cleaning and normalization
* 🔍 Duplicate detection
* 🏷️ Tender classification
* 🗄️ MongoDB integration
* ⚡ Support for static and dynamic websites
* 🛡️ Handling of modern browser-based websites
* 📊 Data preparation for analytics dashboards
* 🔧 Modular scraper architecture

---

## 🛠️ Technology Stack

### Web Scraping

* **Python**
* **BeautifulSoup**
* **Selenium**
* **Playwright**
* Requests / HTTP clients

### Data Processing

* **Pandas**
* **NumPy**
* Python data-processing utilities

### Database

* **MongoDB**
* MongoDB Atlas

### Data Pipeline

```text
Scrape
   ↓
Extract
   ↓
Validate
   ↓
Clean
   ↓
Normalize
   ↓
Classify
   ↓
Deduplicate
   ↓
Store
```

---

## 📂 Project Structure

```text
tender_bharo/
│
├── scrapers/
│   ├── usa/
│   │   ├── scraper_01.py
│   │   ├── scraper_02.py
│   │   └── ...
│   │
│   └── europe/
│       ├── scraper_01.py
│       ├── scraper_02.py
│       └── ...
│
├── etl/
│   ├── cleaning/
│   ├── transformation/
│   ├── validation/
│   └── classification/
│
├── database/
│   └── mongodb/
│
├── utils/
│   ├── logger.py
│   ├── helpers.py
│   └── config.py
│
├── requirements.txt
├── .env.example
└── README.md
```

> The exact directory structure may vary depending on the current implementation.

---

## 📋 Standard Tender Schema

Scrapers transform source-specific data into a common structure.

Example:

```json
{
  "tender_id": "TENDER-12345",
  "title": "IT Infrastructure Services",
  "organization": "Example Government Department",
  "country": "USA",
  "location": "New York",
  "tender_type": "Open",
  "description": "Procurement of IT infrastructure services.",
  "published_date": "2026-08-01",
  "deadline": "2026-09-15",
  "estimated_value": 500000,
  "currency": "USD",
  "source": "Procurement Portal",
  "source_url": "https://example.com/tender/12345"
}
```

The standardized schema allows tenders from different websites to be queried and analyzed consistently.

---

## 🔎 Scraping Strategies

Different procurement portals require different extraction strategies.

### 1. Static Websites

For pages where the tender data exists directly in the HTML:

```text
HTTP Request
     ↓
HTML Response
     ↓
BeautifulSoup
     ↓
Data Extraction
```

### 2. JavaScript-Based Websites

For dynamically rendered content:

```text
Browser
   ↓
JavaScript Execution
   ↓
Rendered Page
   ↓
Element Extraction
```

Playwright or Selenium can be used depending on the portal.

### 3. Pagination

Scrapers can iterate through multiple result pages:

```text
Page 1
  ↓
Page 2
  ↓
Page 3
  ↓
...
  ↓
Last Page
```

### 4. Detail-Page Extraction

Where the listing page contains limited information:

```text
Tender Listing
      ↓
Tender URL
      ↓
Detail Page
      ↓
Complete Tender Data
```

---

## 🧹 Data Processing

Raw scraped data is processed before being stored.

### Cleaning

* Remove unnecessary whitespace
* Normalize text
* Standardize dates
* Normalize currency values
* Handle missing fields
* Clean HTML content

### Validation

Records are checked for required fields such as:

* Tender ID
* Tender title
* Source
* URL
* Publication date
* Deadline

### Deduplication

Duplicate records can occur when:

* The same tender appears on multiple pages
* A scraper is executed repeatedly
* A tender is updated by the source portal

Deduplication is therefore performed before final storage.

---

## 🏷️ Tender Classification

The pipeline can classify tender records into standardized categories based on available tender information.

Example:

```text
Raw Tender
     ↓
Title + Description
     ↓
Classification Logic / Model
     ↓
Tender Category
```

This makes the collected data more useful for **tender discovery, filtering, analytics, and business intelligence**.

---

## 🗄️ MongoDB Storage

Processed tender records are stored in MongoDB.

Example collection:

```text
tender_bharo
     │
     ├── tenders
     ├── classified_tenders
     └── scraper_logs
```

MongoDB is used because tender records can vary considerably between procurement sources and may contain source-specific fields.

---

## ⚙️ Installation

Clone the repository:

```bash
git clone <YOUR_REPOSITORY_URL>
cd tender_bharo
```

Create a virtual environment:

```bash
python3 -m venv venv
```

Activate it:

### macOS / Linux

```bash
source venv/bin/activate
```

### Windows

```bash
venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

If Playwright is used:

```bash
playwright install
```

---

## 🔐 Environment Variables

Create a `.env` file:

```env
MONGODB_URI=your_mongodb_connection_string
MONGODB_DATABASE=tender_bharo

# Optional API / classification credentials
API_KEY=your_api_key
```

Never commit `.env` or credentials to GitHub.

Add this to `.gitignore`:

```gitignore
.env
venv/
__pycache__/
*.pyc
```

---

## ▶️ Running a Scraper

Run an individual scraper:

```bash
python scrapers/usa/scraper_name.py
```

or:

```bash
python scrapers/europe/scraper_name.py
```

Run the ETL pipeline:

```bash
python etl/main.py
```

> Replace the paths above with the actual entry-point filenames in the repository.

---

## 📊 Example Pipeline Output

```text
Starting scraper...

Source: Example Procurement Portal
Pages processed: 25
Tender links found: 1,240
Tender details extracted: 1,180

Cleaning data...
Valid records: 1,145
Duplicates removed: 35

Classification...
Records classified: 1,145

Saving to MongoDB...
Successfully inserted: 1,145

Scraping completed.
```

---

## 🛡️ Challenges

The project involves working with real-world procurement websites, which introduces several challenges:

### Dynamic Content

Some websites load tender information through JavaScript rather than returning it directly in the initial HTML response.

### Anti-Bot Protection

Some portals implement technologies such as:

* CAPTCHA
* Cloudflare
* Turnstile
* Rate limiting
* Browser fingerprinting

Scrapers therefore need appropriate browser automation, request handling, and retry strategies.

### Inconsistent Data

Different sources use different names and formats for the same fields.

For example:

```text
Closing Date
Deadline
Bid Due Date
Submission Deadline
```

These must be mapped into a common schema.

### Website Changes

Procurement websites can change their HTML structure without notice. Scrapers therefore need monitoring and periodic maintenance.

---

## 📈 Scalability

The architecture is designed to allow additional procurement sources to be added without rewriting the entire pipeline.

A new source can follow the general pattern:

```text
New Portal
    ↓
New Scraper
    ↓
Standard Schema
    ↓
Existing ETL Pipeline
    ↓
MongoDB
    ↓
Analytics
```

This allows the system to scale from a small number of sources to a larger multi-country procurement intelligence platform.

---

## 🔮 Future Improvements

* [ ] Centralized scraper scheduler
* [ ] Automatic scraper health monitoring
* [ ] Retry and failure management
* [ ] Proxy rotation where legally appropriate
* [ ] Improved duplicate detection
* [ ] Automated schema validation
* [ ] Incremental scraping
* [ ] Scraper performance monitoring
* [ ] More procurement portals
* [ ] Advanced tender classification
* [ ] Full-text search
* [ ] Tender recommendation system
* [ ] Real-time analytics dashboard

---

## 📊 Intended Use

The collected data can be used for:

* Tender discovery
* Procurement analytics
* Market intelligence
* Opportunity identification
* Tender classification
* Government procurement research
* Business intelligence dashboards

---

## ⚠️ Responsible Scraping

This project is intended for legitimate data collection and research purposes.

When running the scrapers:

* Respect the target website's Terms of Service.
* Respect applicable robots.txt policies where relevant.
* Follow applicable laws and regulations.
* Avoid excessive request rates.
* Do not attempt to bypass authentication or access controls.
* Do not collect sensitive personal information unnecessarily.
* Respect CAPTCHA and anti-bot mechanisms rather than attempting unauthorized circumvention.

---

## 👨‍💻 Author

**Nihal Khedekar**

Computer Engineering
Data Science & Analytics | Python | Web Scraping | ETL | MongoDB

---

## ⭐ Project Highlights

```text
29 Scrapers
   │
   ├── 22 USA
   └── 7 Europe

Python
   ↓
BeautifulSoup / Selenium / Playwright
   ↓
ETL + Data Cleaning
   ↓
Classification
   ↓
MongoDB
   ↓
Tender Intelligence
```

This project demonstrates practical experience in **web scraping, browser automation, data engineering, ETL pipelines, NoSQL databases, and analytics-oriented data processing**.
