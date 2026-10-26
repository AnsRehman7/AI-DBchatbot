import difflib
import functools
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from prompt import SYSTEM_PROMPT
from query import (CANDIDATES_SQL, PRODUCT_OFFERS_SQL, ZONING_RECORDS_SQL, ZIP_POINTS_TABLE_SQL, ZIP_POINT_SQL,
                   SAVE_ZIP_POINT_SQL, ZIP_CENTROID_SQL, PRODUCTS_SQL, CITY_ZIPS_SQL, VENDOR_NAME_SQL,
                   VENDOR_PRODUCT_SQL, VENDOR_ZONING_SQL)

load_dotenv()
GROQ_API_KEY = os.environ.get("LLM_API_KEY")
groq_model_name = os.environ.get("LLM_MODEL")
MAPS_API_KEY = os.environ.get("MAPS_API_KEY")
# Password comes from ~/.pgpass, so the URL has none
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/primvmon_zoho_vendors")
# Read-only login for run_sql, made by db/create_readonly_role.py
READONLY_DB_URL = os.environ.get("READONLY_DATABASE_URL")
# Driving distances, the service search_vendor_osrm.php calls
DISTANCE_SERVICE_URL = os.environ.get("DISTANCE_SERVICE_URL", "http://13.58.151.6").rstrip("/")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
METERS_TO_MILES = 0.000621371
# The widget asks the PHP and its Zoho queries for at most this many rows
WIDGET_LIMIT = 100

DUMPSTER_FIELDS = ("tons_included", "extra_ton_price", "rental_days", "extra_day_price")
DUMPSTER_TONS_INCLUDED = {
    "10 Yard Dumpster": 1, "15 Yard Dumpster": 1, "20 Yard Dumpster": 1,
    "30 Yard Dumpster": 2, "40 Yard Dumpster": 2,
}
# The zip table mostly spells these out ("Saint Louis")
CITY_ABBREVIATIONS = {"st": "saint", "mt": "mount", "ft": "fort"}
# Words users type -> the spelling used in product names
WORD_ALIASES = {"yd": "yard", "yds": "yard", "yards": "yard", "potties": "potty"}
# Zoning task type for a product category, normalized like search_vendor_osrm.php
ZONING_TASK_TYPES = [("Porta Potty", "Porta Potty"), ("Dumpster", "Dumpster"), ("Fenc", "Fencing"),
                     ("Washout", "Washout"), ("Holding Tank", "Holding Tank")]
# Google's county names carry a suffix that vendor county lists leave out ("Gaston County" -> "Gaston")
COUNTY_SUFFIXES = (" County", " Parish", " City and Borough", " Borough", " Census Area", " Municipality")
# Match sources of search_vendor_osrm.php, and the labels searchv3duplicate shows for them
MILEAGE_MATCHES = ("service_by_zip_a", "service_by_zip_b", "service_by_zip_c")
ZIP_COUNTY_MATCHES = ("service_by_zip_za", "service_by_zip_zb", "service_by_zip_zc",
                      "service_by_county_za", "service_by_county_zb", "service_by_county_zc")
MATCH_LABELS = {
    "distance": "Distance",
    "service_by_zip_a": "Service by Miles (Zone A)",
    "service_by_zip_b": "Service by Miles (Zone B)",
    "service_by_zip_c": "Service by Miles (Zone C)",
    "service_by_zip_za": "Service by Zip (Zone A)",
    "service_by_zip_zb": "Service by Zip (Zone B)",
    "service_by_zip_zc": "Service by Zip (Zone C)",
    "service_by_county_za": "Service by County (Zone A)",
    "service_by_county_zb": "Service by County (Zone B)",
    "service_by_county_zc": "Service by County (Zone C)",
}
MAX_PRODUCT_OPTIONS = 20
# Messages of chat history sent with each question (6 questions and answers)
MAX_HISTORY = 12
# run_sql result size sent back to the model
MAX_SQL_ROWS = 50
MAX_SQL_CHARS = 8000
# Vendor details shown when the vendor has them
DETAIL_FORMATS = [
    ("delivery_fee", "delivery ${:,.2f}"),
    ("tons_included", "{:g} tons incl."),
    ("rental_days", "{:g} days incl."),
    ("extra_ton_price", "extra ton ${:,.2f}"),
    ("extra_day_price", "extra day ${:,.2f}"),
]


