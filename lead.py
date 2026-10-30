"""Lead intake: the customer details an agent types, put in order and checked, with what is still missing."""
import difflib
import re
import urllib.parse
from collections import Counter
from datetime import date, timedelta
import psycopg
from psycopg.rows import dict_row
from langchain_core.tools import tool
from main import (DB_URL, MAPS_API_KEY, GEOCODE_URL, get_json, city_keys, load_products, match_products,
                  natural_order, product_words, similar_products, reply, vendor_status)
from query import VENDOR_NAME_SQL

# Email domains customers use most; a domain one typo away from these is flagged
EMAIL_DOMAINS = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com",
                 "msn.com", "me.com", "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}")
# A date further ahead than this is more likely a typo
MAX_DAYS_AHEAD = 365
MAX_LEAD_OPTIONS = 10
# Picklists of the lead fields
LEAD_TYPES = ("Live Call", "Voicemail", "Web Form", "Email", "Text", "Chat", "Spam")
DEFAULT_LEAD_TYPE = "Live Call"
CUSTOMER_TYPES = ("Homeowner", "Business", "Contractor", "Property Manager", "Government", "Other")
# Rental duration units, by first letter (a month counts as 30 days)
DAYS_PER_UNIT = {"d": 1, "w": 7, "m": 30}
# Google result types that point at a building, not just a street, city or zip
STREET_LEVEL_TYPES = {"street_address", "premise", "subpremise"}
CITY_TYPES = ("locality", "postal_town", "sublocality", "neighborhood")


def tidy_name(name):
    """Capitalize a name typed all in lower or upper case; keep "McDonald" as typed."""
    name = " ".join(name.split())
    return name.title() if name.islower() or name.isupper() else name


def check_phone(text):
    """Return (phone as 704-555-1234, problem or None)."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return text, f"{text} has {len(digits)} digits; a US number has 10"
    phone = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if digits[0] in "01":
        return phone, f"{phone}: no US area code starts with {digits[0]}"
    return phone, None


def check_email(text):
    """Return (email in lower case, problem or None)."""
    email = re.sub(r"\s", "", text).lower()
    if not EMAIL_PATTERN.fullmatch(email):
        return email, f'"{text}" doesn\'t look like an email address'
    user, domain = email.rsplit("@", 1)
    close = difflib.get_close_matches(domain, EMAIL_DOMAINS, n=1, cutoff=0.8)
    if domain not in EMAIL_DOMAINS and close:
        return email, f"{email}: did you mean {user}@{close[0]}?"
    return email, None


def check_date(text):
    """Return (date as "Wed, Oct 28, 2026", problem or None) for a YYYY-MM-DD date."""
    try:
        needed = date.fromisoformat(text.strip())
    except ValueError:
        return text, f'couldn\'t read the date "{text}"'
    today = date.today()
    shown = f"{needed:%a, %b} {needed.day}, {needed.year}"
    if needed == today:
        shown += " (today, same-day delivery)"
    elif needed == today + timedelta(days=1):
        shown += " (tomorrow)"
    if needed < today:
        return shown, f"{shown} is in the past"
    if needed > today + timedelta(days=MAX_DAYS_AHEAD):
        return shown, f"{shown} is more than a year away"
    return shown, None


def check_product(text):
    """Return (product, category, question, problem). A category alone ("dumpster") gives a question."""
    products = match_products(text)
    if len(products) == 1:
        return products[0], load_products()[products[0]], None, None
    if not products:
        similar = similar_products(text)
        return None, None, None, f'no product called "{text}"' + (f"; did you mean {', '.join(similar)}?" if similar else "")
    # The category most of them share: a few porta potties are filed under restroom trailers
    category = Counter(load_products()[product] for product in products).most_common(1)[0][0]
    # Plain names first: "10 Yard Dumpster" before "10 Yard Heavies Dumpster"
    products.sort(key=lambda product: (len(product_words(product)), natural_order(product)))
    more = len(products) - MAX_LEAD_OPTIONS
    options = ", ".join(products[:MAX_LEAD_OPTIONS]) + (f" …and {more} more" if more > 0 else "")
    return None, category, f"which {text}? {options}", None


def same_place(typed, found):
    """Whether a typed city or state names the same place as Google's, allowing a typo ("Charlote")."""
    if set(city_keys(typed)) & set(city_keys(found)):
        return True
    return difflib.SequenceMatcher(None, typed.lower(), found.lower()).ratio() >= 0.8


