# Port of the live search. search_vendor_osrm.php finds vendors whose service zones (mileage,
# zip list or county list) cover the customer or who are within the radius; the Zoho function
# searchv3duplicate then keeps vendors whose product qualifies and prices it (see main.py).


def _mysql_unsigned(expr):
    """MySQL CAST(<text> AS UNSIGNED): the leading digits, 0 when there are none."""
    return rf"coalesce(substring({expr} from '^\s*(\d+)'), '0')::numeric"


def _zone_fits(column):
    """The PHP's test of the vendor's miles against a zone's "low,high" range ("0" = unused)."""
    low = _mysql_unsigned(f"split_part(b.{column}, ',', 1)")
    high = _mysql_unsigned(f"regexp_replace(b.{column}, '^.*,', '')")
    return f"coalesce(b.{column} NOT IN ('', '0') AND {high} > 0 AND b.miles >= {low} AND b.miles <= {high}, false)"


def _in_list(column, pattern):
    return f"coalesce(b.{column} <> '' AND b.{column} ILIKE {pattern}, false)"


# Every vendor row the PHP's three UNION branches can return, with the tests the branches use.
# Miles are the saved driving miles to the zip, else straight-line miles from the search point
# (Google's point for the zip, which the widget sends). A vendor has a row per matching zoning record.
CANDIDATES_SQL = rf"""
WITH base AS (
    SELECT v.zoho_id, v.street, v.city, v.state, v.zip_code, v.latitude, v.longitude,
           v.hot_list = '1' AS hot_list,
           z.id AS zoning_id, z.zone_a_range, z.zone_b_range, z.zone_c_range,
           z.zone_a_zipcodes_serviced, z.zone_b_zipcodes_serviced, z.zone_c_zipcodes_serviced,
           z.county_a, z.county_b, z.county_c, z.cross_state_line, z.states_serviced,
           s.calc_distance,
           d.driving_miles AS cached_driving_miles,
           coalesce(d.driving_miles, s.calc_distance) AS miles
    FROM vendors_list_new v
    -- MySQL reads a non-numeric coordinate as 0
    CROSS JOIN LATERAL (
        SELECT CASE WHEN v.latitude ~ '^\s*-?\d+(\.\d+)?\s*$' THEN v.latitude::float ELSE 0 END AS lat,
               CASE WHEN v.longitude ~ '^\s*-?\d+(\.\d+)?\s*$' THEN v.longitude::float ELSE 0 END AS lng
    ) c
    CROSS JOIN LATERAL (
        SELECT round((3959 * acos(least(1, greatest(-1,
                   cos(radians(%(lat)s)) * cos(radians(c.lat)) * cos(radians(c.lng) - radians(%(lng)s))
                   + sin(radians(%(lat)s)) * sin(radians(c.lat))))))::numeric, 2) AS calc_distance
    ) s
    LEFT JOIN vendor_zoning z
           ON z.vendor_id = v.zoho_id AND z.zone_data_active IN (0, 1)
          AND z.task_type ILIKE %(task_like)s AND z.is_deleted = 0
    LEFT JOIN vendor_zip_driving_distance d
           ON d.vendor_id = v.zoho_id AND d.zip_code = %(zip)s
    WHERE v.review_notes NOT ILIKE '%%do not use%%'
      AND v.search_criteria ILIKE %(product_like)s
)
SELECT b.*, f.*,
       coalesce(b.calc_distance <= %(radius)s, false) OR f.fits_a OR f.fits_b OR f.fits_c AS in_radius,
       f.in_zip_a OR f.in_zip_b OR f.in_zip_c AS zip_zone,
       f.in_county_a OR f.in_county_b OR f.in_county_c AS county_zone
FROM base b
CROSS JOIN LATERAL (
    SELECT {_zone_fits('zone_a_range')} AS fits_a,
           {_zone_fits('zone_b_range')} AS fits_b,
           {_zone_fits('zone_c_range')} AS fits_c,
           {_in_list('zone_a_zipcodes_serviced', '%(zip_like)s')} AS in_zip_a,
           {_in_list('zone_b_zipcodes_serviced', '%(zip_like)s')} AS in_zip_b,
           {_in_list('zone_c_zipcodes_serviced', '%(zip_like)s')} AS in_zip_c,
           -- County lists only for a vendor in the customer's state (county names repeat across states)
           %(county)s <> '' AND upper(trim(b.state)) = %(state)s AND {_in_list('county_a', '%(county_like)s')} AS in_county_a,
           %(county)s <> '' AND upper(trim(b.state)) = %(state)s AND {_in_list('county_b', '%(county_like)s')} AS in_county_b,
           %(county)s <> '' AND upper(trim(b.state)) = %(state)s AND {_in_list('county_c', '%(county_like)s')} AS in_county_c
) f
WHERE coalesce(b.calc_distance <= %(radius)s, false) OR f.fits_a OR f.fits_b OR f.fits_c
   OR f.in_zip_a OR f.in_zip_b OR f.in_zip_c OR f.in_county_a OR f.in_county_b OR f.in_county_c
"""

