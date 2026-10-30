# Vendor Finder: AI Vendor Search and Lead Intake Chatbot

A chatbot for a rental company's sales agents to use while they're on a call with a customer. It can:

- **Find vendors** that deliver a product (dumpsters, porta potties, hand wash stations, holding tanks,
  washout tanks, fencing and more) to a US zip code or city. Prices, zone up-charges and road miles match
  what the existing **Search Vendor** widget shows.
- **Look up one vendor** by name and explain why it does or doesn't appear in a search.
- **Answer free-form questions** about the vendor database ("how many vendors carry 30 yard dumpsters in
  GA?") by writing read-only SQL.
- **Take down lead details** typed quickly and out of order ("john smth 3312654343 jon@gmial.com 10yd
  dumpster tmrw 4521 park rd charlotte"). It puts them in order, checks them, and lists what's still
  missing.

You can type to it or speak to it. Voice input is transcribed with Whisper.

---

## Contents

- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running](#running)
- [Example prompts](#example-prompts)
- [Tools the agent uses](#tools-the-agent-uses)
- [Search and pricing rules](#search-and-pricing-rules)
- [Lead intake](#lead-intake)
- [Database](#database)
- [Configuration reference](#configuration-reference)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## How it works

```
 Streamlit UI (chatbot.py)  ── or ──  terminal REPL (python main.py)
            │  text, or voice → Whisper (Groq)
            ▼
   LangChain agent (main.py: load_agent / ask)
   LLM on Groq (LLM_MODEL), system prompt from prompt.py + today's date
            │
            ├── search_vendors ─┐
            ├── check_vendor  ──┼──► PostgreSQL  primvmon_zoho_vendors
            ├── review_lead  ───┘       (vendor, zoning, product and zip tables)
            └── run_sql ──────────► same DB through a read-only login
                     │
                     ├── Google Geocoding API (search origin for a zip, address checks)
                     └── Distance service (live road miles, as search_vendor_osrm.php uses)
```

- The agent is built with LangChain `create_agent` on a Groq-hosted model at temperature 0.
- `search_vendors`, `check_vendor` and `review_lead` are **`return_direct`** tools. Their formatted
  answer goes straight to the user, so each search or lead update costs **one LLM call**. Only `run_sql`
  hands its rows back to the model for a written answer.
- **Chat history is trimmed.** Only the last 12 messages are sent, and older vendor lists are reduced to
  their header line. The latest list is sent in full, so follow-ups like "the 2nd one's phone" or
  "cheapest under $500" can be answered without a new search.
- Every answer ends with a token-usage line (`Tokens used: … in · … out, N LLM calls`).
- If Groq returns a malformed tool call (`tool_use_failed`), the question is retried once.

## Project layout

| Path | Purpose |
|---|---|
| [chatbot.py](chatbot.py) | Streamlit web UI: chat window, voice input (Whisper on Groq), `$` escaping for markdown |
| [main.py](main.py) | Agent setup, the `search_vendors` / `check_vendor` / `run_sql` tools, the search and pricing logic, and a terminal REPL |
| [lead.py](lead.py) | The `review_lead` tool: parses, checks and formats a customer's lead details |
| [prompt.py](prompt.py) | System prompt: when to use which tool, the database schema, and how to read lead details |
| [query.py](query.py) | SQL for the search (a Postgres port of `search_vendor_osrm.php` and the Zoho `searchv3duplicate` COQL) |
| [db/schema.sql](db/schema.sql) | Postgres schema converted from the MariaDB/phpMyAdmin dump |
| [db/indexes.sql](db/indexes.sql) | Keys and indexes, created after the data load |
| [db/convert_dump.py](db/convert_dump.py) | Converts the dump's `INSERT` data to Postgres SQL |
| [db/import_saleshub.py](db/import_saleshub.py) | Copies `vendor_products` and `vendor_details` from the SalesHub DB (a Zoho CRM mirror) |
| [db/import_zip_counties.py](db/import_zip_counties.py) | Loads zip → county from the US Census 2020 ZCTA-to-county file |
| [db/create_readonly_role.py](db/create_readonly_role.py) | Creates the `vendor_reader` login for `run_sql` and writes its URL to `.env` |
| [requirements.txt](requirements.txt) | Python dependencies |
| [.env.example](.env.example) | Template for `.env` |

## Requirements

- Python 3.12 (other 3.10+ versions should work)
- PostgreSQL with the `primvmon_zoho_vendors` database (see [Database](#database))
- A [Groq](https://console.groq.com) API key for the LLM and Whisper
- A Google Maps API key with the **Geocoding API** enabled. This one is optional: without it, searches
  fall back to the zip's centroid, and lead addresses can't be checked.
- To refresh vendor data: access to the SalesHub Postgres (a local Docker DB from `pd-saleshub`)

Python packages ([requirements.txt](requirements.txt)): `langchain`, `langchain-groq`, `groq`,
`psycopg[binary]`, `python-dotenv`, `streamlit`.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/AnsRehman7/AI-DBchatbot.git
cd AI-DBchatbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure the environment

```bash
cp .env.example .env
```

Fill in at least `LLM_API_KEY` and `LLM_MODEL`. All variables are described in the
[Configuration reference](#configuration-reference).

The main database password isn't kept in `.env`. Put it in `~/.pgpass` instead:

```bash
echo "localhost:5432:primvmon_zoho_vendors:postgres:<password>" >> ~/.pgpass
chmod 600 ~/.pgpass
```

### 3. Build the database

```bash
createdb primvmon_zoho_vendors

# Schema, data from the phpMyAdmin/MariaDB dump, then keys and indexes, in one transaction
(cat db/schema.sql; python3 db/convert_dump.py dump.sql; cat db/indexes.sql) \
    | psql -d primvmon_zoho_vendors -v ON_ERROR_STOP=1 --single-transaction -q

# Vendor products and vendor details from SalesHub (re-run any time to refresh)
python db/import_saleshub.py

# Zip → county from the US Census (used for county service zones)
python db/import_zip_counties.py

# Read-only login for run_sql; writes READONLY_DATABASE_URL into .env
python db/create_readonly_role.py
```

`convert_dump.py` prints per-table row counts to stderr so you can check the load.
`import_saleshub.py` rebuilds both of its tables in a single transaction. Re-running
`create_readonly_role.py` sets a new password and updates `.env`.

## Running

**Web UI**

```bash
streamlit run chatbot.py
```

Open the URL Streamlit prints (usually http://localhost:8501). Type a request, or use the microphone
button in the chat box.

**Terminal**

```bash
python main.py
```

Type `exit` or `quit` to leave.

## Example prompts

| You type | What happens |
|---|---|
| `20 yard dumpster for 40056 within 50 miles` | Vendor search with prices and road miles |
| `porta potty 28021` | Asks which porta potty (construction, event, handicap, …) |
| `portable toilet for an event in Charlotte, NC` | Asks for the zip, because Charlotte has many |
| `what about 75 miles` / `show notes` / `show all vendors` | Repeats the search with the change |
| `which is cheapest?` / `phone of the 2nd one` | Answered from the latest results, no new search |
| `why isn't ADM Rolloff LLC listed?` | `check_vendor` explains the deciding fact |
| `how many active vendors carry 30 yard dumpsters in GA?` | `run_sql` writes and runs a SELECT |
| `john smth 3312654343 jon@gmial.com 10yd dumpster tmrw 4521 park rd charlotte` | Lead table, checked, with missing fields listed |
| `go with 2` | Adds vendor #2 of the latest search to the lead |
| `last name Smyth, homeowner, 2 weeks` | Updates the lead |

## Tools the agent uses

### `search_vendors`
Finds vendors that deliver a product to a location and shows them in the same way as the Search Vendor
widget.

- **Product matching.** Matching is fuzzy and works word by word: `10yd` → `10 yard`, `potties` →
  `potty`, and typos are corrected against the product vocabulary. If more than one product matches,
  you're asked which one ("Which porta potty do you need?"). If nothing matches, you get "Did you
  mean…" suggestions.
- **Location.** You can give a 5-digit zip, or a city and state. A city with several zips is never
  guessed, because prices and distances depend on the exact zip.
- **Options.** `radius_miles` (25 / 50 / 75), `show_notes`, and `all_vendors` (also lists vendors who
  have no price yet).

### `check_vendor`
Finds one vendor by name, ignoring punctuation and word order. It reports the vendor's status (active,
do-not-use, exclusions). Given a product, location and radius, it also says whether the vendor would
appear in that search and why: out of range, no zone, cross-state rule, product doesn't qualify, and
so on.

### `run_sql`
Runs one read-only `SELECT` through the `vendor_reader` login. It returns at most 50 rows and 8,000
characters, and each value is cut at 200 characters. The model fixes and retries failed queries. Prices
in the tables don't include zone up-charges, and stored miles aren't live, so any price or distance for
a particular location always comes from `search_vendors`.

### `review_lead`
See [Lead intake](#lead-intake).

## Search and pricing rules

The search reproduces the live widget's two stages: the PHP endpoint `search_vendor_osrm.php` and the
Zoho Deluge function `searchv3duplicate`.

1. **Search origin.** Google's geocoded point, state and county for the zip, which is what the widget
   sends. Each lookup is saved in `zip_google_points`. When Google can't be reached, the search uses the
   zip's centroid from `zip_centroids` and the county from `zip_counties`.
2. **Candidate vendors** ([query.py](query.py) `CANDIDATES_SQL`). A vendor qualifies through any of the
   PHP's three branches:
   - **distance:** straight-line miles within the radius, or the vendor's saved driving miles inside a
     zone A/B/C mileage range
   - **zip list:** the zip appears in a zone's serviced-zip list
   - **county list:** the county appears in a zone's county list. This only counts for vendors in the
     customer's state, because county names repeat across states.

   Vendors whose review notes say "do not use" are excluded.
3. **Live road miles.** These come from the distance service. When it fails, the search falls back to
   saved or straight-line miles, as the PHP does.
4. **Product offers** (`PRODUCT_OFFERS_SQL`). A product qualifies when all of these hold:
   - `product_availability` is `Hauler Carries` or `No Data Available`
   - the product is active and not a duplicate
   - the vendor is active and not marked do-not-use
   - `unit_price > 0`, except in the "all vendors" view

   Hot-list vendors come first, and results are capped at the widget's limit of 100.
5. **Pricing.**
   - The zone up-charge is added to the base price.
   - Construction and event porta potties have a $49 minimum delivery fee and a $199 minimum price
     ($249 when there's no delivery fee).
   - Dumpsters get default tons included (1 ton for 10–20 yard, 2 tons for 30–40 yard), a $199 extra-ton
     price, and 7 rental days with $35 per extra day, unless the vendor sets its own values.
   - Rounding matches PHP and Deluge (halves round away from zero).
6. **Zoning task type.** This comes from the product category: Porta Potty, Dumpster, Fencing, Washout
   or Holding Tank.

## Lead intake

The agent types the customer's details as fast as the customer says them. The model sorts the pieces
into fields, using the rules in [prompt.py](prompt.py), and calls `review_lead`. The tool then checks
and formats them:

| Field | Check |
|---|---|
| First / last name | Capitalized if typed all lower or upper case; spelling is never changed |
| Phone / best number | 10 US digits (11 with a leading 1), formatted `704-555-1234`; flags impossible area codes. Best number defaults to the phone |
| Email | Valid format; flags a domain one typo away from a common provider (`gmial.com` → `gmail.com`) |
| Customer type | Homeowner, Business, Contractor, Property Manager, Government, Other |
| Date needed / backup date | Real date, not in the past, not over a year ahead; backup ≠ date needed |
| Rental duration | Days ("2 weeks" → 14, "a month" → 30) |
| Product / category | Matched against real product names; asks for the size when only a category is given |
| Address | Geocoded with Google. Flags a street, city, state or zip that doesn't match, and a zip that isn't in our zip table. When nothing is flagged, Google's full address is used, so a missing city or state gets filled in |
| Vendor | Must be a real vendor; shows its status (inactive, do-not-use, …) |
| Lead type | Live Call (default), Voicemail, Web Form, Email, Text, Chat, Spam |
| Test lead | Marked `TEST LEAD` when the agent says it's a test |
| Description | Free-text order details (quantity, placement, gate code, …) |

The tool's answer has three parts: the lead as a table, a **Missing** list, and a **Please check** list
for flagged values. Every later message is applied on top of the latest table. Spam leads don't need
the other fields. Pieces of text that the model can't place go into an "unclear" list, so nothing is
silently dropped.

## Database

PostgreSQL database `primvmon_zoho_vendors`. Vendor ids are the same in every vendor table.

| Table | Source | Contents |
|---|---|---|
| `vendor_details` | SalesHub (Zoho Vendors) | One row per vendor: name, phone, status flags, hot list, exclusions, notes, rental terms |
| `vendor_products` | SalesHub (Zoho Products) | One row per vendor product: type, category, availability, price, delivery fee, tons |
| `vendors_list_new` | MariaDB dump | Vendor locations (address, lat/lng), hot list, `search_criteria` |
| `vendor_zoning` | MariaDB dump | Service zones per task type: A/B/C mileage ranges, up-charges, zip and county lists, cross-state rules |
| `vendor_zip_driving_distance` | MariaDB dump | Saved driving miles from vendor to zip |
| `zip_centroids` | MariaDB dump | Zip → lat/lng, city, state |
| `zip_counties` | US Census 2020 | Zip → county (the county covering most of the zip's land) |
| `zip_google_points` | Created at runtime | Cached Google geocode of each searched zip |
| `app_*` | MariaDB dump | Login tables of the old web app. Not readable by `vendor_reader` |

The full column list the model works from is in [prompt.py](prompt.py).

## Configuration reference

All variables are read from `.env`. See [.env.example](.env.example).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LLM_API_KEY` | yes | — | Groq API key, used for the chat model and Whisper |
| `LLM_MODEL` | yes | — | Groq model id, e.g. `openai/gpt-oss-120b` |
| `DATABASE_URL` | no | `postgresql://postgres@localhost:5432/primvmon_zoho_vendors` | Main DB. Password comes from `~/.pgpass` |
| `READONLY_DATABASE_URL` | for `run_sql` | — | Written by `db/create_readonly_role.py` |
| `MAPS_API_KEY` | recommended | — | Google Geocoding, for the search origin and address checks |
| `DISTANCE_SERVICE_URL` | no | `http://13.58.151.6` | Driving-distance service (`/distance.php`) |
| `SALESHUB_DATABASE_URL` | for import | `postgresql://saleshub_admin:saleshub@localhost:5533/saleshub` | Source for `db/import_saleshub.py` |

## Security notes

- `.env` is git-ignored. Never commit API keys or passwords.
- The model's SQL runs only through `vendor_reader`. That login can read only the vendor and geography
  tables, every transaction is read-only, and queries time out after 15 seconds. The connection is also
  set to `read_only` in code.
- `search_vendors`, `check_vendor` and `review_lead` use fixed, parameterized SQL. The model never
  writes SQL for them.
- The system prompt tells the model never to make up vendors, prices or customer details: every fact has
  to come from a tool, from earlier results, or from what the agent typed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `run_sql isn't set up` | Run `python db/create_readonly_role.py` |
| `fe_sendauth: no password supplied` | Add the DB password to `~/.pgpass` (mode 600) |
| Prices differ slightly from the widget | Check `MAPS_API_KEY`. Without it the origin is the zip centroid, and the answer notes this |
| Miles look like straight-line distances | The distance service is down. The search falls back the same way the PHP does |
| "I couldn't find zip code …" | The zip isn't in `zip_centroids`; check it or reload the dump |
| County-zone vendors missing | Run `python db/import_zip_counties.py` |
| Occasional `tool_use_failed` error | It's retried once automatically. If it keeps happening, try another `LLM_MODEL` |
| Voice: "Could not read the recording" | Check `LLM_API_KEY` and microphone permission in the browser |