def check_address(conn, street, city, state, zip_code):
    """Return (address, missing, problems), checked with Google and our zip table."""
    typed = ", ".join(part for part in (street, city, f"{state} {zip_code}".strip()) if part)
    missing = [] if street else ["street address (house number and street)"]
    # Without a street the zip alone says where it is, and a wrong city is then flagged, not looked up
    search = typed if street or not zip_code else zip_code
    try:
        query = urllib.parse.urlencode({"address": search, "components": "country:US", "key": MAPS_API_KEY})
        results = get_json(f"{GEOCODE_URL}?{query}")["results"]
    except (OSError, ValueError, KeyError):
        known = zip_code and conn.execute("SELECT 1 FROM zip_centroids WHERE zip_code = %s", (zip_code,)).fetchone()
        problems = ["Google couldn't be reached to check the address"]
        if not zip_code:
            missing.append("zip code")
        elif not known:
            problems.append(f"zip code {zip_code} isn't in our zip table")
        return typed, missing, problems
    if not results:
        return typed, missing, [f'Google can\'t find "{typed}"']

    result = results[0]
    parts = {kind: part for part in result["address_components"] for kind in part["types"]}
    found_zip = parts.get("postal_code", {}).get("short_name")
    found_state = parts.get("administrative_area_level_1", {}).get("short_name")
    found_city = next((parts[kind]["long_name"] for kind in CITY_TYPES if kind in parts), None)
    # Built from the parts, as formatted_address can start with a place name ("Parking lot, ...")
    found_street = " ".join(parts[kind]["short_name"] for kind in ("street_number", "route") if kind in parts)
    if found_street and "subpremise" in parts:
        found_street += f" #{parts['subpremise']['short_name']}"
    formatted = ", ".join(part for part in (found_street, found_city, f"{found_state or ''} {found_zip or ''}".strip()) if part)
    problems = []
    if street and not STREET_LEVEL_TYPES & set(result["types"]):
        problems.append(f'Google can\'t find the street address "{street}", only {formatted}')
    if zip_code and found_zip and zip_code != found_zip:
        problems.append(f"Google has zip {found_zip} for this address, not {zip_code}: {formatted}")
    if city and found_city and not same_place(city, found_city):
        problems.append(f"Google places this address in {found_city}, not {city}: {formatted}")
    if state and found_state and state.upper() != found_state and not same_place(state, found_state):
        problems.append(f"Google places this address in {found_state}, not {state}: {formatted}")
    if result.get("partial_match") and not problems:
        problems.append(f"Google only partly matched the address: {formatted}")
    if not found_zip:
        missing.append("zip code")
    elif not conn.execute("SELECT 1 FROM zip_centroids WHERE zip_code = %s", (found_zip,)).fetchone():
        problems.append(f"zip code {found_zip} isn't in our zip table, so vendors can't be searched for it")
    # Google's spelling when it agrees with what was typed, else as typed, so nothing changes silently
    return (typed if problems else formatted), missing, problems


def check_vendor_name(conn, name):
    """Return (vendor name as in our data, problem or None)."""
    patterns = [f"%{word}%" for word in re.findall(r"[a-z0-9]+", name.lower())]
    vendors = conn.execute(VENDOR_NAME_SQL, {"name_patterns": patterns}).fetchall()
    if not vendors:
        return name, f'no vendor called "{name}" in our data'
    if len(vendors) > 1:
        return name, f'several vendors match "{name}": ' + "; ".join(vendor["vendor_name"] for vendor in vendors)
    vendor = vendors[0]
    if not vendor["is_active"] or vendor["do_not_use"] or vendor["notes_say_do_not_use"]:
        return vendor["vendor_name"], f"{vendor['vendor_name']} is {vendor_status(vendor)}"
    return vendor["vendor_name"], None


def check_choice(options):
    """A check that turns a value into one of the options, allowing a typo ("homeowmer")."""
    by_lower = {option.lower(): option for option in options}

    def check(text):
        close = difflib.get_close_matches(text.lower(), by_lower, n=1, cutoff=0.75)
        if close:
            return by_lower[close[0]], None
        return text, f'"{text}" isn\'t one of: {", ".join(options)}'
    return check


def check_days(text):
    """Return (rental duration as "14 days (2 weeks)", problem or None) for days, or "2 weeks"/"1 month"."""
    match = re.fullmatch(r"(\d+)\s*(d|days?|w|wks?|weeks?|mo|mos|months?)?", text.lower())
    if not match or int(match.group(1)) == 0:
        return text, f'couldn\'t read the rental duration "{text}"'
    days = int(match.group(1)) * DAYS_PER_UNIT.get((match.group(2) or "d")[0], 1)
    shown = f"{days} day{'s' * (days != 1)}"
    if days % 7 == 0:
        shown += f" ({days // 7} week{'s' * (days != 7)})"
    if days > MAX_DAYS_AHEAD:
        return shown, f"{shown} is more than a year"
    return shown, None


def format_lead(rows, missing, problems, is_test, spam, unclear):
    """The lead as a table in a fixed order, then what is missing and what to check."""
    lines = ["**Lead details**" + ("TEST LEAD" if is_test else ""), "", "| Field | Value |", "|---|---|"]
    for label, value, flagged in rows:
        if spam and not value:
            continue
        if value is None:
            shown = "**missing**"
        else:
            shown = (str(value) or "—").replace("|", "/").replace("\n", " ") + (" ⚠️" if flagged else "")
        lines.append(f"| {label} | {shown} |")
    if unclear:
        problems.append("not sure what these are: " + ", ".join(f'"{piece}"' for piece in unclear))
    sections = ["\n".join(lines)]
    if spam:
        sections.append("Marked as spam, so no other details are needed. Tell me if it isn't spam.")
        return "\n\n".join(sections + (["**Please check:**\n" + "\n".join(f"- {p}" for p in problems)] if problems else []))
    if missing:
        sections.append(f"**Missing ({len(missing)}):**\n" + "\n".join(f"- {item}" for item in missing))
    if problems:
        sections.append("**Please check:**\n" + "\n".join(f"- {problem}" for problem in problems))
    if missing or problems:
        sections.append("Send the missing details or corrections, in any order.")
    else:
        sections.append("All details are here. Tell me if anything should change.")
    return "\n\n".join(sections)


