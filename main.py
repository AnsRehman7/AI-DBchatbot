import difflib
import functools
import os
import re
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from query import (SEARCH_SQL, PRODUCTS_SQL, CITY_ZIPS_SQL, VENDOR_NAME_SQL, VENDOR_PRODUCT_SQL,
                   VENDOR_DISTANCE_SQL, VENDOR_ZONING_SQL)

load_dotenv()
GROQ_API_KEY = os.environ.get("LLM_API_KEY")
groq_model_name = os.environ.get("LLM_MODEL")
# Password comes from ~/.pgpass, so the URL has none
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/primvmon_zoho_vendors")

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
MAX_PRODUCT_OPTIONS = 20
# Match label for vendors inside the radius in a straight line but outside it by road
EDGE_MATCH = "Just outside by road"
# Messages of chat history sent with each question (6 questions and answers)
MAX_HISTORY = 12
# Vendor details shown when the vendor has them
DETAIL_FORMATS = [
    ("delivery_fee", "delivery ${:,.2f}"),
    ("tons_included", "{:g} tons incl."),
    ("rental_days", "{:g} days incl."),
    ("extra_ton_price", "extra ton ${:,.2f}"),
    ("extra_day_price", "extra day ${:,.2f}"),
]

SYSTEM_PROMPT = """You help a sales team find vendors who deliver a product to a customer's location.
- Call search_vendors when the user wants vendors, or answers a question about their search. It shows the
  results to the user. For anything else (greetings, other questions), reply briefly without calling it.
  Never make up vendors or prices.
- product: the product words exactly as the user gave them, including the kind of job (e.g. "portable
  toilet for an event" -> "event porta potty"). Only change a word that is another name for ours:
  portable toilet / porta john -> porta potty, roll off / bin -> dumpster. Never swap one product for
  another (a hand wash station is not a washout tank). When the user picks from a "Which ... do you
  need?" list, pass the option they picked.
- Location: the zip code if the user gave one (also inside a full address), otherwise the city and
  2-letter state code. If the product or the location is missing, ask for it.
- For follow-ups ("what about 50 miles", "show notes", "show all vendors"), search again with the earlier
  product and location.
- Call check_vendor when the user asks about one vendor by name (does it exist, why isn't it listed),
  with the product, location and radius of the search being discussed, if any. Pass the vendor name
  exactly as written, including the city/zip in brackets. When the user picks from a "Several vendors
  match" list (e.g. "second one"), pass the full name of the vendor they picked."""


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
        row["rental_days"] = row["rental_days"] or 7
        row["extra_day_price"] = row["extra_day_price"] or 35
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
    """Comparable words of a product name: lowercase, singular, "yd" -> "yard"."""
    found = (WORD_ALIASES.get(word, word) for word in re.findall(r"[a-z0-9]+", text.lower()))
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


def format_vendors(product, zip_code, radius_miles, rows, show_notes, unpriced_hidden):
    """Numbered vendor list. The first line names the search; it is what the chat history keeps."""
    edge = sum(row["match"] == EDGE_MATCH for row in rows)
    count = f"{len(rows) - edge} vendors" + (f" + {edge} just outside by road" if edge else "")
    if unpriced_hidden:
        count += f", {unpriced_hidden} more without a price (ask for all vendors)"
    lines = [f"**{product}** near zip **{zip_code}** within {radius_miles} mi: {count} (hot list first, then nearest)", ""]
    for number, row in enumerate(rows, 1):
        # Edge vendors come last (see SEARCH_SQL), under their own heading
        if row["match"] == EDGE_MATCH and number == len(rows) - edge + 1:
            lines += ["", f"**Just outside {radius_miles} mi by road** (inside it in a straight line):", ""]
        if row["miles"] is None:
            miles = "distance unknown"
        else:
            miles = f"{row['miles']} mi" + ("" if row["driving_miles"] else " (straight-line)")
        price = f"${row['price']:,.2f}" if row["price"] else "no price yet"
        details = [price, miles, format_phone(row["phone"]), row["match"]]
        details += [fmt.format(row[key]) for key, fmt in DETAIL_FORMATS if row[key] is not None]
        hot = " **(HOT LIST)**" if row["hot_list"] else ""
        marker = f"{number}. "
        lines.append(f"{marker}**{row['vendor_name']}**{hot}: " + " · ".join(details))
        if show_notes and row["notes"]:
            # Indented as far as the number, so markdown keeps the note inside that vendor's item
            lines.append(f"{' ' * len(marker)}- Notes: {' '.join(row['notes'].split())}")
    return "\n".join(lines)