def php_round(value, digits=0):
    """PHP's and Deluge's round(): halves away from zero."""
    return float(Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-digits), ROUND_HALF_UP))


def leading_int(text):
    """PHP's (int) and MySQL's CAST AS UNSIGNED of text: the leading digits, else 0."""
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else 0


def apply_price_rules(row, product):
    """Price floors and dumpster defaults that searchv3duplicate applies before showing a price."""
    if product.startswith(("Construction", "Event")) and "Porta Potty" in product:
        if 0 < (row["delivery_fee"] or 0) < 49:
            row["delivery_fee"] = 49
        if row["price"]:
            row["price"] = max(row["price"], 199 if row["delivery_fee"] else 249)
    if "Dumpster" not in product:
        row.update(dict.fromkeys(DUMPSTER_FIELDS))
    elif product in DUMPSTER_TONS_INCLUDED:
        row["tons_included"] = row["tons_included"] or DUMPSTER_TONS_INCLUDED[product]
        row["extra_ton_price"] = row["extra_ton_price"] or 199
        if not row["rental_days"]:
            row["rental_days"], row["extra_day_price"] = 7, 35
    return row


def city_keys(city):
    """City spellings to look up: as typed, and with St/Mt/Ft spelled out."""
    words = city.lower().replace(".", " ").split()
    spelled_out = [CITY_ABBREVIATIONS.get(words[0], words[0])] + words[1:] if words else []
    return [re.sub(r"[^a-z]", "", "".join(spelling)) for spelling in (words, spelled_out)]


def find_zip(conn, zip_code, city, state):
    """Return (zip, None) for the customer's zip, or (None, question for the user).

    A city with several zips is not guessed: distances and prices depend on the exact zip.
    """
    match = re.search(r"\d{5}", zip_code or "")
    if match:
        zip_code = match.group()
        if conn.execute("SELECT 1 FROM zip_centroids WHERE zip_code = %s", (zip_code,)).fetchone():
            return zip_code, None
        return None, f"I couldn't find zip code {zip_code}. Please check it."
    if not (city and state):
        return None, "Which zip code, or city and state, should I search?"
    zips = [row["zip_code"] for row in conn.execute(CITY_ZIPS_SQL, {"state": state, "city_keys": city_keys(city)})]
    if len(zips) == 1:
        return zips[0], None
    if not zips:
        return None, f"I couldn't find {city}, {state}. What's the delivery zip code?"
    return None, f"{city}, {state} has {len(zips)} zip codes. What's the delivery zip code?"


def product_words(text):
    """Comparable words of a product name: lowercase, singular, "yd" -> "yard", "10yd" -> "10" + "yard"."""
    found = (WORD_ALIASES.get(word, word) for word in re.findall(r"[a-z]+|[0-9]+", text.lower()))
    return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in found}


def zoning_task_type(category):
    return next((task for keyword, task in ZONING_TASK_TYPES if keyword in category), category)


def natural_order(name):
    """Sort key that puts "2 Yard" before "10 Yard"."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


@functools.cache
def load_products():
    """Product name -> zoning task type, for every product vendors can be found for, in natural order."""
    with psycopg.connect(DB_URL) as conn:
        products = {product: zoning_task_type(category or product) for product, category in conn.execute(PRODUCTS_SQL)}
    return dict(sorted(products.items(), key=lambda item: natural_order(item[0])))


@functools.cache
def product_vocabulary():
    return set().union(*(product_words(product) for product in load_products()))


def correct_words(words):
    """Fix typos and joined words: "constration" -> "construction", "handwash" -> "hand" + "wash".

    Numbers are kept as typed, so a size is never changed to another size.
    """
    vocabulary = product_vocabulary()
    fixed = set()
    for word in words:
        if word in vocabulary or word.isdigit():
            fixed.add(word)
            continue
        split = next(([word[:i], word[i:]] for i in range(2, len(word) - 1)
                      if word[:i] in vocabulary and word[i:] in vocabulary), None)
        fixed.update(split or difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.75) or [word])
    return fixed


def match_products(name):
    """Products whose name has every word the user gave (after fixing typos). An exact match wins."""
    wanted = correct_words(product_words(name))
    matches = [product for product in load_products() if wanted <= product_words(product)]
    return [product for product in matches if product_words(product) == wanted] or matches


def similar_products(name, count=5):
    """Closest product names, for when no product has all the user's words."""
    by_lower = {product.lower(): product for product in load_products()}
    return [by_lower[match] for match in difflib.get_close_matches(name.lower(), by_lower, n=count, cutoff=0.4)]


