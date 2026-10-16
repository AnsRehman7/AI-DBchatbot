"""Copy vendor products and vendor details from the SalesHub DB (a mirror of Zoho CRM)
into primvmon_zoho_vendors, so vendor search reads everything from one database.

Re-run any time to refresh. Both tables are rebuilt in one transaction.

    python db/import_saleshub.py
"""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()
VENDOR_DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/primvmon_zoho_vendors")
# Default is the local dev login from pd-saleshub/docker-compose.yml
SALESHUB_DB_URL = os.environ.get("SALESHUB_DATABASE_URL", "postgresql://saleshub_admin:saleshub@localhost:5533/saleshub")

# (column, type, Zoho field). SalesHub keeps each record's fields in records.data;
# "Vendor_Name.id" reads the id inside the Vendor_Name lookup.
PRODUCT_FIELDS = [
    ("product_id", "text", "id"),
    ("vendor_id", "text", "Vendor_Name.id"),
    ("product_name", "text", "Product_Name"),
    ("product_type", "text", "Product_Type"),
    ("product_category", "text", "Product_Category"),
    ("product_availability", "text", "Product_Availability"),
    ("product_active", "boolean", "Product_Active"),
    ("duplicate", "boolean", "Duplicate"),
    ("price_type", "text", "Price_Type"),
    ("unit_price", "numeric", "Unit_Price"),
    ("default_unit_price", "numeric", "Default_Unit_Price"),
    ("delivery_fee_msrp", "numeric", "Delivery_Fee_MSRP"),
    ("porta_potty_msrp", "numeric", "Porta_Potty_MSRP"),
    ("rental_cost_porta_potty", "numeric", "Rental_Cost_Porta_Potty"),
    ("delivery_fee_porta_potty", "numeric", "Delivery_Fee_Porta_Potty"),
    ("dumpster_classification", "text", "Dumpster_Classification"),
    ("dumpster_yard_size", "numeric", "Dumpster_Yard_Size"),
    ("tons_included", "numeric", "Tons_Included"),
    ("additional_tons_cost", "numeric", "Additional_Tons_Cost"),
    ("dumpster_ton_costs", "numeric", "Dumpster_Ton_Costs"),
    ("minimum_tonnage1", "numeric", "Minimum_Tonnage1"),
    ("rental_cost_additional_days", "numeric", "Rental_cost_Additional_Days"),
    ("daily_rent_cost", "numeric", "Daily_Rent_Cost"),
    ("delivery_fee_dumpster", "numeric", "Delivery_Fee_Dumpster"),
    ("haul_rate_dumpsters", "numeric", "Haul_Rate_Dumpsters"),
    ("vendor_all_included_price_dumpsters", "numeric", "Vendor_All_Included_Price_Dumpsters"),
    ("other_dollar_fees", "numeric", "Other_Dollar_Fees"),
    ("other_percent_fees", "numeric", "Other_Percent_Fees"),
    ("fuel_fee", "numeric", "Fuel_Fee"),
    ("taxes_charged_to_us", "numeric", "Taxes_Charged_to_Us"),
    ("trailer_porta_potty_notes", "text", "Trailer_Porta_Potty_Notes"),
    ("sold_out_start_date", "date", "Sold_Out_Start_Date"),
    ("sold_out_end_date", "date", "Sold_Out_End_Date"),
    ("modified_time", "timestamptz", "Modified_Time"),
]