def numbered(names):
    return "\n".join(f"{number}. {name}" for number, name in enumerate(names, 1))


def reply(text, keep=None):
    """Tool result: the text shown to the user, and what to keep of it in the chat history."""
    return text, keep or text


def search_params(product, zip_code, radius_miles):
    return {
        "zip": zip_code,
        "zip_like": f"%{zip_code}%",
        "product": product,
        "product_like": f"%{product}%",
        "task_type": f"%{load_products()[product]}%",
        "radius": radius_miles,
        "edge_label": EDGE_MATCH,
    }


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
    params = search_params(product, zip_code, radius_miles) | {"vendor_id": vendor["vendor_id"]}
    task = load_products()[product]
    facts = [f"- {product}: {p['product_availability']} · "
             + (f"${p['unit_price']:,.2f}" if p["unit_price"] else "no price yet") + " · "
             + ("active" if p["product_active"] and not p["duplicate"] else "inactive or duplicate")
             for p in conn.execute(VENDOR_PRODUCT_SQL, params)] or [f"- Has no {product} product"]
    distance = conn.execute(VENDOR_DISTANCE_SQL, params).fetchone()
    facts.append(f"- Distance to {zip_code}: {distance['driving_miles']} mi by road, {distance['straight_miles']} mi straight-line"
                 if distance else f"- No saved distance to {zip_code}")
    facts += [f"- {task} zoning: Zone A {z['zone_a_range']} · Zone B {z['zone_b_range']} · Zone C {z['zone_c_range']} · "
              f"{z['services_term'] or 'no service term'}"
              + (f" · cross-state: {z['cross_state_line']} {z['states_serviced'] or ''}" if z["cross_state_line"] else "")
              for z in conn.execute(VENDOR_ZONING_SQL, params)] or [f"- No {task} zoning"]
    rows = conn.execute(SEARCH_SQL, params).fetchall()
    found = next((number for number, row in enumerate(rows, 1) if row["vendor_name"] == vendor["vendor_name"]), None)
    unpriced = found and not rows[found - 1]["price"]
    facts.append(f"- In the {product} results for {zip_code} within {radius_miles} mi: "
                 + (f"**yes** ({rows[found - 1]['match']})" if found else "**no**")
                 + (", but only with all vendors, as it has no price yet" if unpriced else ""))
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
        rows = conn.execute(SEARCH_SQL, search_params(product, zip_code, radius_miles)).fetchall()
    if not rows:
        return reply(f"No vendors found for {product} near zip {zip_code} within {radius_miles} miles. Try 50 or 75 miles.")
    rows = [apply_price_rules(row, product) for row in rows]
    # Default is the widget's "Standard View": only vendors with a price
    shown = rows if all_vendors else [row for row in rows if row["price"]]
    if not shown:
        return reply(f"No priced vendors for {product} near zip {zip_code} within {radius_miles} miles; "
                     f"{len(rows)} have no price yet (ask for all vendors to see them).")
    text = format_vendors(product, zip_code, radius_miles, shown, show_notes, len(rows) - len(shown))
    # Follow-ups only need the product and location from the header, not every vendor row
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


def load_llm(groq_model_name):
    llm = ChatGroq(
        model=groq_model_name,
        temperature=0,
        reasoning_effort="low",
        api_key=GROQ_API_KEY,
    )
    return llm


def load_agent():
    agent = create_agent(
        model=load_llm(groq_model_name),
        tools=[search_vendors, check_vendor],
        system_prompt=SYSTEM_PROMPT,
    )
    return agent


def ask(agent, history, question):
    """Answer a question in the context of the chat history, and add both to the history."""
    history.append({"role": "user", "content": question})
    try:
        response = agent.invoke({"messages": history[-MAX_HISTORY:]})
    except Exception:
        history.pop()
        raise
    answer = response["messages"][-1]
    # The history is re-sent with every question, so a tool result is kept as the short
    # version the tool chose (see reply())
    history.append({"role": "assistant", "content": answer.artifact if answer.type == "tool" else answer.content})
    return answer.content


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