def format_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (phone or "no phone")


def bare_county(name):
    return next((name[:-len(suffix)] for suffix in COUNTY_SUFFIXES if name.endswith(suffix)), name)


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def search_origin(conn, zip_code):
    """Point, state and county the search starts from: Google's for the zip, as the widget sends.

    Saved after the first lookup. Falls back to the zip's centroid (approximate=True).
    """
    conn.execute(ZIP_POINTS_TABLE_SQL)
    point = conn.execute(ZIP_POINT_SQL, {"zip": zip_code}).fetchone()
    if point:
        return point | {"approximate": False}
    try:
        query = urllib.parse.urlencode({"address": zip_code, "components": "country:US", "key": MAPS_API_KEY})
        result = get_json(f"{GEOCODE_URL}?{query}")["results"][0]
    except (OSError, ValueError, KeyError, IndexError):
        return conn.execute(ZIP_CENTROID_SQL, {"zip": zip_code}).fetchone() | {"approximate": True}
    parts = {kind: part for part in result["address_components"] for kind in part["types"]}
    point = {
        "zip": zip_code,
        "lat": result["geometry"]["location"]["lat"],
        "lng": result["geometry"]["location"]["lng"],
        "state": parts.get("administrative_area_level_1", {}).get("short_name"),
        "county": bare_county(parts.get("administrative_area_level_2", {}).get("long_name", "")),
        "address": result.get("formatted_address"),
    }
    conn.execute(SAVE_ZIP_POINT_SQL, point)
    return point | {"approximate": False}


def live_driving(origin, coords):
    """Road miles and minutes from the origin to each vendor's "lat,lng", from the distance service.

    Empty when the service fails, as it regularly does; the search then falls back like the PHP.
    """
    if not coords:
        return {}
    destinations = urllib.parse.quote("|".join(coords.values()), safe=",|.-")
    try:
        data = get_json(f"{DISTANCE_SERVICE_URL}/distance.php?origins={origin['lat']},{origin['lng']}&destinations={destinations}")
        # The first destination is the origin itself
        distances, durations = data["distances"][0][1:], data["durations"][0][1:]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}
    return {vendor_id: (php_round(meters * METERS_TO_MILES, 2), php_round(seconds / 60))
            for vendor_id, meters, seconds in zip(coords, distances, durations)
            if meters is not None and seconds is not None}


def php_match(row, branch):
    """match_source of a row in one of the PHP's UNION branches ("radius", "zip" or "county")."""
    if row["fits_b"]:
        return "service_by_zip_b"
    if row["fits_c"]:
        return "service_by_zip_c"
    near = row["miles"] is not None and row["miles"] <= 25
    if row["fits_a"]:
        return "distance" if near else "service_by_zip_a"
    if branch == "radius":
        return "distance"
    kind = "zip" if branch == "zip" else "county"
    if row[f"in_{kind}_b"]:
        return f"service_by_{kind}_zb"
    if row[f"in_{kind}_c"]:
        return f"service_by_{kind}_zc"
    if row[f"in_{kind}_a"]:
        return "distance" if near else f"service_by_{kind}_za"
    return f"service_by_{kind}"


def php_replaces(old, new):
    """Whether a later row's match wins over the vendor's earlier one."""
    if new in MILEAGE_MATCHES:
        return old not in MILEAGE_MATCHES
    if old in MILEAGE_MATCHES:
        return False
    if new in ZIP_COUNTY_MATCHES and old not in ZIP_COUNTY_MATCHES:
        return True
    return old == "distance" and new != "distance"


def zone_range(text):
    """(low, high) of a "low,high" zone range as the PHP's parseRange reads it, or None."""
    parts = (text or "").split(",")
    if text in (None, "", "0") or len(parts) < 2 or leading_int(parts[-1]) <= 0:
        return None
    return leading_int(parts[0]), leading_int(parts[-1])