VENDOR_FIELDS = [
    ("vendor_id", "text", "id"),
    ("vendor_name", "text", "Vendor_Name"),
    ("phone", "text", "Phone"),
    ("email", "text", "Email"),
    ("website", "text", "Website"),
    ("is_active", "boolean", "Vendor_Active_Unchecked_is_innactive"),
    ("do_not_use", "boolean", "Do_not_use"),
    ("do_not_use_reason", "text", "Do_Not_Use_Reason"),
    ("do_not_book_unless_confirmed", "boolean", "DO_NOT_BOOK_UNLESS_CONFIRMED"),
    ("hot_list", "boolean", "HOT_LIST"),
    ("official_partner", "boolean", "Official_Partner"),
    ("unresponsive", "boolean", "Unresponsive"),
    ("no_pricing_verification", "boolean", "No_Pricing_Verification"),
    ("hauler_on_vacation", "boolean", "Hauler_on_Vacation"),
    ("unavailability_start_date", "date", "Unavailability_Start_Date"),
    ("unavailability_end_date", "date", "Unavailability_End_Date"),
    ("rating", "numeric", "Rating"),
    ("vendor_availability", "numeric", "Vendor_Availability"),
    ("carries_dumpsters", "text", "Carries_Dumpsters"),
    ("carries_porta_potties", "text", "Carries_Porta_Potties"),
    ("open_on_weekends", "text", "Open_on_Weekends"),
    ("same_day_delivery", "text", "Same_Day_Delivery"),
    ("services_term", "text", "Services_Term"),
    ("dumpster_daily_rental_fee", "numeric", "Dumpster_Daily_Rental_Fee"),
    ("rental_length_days", "numeric", "Rental_Length_Days"),
    ("tonnage_fee", "numeric", "Tonnage_Fee"),
    ("heavies_max_tonnage", "numeric", "Heavies_Max_Tonnage_Field"),
    ("exclude_zipcodes", "text", "Exclude_Zipcodes"),
    ("exclude_cities", "text", "Exclude_Cities"),
    ("exclude_states", "text", "Exclude_States"),
    ("review_notes", "text", "Review_Notes"),
    ("dumpster_notes", "text", "Dumpster_Notes"),
    ("heavies_notes", "text", "Heavies_Notes"),
    ("porta_potty_notes", "text", "Porta_Potty_Notes"),
    ("pumping_services_notes", "text", "Pumping_Services_Notes"),
    ("recent_vendor_message", "text", "Recent_Vendor_Message"),
    ("pricing_last_updated", "timestamptz", "Last_Updated"),
    ("modified_time", "timestamptz", "Modified_Time"),
]

POST_LOAD = [
    "ALTER TABLE vendor_products ADD PRIMARY KEY (product_id)",
    "CREATE INDEX vendor_products_vendor_type_idx ON vendor_products (vendor_id, product_type)",
    "ALTER TABLE vendor_details ADD PRIMARY KEY (vendor_id)",
    "ANALYZE vendor_products",
    "ANALYZE vendor_details",
]


def json_expr(field, pg_type):
    *lookups, key = field.split(".")
    expr = "data" + "".join(f"->'{name}'" for name in lookups) + f"->>'{key}'"
    return expr if pg_type == "text" else f"({expr})::{pg_type}"


def copy_module(src, dst, table, module, fields):
    columns = ", ".join(column for column, _, _ in fields)
    dst.execute(f"DROP TABLE IF EXISTS {table}")
    dst.execute(f"CREATE TABLE {table} ({', '.join(f'{column} {pg_type}' for column, pg_type, _ in fields)})")

    select = ", ".join(json_expr(field, pg_type) for _, pg_type, field in fields)
    query = f"""COPY (SELECT {select} FROM records r JOIN modules m ON m.id = r.module_id
                      WHERE m.api_name = '{module}' AND NOT r.deleted) TO STDOUT"""
    with src.cursor().copy(query) as copy_out, dst.cursor().copy(f"COPY {table} ({columns}) FROM STDIN") as copy_in:
        for data in copy_out:
            copy_in.write(data)


def main():
    with psycopg.connect(SALESHUB_DB_URL) as src, psycopg.connect(VENDOR_DB_URL) as dst:
        copy_module(src, dst, "vendor_products", "Products", PRODUCT_FIELDS)
        copy_module(src, dst, "vendor_details", "Vendors", VENDOR_FIELDS)
        for statement in POST_LOAD:
            dst.execute(statement)
        for table in ("vendor_products", "vendor_details"):
            print(f"{table}: {dst.execute(f'SELECT count(*) FROM {table}').fetchone()[0]} rows")


if __name__ == "__main__":
    main()