# searchv3duplicate's COQL on Products: the vendors' qualifying records of the product,
# hot list first, capped at the widget's limit. "Standard View" needs a price.
PRODUCT_OFFERS_SQL = """
SELECT p.product_id, p.vendor_id, p.unit_price, p.price_type, p.delivery_fee_msrp, p.tons_included,
       p.additional_tons_cost, p.dumpster_ton_costs,
       vd.vendor_name, vd.phone, vd.hot_list, vd.review_notes AS notes, vd.services_term,
       vd.rental_length_days, vd.dumpster_daily_rental_fee
FROM vendor_products p
JOIN vendor_details vd ON vd.vendor_id = p.vendor_id
WHERE p.vendor_id = ANY (%(vendor_ids)s)
  AND p.product_type = %(product)s
  AND p.product_availability IN ('Hauler Carries', 'No Data Available')
  AND (p.unit_price > 0 OR %(all_vendors)s AND (p.unit_price >= 0 OR p.unit_price IS NULL))
  AND vd.is_active AND vd.do_not_use IS NOT TRUE
  AND p.duplicate IS FALSE AND p.product_active
ORDER BY vd.hot_list DESC NULLS LAST, p.product_id
LIMIT %(limit)s
"""

# searchv3duplicate's COQL on Zoning_Information: every zoning record of the vendors (any task
# type), by vendor id, capped at the widget's limit. It filters the task type afterwards.
ZONING_RECORDS_SQL = """
SELECT vendor_id, task_type, services_term, zone_a_range, zone_b_range, zone_c_range,
       zone_a_up_charge, zone_b_up_charge, zone_c_up_charge
FROM vendor_zoning
WHERE vendor_id = ANY (%(vendor_ids)s) AND is_deleted = 0
ORDER BY vendor_id, zoho_zoning_id
LIMIT %(limit)s
"""

# Google's point, state and county for a zip: the widget's search origin, saved after the first lookup
ZIP_POINTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS zip_google_points (
    zip_code    varchar(10) PRIMARY KEY,
    lat         double precision NOT NULL,
    lng         double precision NOT NULL,
    state       text,
    county      text,
    address     text,
    fetched_at  timestamptz NOT NULL DEFAULT now()
)
"""
ZIP_POINT_SQL = "SELECT lat, lng, state, county FROM zip_google_points WHERE zip_code = %(zip)s"
SAVE_ZIP_POINT_SQL = """
INSERT INTO zip_google_points (zip_code, lat, lng, state, county, address)
VALUES (%(zip)s, %(lat)s, %(lng)s, %(state)s, %(county)s, %(address)s)
ON CONFLICT (zip_code) DO NOTHING
"""
# Fallback origin when Google can't be reached: the zip's centroid and Census county
ZIP_CENTROID_SQL = """
SELECT zc.lat::float AS lat, zc.lng::float AS lng, zc.state, co.county
FROM zip_centroids zc LEFT JOIN zip_counties co ON co.zip_code = zc.zip_code
WHERE zc.zip_code = %(zip)s
"""

# check_vendor: vendors whose name contains every word given, ignoring punctuation
VENDOR_NAME_SQL = """
SELECT vd.vendor_id, vd.vendor_name, vd.phone, vd.is_active, vd.do_not_use,
       coalesce(v.review_notes, '') ILIKE '%%do not use%%' AS notes_say_do_not_use,
       v.zoho_id IS NOT NULL AS has_location,
       nullif(trim(vd.exclude_zipcodes), '') AS exclude_zipcodes,
       nullif(trim(vd.exclude_cities), '') AS exclude_cities,
       nullif(trim(vd.exclude_states), '') AS exclude_states
FROM vendor_details vd
LEFT JOIN vendors_list_new v ON v.zoho_id = vd.vendor_id
WHERE regexp_replace(lower(vd.vendor_name), '[^a-z0-9]', '', 'g') LIKE ALL (%(name_patterns)s)
ORDER BY vd.vendor_name
LIMIT 10
"""

# check_vendor: the facts that decide whether a vendor shows up for a product and zip
VENDOR_PRODUCT_SQL = """
SELECT product_availability, unit_price, product_active, duplicate
FROM vendor_products WHERE vendor_id = %(vendor_id)s AND product_type = %(product)s
"""
VENDOR_ZONING_SQL = """
SELECT zone_a_range, zone_b_range, zone_c_range, services_term, cross_state_line, states_serviced
FROM vendor_zoning WHERE vendor_id = %(vendor_id)s AND task_type ILIKE %(task_like)s AND is_deleted = 0
"""

# Zip codes of a city, for addresses given without a zip. City names are compared
# as letters only, so "St. Louis" and "stlouis" match.
CITY_ZIPS_SQL = """
SELECT zip_code
FROM zip_centroids
WHERE state = upper(%(state)s)
  AND regexp_replace(lower(city), '[^a-z]', '', 'g') = ANY (%(city_keys)s)
ORDER BY zip_code
"""

# Every product name vendors can be found for (same product rules as PRODUCT_OFFERS_SQL),
# with its usual category, which decides the vendor zoning to use
PRODUCTS_SQL = """
SELECT product_type, mode() WITHIN GROUP (ORDER BY product_category) AS category
FROM vendor_products
WHERE product_type IS NOT NULL
  AND product_availability IN ('Hauler Carries', 'No Data Available')
  AND product_active AND NOT duplicate AND (unit_price >= 0 OR unit_price IS NULL)
GROUP BY product_type
ORDER BY product_type
"""