def driving_zone(vendor):
    """The mileage zone that the live road miles fall in (B, then C, then A), else "distance"."""
    for zone in "bca":
        bounds = zone_range(vendor[f"zone_{zone}_range"])
        if vendor["driving_miles"] is not None and bounds and bounds[0] <= vendor["driving_miles"] <= bounds[1]:
            return f"service_by_zip_{zone}"
    return "distance"


def excluded_by_state(vendor, state):
    """The PHP's cross-state rule: "No" = vendor's own state only, "Yes" = the States_Serviced list."""
    cross = (vendor["cross_state_line"] or "").strip().upper()
    if not state or not cross:
        return False
    if cross == "NO":
        own = (vendor["state"] or "").strip().upper()
        return bool(own) and own != state
    if cross == "YES" and vendor["states_serviced"]:
        return state not in [s.strip() for s in vendor["states_serviced"].upper().split(";")]
    return False


def php_vendor_map(conn, product, zip_code, origin, radius_miles):
    """search_vendor_osrm.php: vendor id -> vendor (match_source, miles, ...), in the order it returns them,
    and vendor id -> why it was dropped after its road miles were known."""
    task = load_products()[product]
    rows = conn.execute(CANDIDATES_SQL, {
        "lat": origin["lat"], "lng": origin["lng"], "zip": zip_code, "zip_like": f"%{zip_code}%",
        "county": origin["county"] or "", "county_like": f"%{origin['county']}%", "state": origin["state"] or "",
        "task_like": f"%{task}%", "product_like": f"%{product}%", "radius": radius_miles,
    }).fetchall()

    # The three UNION branches. UNION drops rows equal in every column, and each branch has its own
    # total_count column, so only rows of branches with the same count can be equal.
    union = {}
    for branch, member in (("radius", "in_radius"), ("zip", "zip_zone"), ("county", "county_zone")):
        branch_rows = [row for row in rows if row[member]]
        for row in branch_rows:
            match = php_match(row, branch)
            union.setdefault((row["zoho_id"], row["zoning_id"], match, len(branch_rows)), (row, match))
    # Hot list first, then nearest in a straight line (MySQL puts NULL first), capped at the widget's limit
    ordered = sorted(union.values(), key=lambda item: (not item[0]["hot_list"], item[0]["calc_distance"] is not None,
                                                       item[0]["calc_distance"] or 0))[:WIDGET_LIMIT]

    vendors = {}
    for row, match in ordered:
        old = vendors.get(row["zoho_id"])
        if row["zoho_id"] and (old is None or php_replaces(old["match_source"], match)):
            vendors[row["zoho_id"]] = dict(row, match_source=match)

    driving = live_driving(origin, {vendor_id: f"{v['latitude']},{v['longitude']}" for vendor_id, v in vendors.items()})
    dropped = {}
    for vendor_id, vendor in list(vendors.items()):
        vendor["driving_miles"], vendor["driving_minutes"] = driving.get(vendor_id, (None, None))
        # A mileage zone is checked again with the live road miles
        if vendor["match_source"] in MILEAGE_MATCHES:
            vendor["match_source"] = driving_zone(vendor)
        if vendor["match_source"] == "distance" and (vendor["driving_miles"] or 0) > radius_miles:
            dropped[vendor_id] = f"{vendor['driving_miles']} mi by road, outside {radius_miles} mi"
            del vendors[vendor_id]
            continue
        vendor["excluded_by_state"] = excluded_by_state(vendor, (origin["state"] or "").strip().upper())
    # Hot list first, then nearest by road (unknown road miles count as 0)
    ordered = sorted(vendors.items(), key=lambda item: (not item[1]["hot_list"], item[1]["driving_miles"] or 0))
    return dict(ordered), dropped, bool(driving)