# return_direct: the checked lead goes straight to the user, so it costs a single LLM call
@tool(parse_docstring=True, return_direct=True, response_format="content_and_artifact")
def review_lead(first_name: str = "", last_name: str = "", phone: str = "", best_number: str = "",
                email: str = "", customer_type: str = "", date_needed: str = "", backup_date: str = "",
                rental_days: str = "", product: str = "", street: str = "", city: str = "", state: str = "",
                zip_code: str = "", vendor: str = "", lead_type: str = "", is_test: bool = False,
                description: str = "", unclear: list[str] | None = None) -> tuple[str, str]:
    """Show the customer's lead details in order, checked, with what is still missing.

    Args:
        first_name: Customer's first name.
        last_name: Customer's last name.
        phone: Customer's phone number.
        best_number: Best number to reach the customer, when a second number is given for it.
        email: Customer's email, with obvious typos fixed.
        customer_type: Homeowner, Business, Contractor, Property Manager, Government or Other.
        date_needed: Delivery date as YYYY-MM-DD, or the words as typed if no date can be worked out.
        backup_date: Backup delivery date as YYYY-MM-DD, or the words as typed if no date can be worked out.
        rental_days: Rental duration as a number of days, e.g. "14" for 2 weeks, or the words as typed.
        product: Product with every word given, e.g. "20 yard dumpster", or only "dumpster" if no size was given.
        street: House number and street of the delivery address, e.g. "4521 Park Rd".
        city: City of the delivery address.
        state: 2-letter state code of the delivery address.
        zip_code: 5-digit zip code of the delivery address.
        vendor: Vendor the agent picked, by full name as listed in the search results.
        lead_type: Live Call, Voicemail, Web Form, Email, Text, Chat or Spam. Empty means Live Call.
        is_test: True only when the agent says this is a test lead.
        description: Other order details, e.g. quantity, placement, gate code, what goes in the dumpster.
        unclear: Pieces of the agent's text that couldn't be placed in any field, as typed.
    """
    missing, problems = [], []

    def field(label, value, check=None, optional=False):
        """A table row; an empty value is missing (or "—" if optional), a checked value may add a problem."""
        value = value.strip()
        if not value:
            if not optional:
                missing.append(label.lower())
            return label, "" if optional else None, False
        problem = None
        if check:
            value, problem = check(value)
        if problem:
            problems.append(f"{label}: {problem}")
        return label, value, bool(problem)

    rows = [field("First name", tidy_name(first_name)), field("Last name", tidy_name(last_name)),
            field("Phone", phone, check_phone)]
    if best_number.strip():
        rows.append(field("Best number", best_number, check_phone))
    elif rows[2][1]:
        rows.append(("Best number", f"{rows[2][1]} (same as phone)", rows[2][2]))
    else:
        rows.append(field("Best number", ""))
    rows += [field("Email", email, check_email),
             field("Customer type", customer_type, check_choice(CUSTOMER_TYPES)),
             field("Date needed", date_needed, check_date),
             field("Backup delivery date", backup_date, check_date, optional=True),
             field("Rental duration", rental_days, check_days)]
    if backup_date.strip() and backup_date.strip() == date_needed.strip():
        problems.append("Backup delivery date: it's the same day as the date needed")

    if product.strip():
        found, category, question, problem = check_product(product)
        if problem:
            problems.append(f"Product: {problem}")
        if not found:
            missing.append(f"product needed ({question})" if question else "product needed")
        if not category:
            missing.append("product category")
        rows += [("Product needed", found, False), ("Product category", category, False)]
    else:
        missing += ["product needed", "product category"]
        rows += [("Product needed", None, False), ("Product category", None, False)]

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        if any(part.strip() for part in (street, city, state, zip_code)):
            address, address_missing, address_problems = check_address(
                conn, street.strip(), city.strip(), state.strip(), zip_code.strip())
            missing += address_missing
            problems += [f"Address: {problem}" for problem in address_problems]
            rows.append(("Address", address, bool(address_problems)))
        else:
            missing.append("address")
            rows.append(("Address", None, False))
        rows.append(field("Vendor", vendor, lambda name: check_vendor_name(conn, name)))

    # The agent is on a call while typing, so that is the lead type unless they say otherwise
    rows += [field("Lead type", lead_type or DEFAULT_LEAD_TYPE, check_choice(LEAD_TYPES)),
             ("Test lead", "Yes" if is_test else "No", False),
             field("Description", description, optional=True)]
    spam = rows[-3][1] == "Spam"
    return reply(format_lead(rows, missing, problems, is_test, spam, unclear or []))