def zone_start(text):
    """Start mile of a zone range, as searchv3duplicate reads it from Zoho ("25-30" -> 25; "0" -> 0)."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*,", text or "")
    return Decimal(match.group(1)) if match else Decimal(0)


def deluge_offers(conn, product, vendors, all_vendors):
    """searchv3duplicate: each vendor's priced product, in the PHP's vendor order."""
    params = {"vendor_ids": list(vendors), "product": product, "all_vendors": all_vendors, "limit": WIDGET_LIMIT}
    task = load_products()[product]
    zoning = {}
    for record in conn.execute(ZONING_RECORDS_SQL, params):
        if any(name.strip().startswith(task) for name in (record["task_type"] or "").split(",")):
            zoning.setdefault(record["vendor_id"], []).append(record)
    offers = {}
    # A vendor with several qualifying records gets the last one
    for record in conn.execute(PRODUCT_OFFERS_SQL, params):
        vendor = vendors[record["vendor_id"]]
        if not vendor["excluded_by_state"]:
            offers[record["vendor_id"]] = deluge_offer(record, vendor, zoning.get(record["vendor_id"], [None])[0], product)
    return [offers[vendor_id] for vendor_id in vendors if vendor_id in offers]


def deluge_offer(record, vendor, zone, product):
    """One vendor's row as the widget shows it: price with the zone up-charge, and its details."""
    price = record["unit_price"] or Decimal(0)
    label = MATCH_LABELS.get(vendor["match_source"], "Unknown")
    term = (zone["services_term"] or "") if zone else (record["services_term"] or "Per Miles")
    miles = vendor["driving_miles"] if vendor["driving_miles"] is not None else vendor["calc_distance"]
    letter = next((letter for letter in "ABC" if f"Zone {letter}" in label), None)
    if zone and letter and price > 0:
        upcharge = Decimal(zone[f"zone_{letter.lower()}_up_charge"] or 0)
        if term == "Per Mile":
            start = zone_start(zone[f"zone_{letter.lower()}_range"])
            price += Decimal(str(php_round(upcharge * (Decimal(str(miles or 0)) - start), 2)))
        elif term == "Flat Fee":
            price += upcharge
    if record["price_type"] == "A’La Carte Pricing" and record["dumpster_ton_costs"] is not None:
        extra_ton = record["dumpster_ton_costs"] * Decimal("1.5")
    elif record["price_type"] == "All Inclusive Cost" and record["additional_tons_cost"] is not None:
        extra_ton = php_round(record["additional_tons_cost"] * Decimal("1.5"), 2)
    else:
        extra_ton = None
    fee = record["dumpster_daily_rental_fee"]
    row = {
        "vendor_id": record["vendor_id"],
        "vendor_name": record["vendor_name"],
        "phone": record["phone"],
        "hot_list": record["hot_list"],
        "notes": record["notes"],
        "price": float(price),
        "match": label,
        "services_term": term,
        "straight_miles": float(vendor["calc_distance"]) if vendor["calc_distance"] is not None else None,
        "driving_miles": vendor["driving_miles"],
        "driving_minutes": vendor["driving_minutes"],
        "delivery_fee": float(record["delivery_fee_msrp"] or 0),
        "tons_included": float(record["tons_included"] or 0),
        "extra_ton_price": float(extra_ton) if extra_ton is not None else None,
        "rental_days": float(record["rental_length_days"] or 0),
        "extra_day_price": float(fee) * 1.5 if fee is not None else None,
    }
    return apply_price_rules(row, product)


def find_vendors(conn, product, zip_code, radius_miles, all_vendors):
    """Vendors and prices as the Search Vendor widget shows them for a product, zip and radius."""
    origin = search_origin(conn, zip_code)
    vendors, dropped, live = php_vendor_map(conn, product, zip_code, origin, radius_miles)
    offers = deluge_offers(conn, product, vendors, all_vendors)
    # "Standard View" hides vendors without a price; count them for the hint
    unpriced = 0 if all_vendors else len(deluge_offers(conn, product, vendors, True)) - len(offers)
    return {"origin": origin, "vendors": vendors, "dropped": dropped, "live": live, "offers": offers, "unpriced": unpriced}


def format_miles(row):
    straight = f"{row['straight_miles']:.2f} mi straight" if row["straight_miles"] is not None else "distance unknown"
    if row["driving_miles"] is None:
        return straight
    return f"{row['driving_miles']:.2f} mi by road ({row['driving_minutes']:g} min) · {straight}"


def format_vendors(product, zip_code, radius_miles, search, show_notes):
    """Numbered vendor list. The first line names the search; older searches keep only that line."""
    rows = search["offers"]
    count = f"{len(rows)} vendors"
    if search["unpriced"]:
        count += f", {search['unpriced']} more without a price (ask for all vendors)"
    lines = [f"**{product}** near zip **{zip_code}** within {radius_miles} mi: {count} (hot list first, then nearest by road)"]
    if search["origin"]["approximate"]:
        lines.append("_Google's point for this zip couldn't be looked up, so miles are from the zip's center._")
    if not search["live"]:
        lines.append("_The road distance service didn't answer, so straight-line miles are used, as the widget does._")
    lines.append("")
    for number, row in enumerate(rows, 1):
        price = f"${row['price']:,.2f}" if row["price"] else "no price yet"
        details = [price, format_miles(row), row["match"]]
        if row["match"] != "Distance" and row["services_term"]:
            details.append(row["services_term"])
        details.append(format_phone(row["phone"]))
        details += [fmt.format(row[key]) for key, fmt in DETAIL_FORMATS if row[key]]
        hot = " **(HOT LIST)**" if row["hot_list"] else ""
        marker = f"{number}. "
        lines.append(f"{marker}**{row['vendor_name']}**{hot}: " + " · ".join(details))
        if show_notes and row["notes"]:
            # Indented as far as the number, so markdown keeps the note inside that vendor's item
            lines.append(f"{' ' * len(marker)}- Notes: {' '.join(row['notes'].split())[:300]}")
    return "\n".join(lines)


def numbered(names):
    return "\n".join(f"{number}. {name}" for number, name in enumerate(names, 1))


def reply(text, keep=None):
    """Tool result: the text shown to the user, and the short version kept for older chat history."""
    return text, keep or text


def vendor_status(vendor):
    status = ["active" if vendor["is_active"] else "**inactive**"]
    if vendor["do_not_use"] or vendor["notes_say_do_not_use"]:
        status.append("**marked do not use**")
    if not vendor["has_location"]:
        status.append("**no address in the vendor list**")
    excluded = [f"{kind} {vendor[f'exclude_{kind}']}" for kind in ("zipcodes", "cities", "states") if vendor[f"exclude_{kind}"]]
    if excluded:
        status.append("excludes " + "; ".join(excluded))
    return " · ".join(status)


def vendor_search_facts(conn, vendor, product, zip_code, radius_miles):
    """What decides whether a vendor is in the results for a product and zip, as bullet points."""
    vendor_id = vendor["vendor_id"]
    task = load_products()[product]
    params = {"vendor_id": vendor_id, "product": product, "task_like": f"%{task}%"}
    facts = [f"- {product}: {p['product_availability']} · "
             + (f"${p['unit_price']:,.2f}" if p["unit_price"] else "no price yet") + " · "
             + ("active" if p["product_active"] and not p["duplicate"] else "inactive or duplicate")
             for p in conn.execute(VENDOR_PRODUCT_SQL, params)] or [f"- Has no {product} product"]
    facts += [f"- {task} zoning: Zone A {z['zone_a_range']} · Zone B {z['zone_b_range']} · Zone C {z['zone_c_range']} · "
              f"{z['services_term'] or 'no service term'}"
              + (f" · cross-state: {z['cross_state_line']} {z['states_serviced'] or ''}" if z["cross_state_line"] else "")
              for z in conn.execute(VENDOR_ZONING_SQL, params)] or [f"- No {task} zoning"]
    search = find_vendors(conn, product, zip_code, radius_miles, all_vendors=True)
    found = next((number for number, row in enumerate(search["offers"], 1) if row["vendor_id"] == vendor_id), None)
    where = f"- In the {product} results for {zip_code} within {radius_miles} mi: "
    if found:
        row = search["offers"][found - 1]
        facts.append(where + f"**yes**, #{found} ({row['match']}, {format_miles(row)})"
                     + ("" if row["price"] else ", but only with all vendors, as it has no price yet"))
    elif vendor_id in search["dropped"]:
        facts.append(where + f"**no**: {search['dropped'][vendor_id]}")
    elif vendor_id in search["vendors"] and search["vendors"][vendor_id]["excluded_by_state"]:
        facts.append(where + "**no**: its zoning's cross-state rule excludes the customer's state")
    elif vendor_id in search["vendors"]:
        facts.append(where + f"**no**: it's in range, but its {product} product doesn't qualify")
    else:
        facts.append(where + f"**no**: not within {radius_miles} mi in a straight line, nor in any of its zones")
    return facts


# return_direct: the result goes straight to the user, so a search costs a single LLM call
@tool(parse_docstring=True, return_direct=True, response_format="content_and_artifact")
def search_vendors(product: str, zip_code: str = "", city: str = "", state: str = "",
                   radius_miles: int = 25, show_notes: bool = False, all_vendors: bool = False) -> tuple[str, str]:
    """Find vendors that deliver a product to a US location and show them to the user.

    Args:
        product: Product with every word the user gave, e.g. "20 yard dumpster", "construction standard porta potty".
        zip_code: 5-digit delivery zip code, e.g. "40056" from "Pewee Valley, KY 40056, USA".
        city: Delivery city. Only needed when the address has no zip code.
        state: 2-letter state code, e.g. "KY". Only needed when the address has no zip code.
        radius_miles: Search radius in miles (25, 50 or 75).
        show_notes: Also show each vendor's notes. Only when the user asks for notes.
        all_vendors: Also show vendors with no price yet (the widget's "All Vendors" view). Only when the user asks for all vendors.
    """
    products = match_products(product)
    if not products:
        suggestions = numbered(similar_products(product))
        if suggestions:
            return reply(f'I couldn\'t find "{product}". Did you mean:\n{suggestions}')
        return reply(f'I couldn\'t find a product called "{product}". Try a size or type, '
                     'e.g. "20 yard dumpster" or "construction standard porta potty".')
    if len(products) > 1:
        options = numbered(products[:MAX_PRODUCT_OPTIONS])
        more = len(products) - MAX_PRODUCT_OPTIONS
        return reply(f"Which {product} do you need?\n{options}" + (f"\n\n…and {more} more" if more > 0 else ""))
    product = products[0]

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        zip_code, problem = find_zip(conn, zip_code, city, state)
        if problem:
            return reply(problem)
        search = find_vendors(conn, product, zip_code, radius_miles, all_vendors)
    if not search["offers"]:
        if search["unpriced"]:
            return reply(f"No priced vendors for {product} near zip {zip_code} within {radius_miles} miles; "
                         f"{search['unpriced']} have no price yet (ask for all vendors to see them).")
        return reply(f"No vendors found for {product} near zip {zip_code} within {radius_miles} miles. Try 50 or 75 miles.")
    text = format_vendors(product, zip_code, radius_miles, search, show_notes)
    # Once a newer search is made, only the header of this one stays in the history
    return reply(text, keep=text.split("\n", 1)[0])


@tool(parse_docstring=True, return_direct=True, response_format="content_and_artifact")
def check_vendor(vendor_name: str, product: str = "", zip_code: str = "", city: str = "", state: str = "",
                 radius_miles: int = 25) -> tuple[str, str]:
    """Look up one vendor by name: does it exist, and is it in a search's results (and what decides it).

    Args:
        vendor_name: Vendor name as written, including any city/zip, e.g. "ADM Rolloff LLC (Hampton, GA 30228)".
        product: Product of the search being discussed, if any.
        zip_code: Zip code of that search, if any.
        city: City of that search, when there is no zip code.
        state: 2-letter state code of that search, when there is no zip code.
        radius_miles: Radius of that search.
    """
    patterns = [f"%{word}%" for word in re.findall(r"[a-z0-9]+", vendor_name.lower())]
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        vendors = conn.execute(VENDOR_NAME_SQL, {"name_patterns": patterns}).fetchall()
        if not vendors:
            return reply(f'No vendor called "{vendor_name}" in our data.')
        if len(vendors) > 1:
            return reply(f'Several vendors match "{vendor_name}". Which one?\n'
                         + numbered(vendor["vendor_name"] for vendor in vendors))
        vendor = vendors[0]
        lines = [f"**{vendor['vendor_name']}** exists: {format_phone(vendor['phone'])} · {vendor_status(vendor)}"]
        products = match_products(product) if product else []
        zip_code = find_zip(conn, zip_code, city, state)[0] if zip_code or city else None
        if len(products) == 1 and zip_code:
            lines += vendor_search_facts(conn, vendor, products[0], zip_code, radius_miles)
    return reply("\n".join(lines))


@tool(parse_docstring=True)
def run_sql(query: str) -> str:
    """Run one read-only PostgreSQL SELECT on the vendor database and return its rows.

    Args:
        query: One SELECT (or WITH ... SELECT) statement. Add a LIMIT when listing rows.
    """
    if not READONLY_DB_URL:
        return "run_sql isn't set up: run python db/create_readonly_role.py"
    try:
        with psycopg.connect(READONLY_DB_URL) as conn:
            conn.read_only = True
            cursor = conn.execute(query)
            if cursor.description is None:
                return "The statement returned no rows."
            columns = [column.name for column in cursor.description]
            rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
    except psycopg.Error as error:
        return f"SQL error: {error}"
    lines = [" | ".join(columns)]
    lines += [" | ".join("" if value is None else str(value)[:200] for value in row) for row in rows[:MAX_SQL_ROWS]]
    if len(rows) > MAX_SQL_ROWS:
        lines.append(f"(only the first {MAX_SQL_ROWS} rows shown; use COUNT or a narrower query)")
    if len(rows) == 0:
        lines.append("(no rows)")
    return "\n".join(lines)[:MAX_SQL_CHARS]


def load_llm(groq_model_name):
    llm = ChatGroq(
        model=groq_model_name,
        temperature=0,
        reasoning_effort="medium",
        api_key=GROQ_API_KEY,
    )
    return llm


@dynamic_prompt
def dated_prompt(request):
    """The system prompt with today's date, so "tomorrow" or "next Friday" become a date."""
    today = date.today()
    return f"{SYSTEM_PROMPT}\n\nToday is {today:%A}, {today.isoformat()}."


def load_agent():
    # lead.py builds on the search helpers here, so it is imported once they exist
    from lead import review_lead
    agent = create_agent(
        model=load_llm(groq_model_name),
        tools=[search_vendors, check_vendor, run_sql, review_lead],
        middleware=[dated_prompt],
    )
    return agent


def history_messages(history):
    """Recent chat history for the LLM: the latest vendor list in full, older ones as their header."""
    recent = history[-MAX_HISTORY:]
    latest_list = max((i for i, message in enumerate(recent) if "short" in message), default=None)
    return [{"role": message["role"],
             "content": message["short"] if "short" in message and i != latest_list else message["content"]}
            for i, message in enumerate(recent)]


def token_usage(messages):
    """Tokens of the LLM calls behind an answer, as a line to show under it."""
    usage = [message.usage_metadata for message in messages if getattr(message, "usage_metadata", None)]
    total, sent, received = (sum(u[key] for u in usage) for key in ("total_tokens", "input_tokens", "output_tokens"))
    return f"_Tokens used: {total:,} ({sent:,} in · {received:,} out, {len(usage)} LLM call{'s' * (len(usage) != 1)})_"


def ask(agent, history, question):
    """Answer a question in the context of the chat history, and add both to the history."""
    history.append({"role": "user", "content": question})
    messages = history_messages(history)
    try:
        try:
            response = agent.invoke({"messages": messages})
        except Exception as error:
            # The model sometimes writes a malformed tool call (Groq "tool_use_failed"); a retry fixes it
            if "tool_use_failed" not in str(error) and "Failed to call a function" not in str(error):
                raise
            response = agent.invoke({"messages": messages})
    except Exception:
        history.pop()
        raise
    answer = response["messages"][-1]
    entry = {"role": "assistant", "content": answer.content}
    # A tool's answer has a short version for when it is no longer the latest (see reply())
    if answer.type == "tool" and answer.artifact != answer.content:
        entry["short"] = answer.artifact
    history.append(entry)
    return f"{answer.content}\n\n{token_usage(response['messages'][len(messages):])}"


def main():
    agent = load_agent()
    history = []
    while True:
        user_query = input("\nAsk about vendors (or 'exit'): ").strip()
        if user_query.lower() in ("exit", "quit"):
            break
        if not user_query:
            continue
        try:
            print("\n" + ask(agent, history, user_query))
        except Exception as e:
            print(f"Error: {str(e)}")


if __name__ == "__main__":
    main()
