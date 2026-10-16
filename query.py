# Port of the live search: search_vendor_osrm.php finds vendors whose service zones
# (mileage, zip list or county list) cover the customer zip or who are within the radius;
# the Zoho function searchv3duplicate then keeps vendors whose product qualifies and prices it.
# Miles are cached driving miles, else straight-line miles.
SEARCH_SQL = r"""
WITH customer AS (
    SELECT zc.lat, zc.lng, zc.state, zc.city, co.county
    FROM zip_centroids zc
    LEFT JOIN zip_counties co ON co.zip_code = zc.zip_code
    WHERE zc.zip_code = %(zip)s
),
vendors AS (
    SELECT v.zoho_id, vd.vendor_name, vd.phone, v.street, v.city, v.state, v.zip_code,
           coalesce(vd.hot_list, false) AS hot_list, left(vd.review_notes, 150) AS notes,
           vd.rental_length_days, vd.dumpster_daily_rental_fee,
           p.unit_price, p.price_type, p.delivery_fee_msrp, p.tons_included,
           p.additional_tons_cost, p.dumpster_ton_costs, p.modified_time AS product_modified,
           -- The zoning's service term wins when the vendor has zoning
           CASE WHEN z.id IS NOT NULL THEN z.services_term ELSE vd.services_term END AS services_term,
           z.zone_a_range, z.zone_b_range, z.zone_c_range,
           z.zone_a_up_charge, z.zone_b_up_charge, z.zone_c_up_charge,
           z.zone_a_zipcodes_serviced, z.zone_b_zipcodes_serviced, z.zone_c_zipcodes_serviced,
           z.county_a, z.county_b, z.county_c, c.county AS customer_county,
           upper(trim(v.state)) = c.state AS same_state,
           z.cross_state_line, z.states_serviced, c.state AS customer_state,
           d.driving_miles IS NOT NULL AS is_driving,
           s.straight_miles,
           coalesce(d.driving_miles, s.straight_miles) AS miles
    FROM vendors_list_new v
    CROSS JOIN customer c
    -- Straight-line (haversine) miles; NULL when the vendor has no valid coordinates
    CROSS JOIN LATERAL (
        SELECT CASE WHEN v.latitude ~ '^-?\d+(\.\d+)?$' AND v.longitude ~ '^-?\d+(\.\d+)?$' THEN
            3959 * acos(least(1, greatest(-1,
                cos(radians(c.lat)) * cos(radians(v.latitude::float)) * cos(radians(v.longitude::float) - radians(c.lng))
                + sin(radians(c.lat)) * sin(radians(v.latitude::float)))))
        END AS straight_miles
    ) s
    JOIN vendor_details vd ON vd.vendor_id = v.zoho_id
    JOIN vendor_products p ON p.vendor_id = v.zoho_id
    LEFT JOIN vendor_zoning z
           ON z.vendor_id = v.zoho_id AND z.task_type ILIKE %(task_type)s
          AND z.zone_data_active IN (0, 1) AND z.is_deleted = 0
    LEFT JOIN vendor_zip_driving_distance d
           ON d.vendor_id = v.zoho_id AND d.zip_code = %(zip)s
    -- Product and vendor rules of searchv3duplicate, "All Vendors" view (a vendor without a price
    -- yet is shown; "Standard View" would need unit_price > 0)
    WHERE p.product_type = %(product)s
      AND p.product_availability IN ('Hauler Carries', 'No Data Available')
      AND p.product_active AND NOT p.duplicate AND (p.unit_price >= 0 OR p.unit_price IS NULL)
      AND vd.is_active AND NOT vd.do_not_use
      -- The PHP's own filters: the product is in the vendor's Search_Criteria list, no "do not use" note
      AND v.search_criteria ILIKE %(product_like)s
      AND v.review_notes NOT ILIKE '%%do not use%%'
      -- The vendor's own do-not-service lists
      AND %(zip)s <> ALL (regexp_split_to_array(coalesce(vd.exclude_zipcodes, ''), '\D+'))
      AND lower(c.city) <> ALL (regexp_split_to_array(lower(trim(coalesce(vd.exclude_cities, ''))), '\s*,\s*'))
      AND c.state <> ALL (regexp_split_to_array(upper(coalesce(vd.exclude_states, '')), '[^A-Z]+'))
)
SELECT * FROM (
    SELECT DISTINCT ON (v.zoho_id)
           v.vendor_name,
           v.phone,
           concat_ws(', ', nullif(v.street, ''), v.city, v.state, v.zip_code) AS address,
           round(v.miles::numeric, 1)::float AS miles,
           v.is_driving AS driving_miles,
           m.label AS match,
           -- Zone up-charge: per mile beyond the zone's start, or a flat fee. NULL = no price yet.
           (CASE WHEN v.unit_price > 0 THEN v.unit_price + CASE v.services_term
               WHEN 'Per Mile' THEN coalesce(round(m.up_charge * (v.miles::numeric - m.zone_start), 2), 0)
               WHEN 'Flat Fee' THEN m.up_charge
               ELSE 0
           END END)::float AS price,
           v.delivery_fee_msrp::float AS delivery_fee,
           v.tons_included::float AS tons_included,
           -- Overage prices are the vendor's cost plus 50%%
           round(CASE WHEN v.price_type ILIKE '%%carte%%' THEN v.dumpster_ton_costs * 1.5
                      WHEN v.price_type = 'All Inclusive Cost' THEN v.additional_tons_cost * 1.5
           END, 2)::float AS extra_ton_price,
           v.rental_length_days::float AS rental_days,
           round(v.dumpster_daily_rental_fee * 1.5, 2)::float AS extra_day_price,
           v.hot_list,
           v.notes
    FROM vendors v
    -- Zone ranges are "low,high" miles ("0" when unused). Lowest rank wins, as in the PHP.
    CROSS JOIN LATERAL (
        SELECT rank, label,
               coalesce(nullif(up_charge, '')::numeric, 0) AS up_charge,
               coalesce(nullif(split_part(zone_range, ',', 1), '')::numeric, 0) AS zone_start
        FROM (VALUES
            (1, 'Zone B (mileage)', v.miles BETWEEN split_part(v.zone_b_range, ',', 1)::numeric AND nullif(split_part(v.zone_b_range, ',', 2), '')::numeric, v.zone_b_up_charge, v.zone_b_range),
            (2, 'Zone C (mileage)', v.miles BETWEEN split_part(v.zone_c_range, ',', 1)::numeric AND nullif(split_part(v.zone_c_range, ',', 2), '')::numeric, v.zone_c_up_charge, v.zone_c_range),
            -- The PHP labels Zone A within 25 miles as plain distance, so it gets no up-charge
            (3, 'Zone A (mileage)', v.miles BETWEEN split_part(v.zone_a_range, ',', 1)::numeric AND nullif(split_part(v.zone_a_range, ',', 2), '')::numeric, CASE WHEN v.miles > 25 THEN v.zone_a_up_charge END, v.zone_a_range),
            (4, 'Zone B (zip list)', v.zone_b_zipcodes_serviced LIKE %(zip_like)s, v.zone_b_up_charge, v.zone_b_range),
            (5, 'Zone C (zip list)', v.zone_c_zipcodes_serviced LIKE %(zip_like)s, v.zone_c_up_charge, v.zone_c_range),
            (6, 'Zone A (zip list)', v.zone_a_zipcodes_serviced LIKE %(zip_like)s, CASE WHEN v.miles > 25 THEN v.zone_a_up_charge END, v.zone_a_range),
            -- County lists, only for a vendor in the customer's state (county names repeat across states)
            (7, 'Zone B (county)', v.same_state AND v.county_b ~* ('\m' || v.customer_county || '\M'), v.zone_b_up_charge, v.zone_b_range),
            (8, 'Zone C (county)', v.same_state AND v.county_c ~* ('\m' || v.customer_county || '\M'), v.zone_c_up_charge, v.zone_c_range),
            (9, 'Zone A (county)', v.same_state AND v.county_a ~* ('\m' || v.customer_county || '\M'), CASE WHEN v.miles > 25 THEN v.zone_a_up_charge END, v.zone_a_range),
            (10, 'Within radius', v.miles <= %(radius)s, NULL, NULL),
            -- The PHP picks radius vendors by straight-line miles, then drops those whose live driving
            -- distance (from Google's point for the zip) is longer. Ours is from the zip centroid, so
            -- vendors at the edge can differ: they are listed separately instead of dropped.
            (11, %(edge_label)s, v.straight_miles <= %(radius)s, NULL, NULL)
        ) AS rules (rank, label, ok, up_charge, zone_range)
        WHERE ok
        ORDER BY rank
        LIMIT 1
    ) m
    -- Cross-state rule: "No" = vendor's own state only, "Yes" = states listed in States_Serviced
    WHERE (v.cross_state_line IS DISTINCT FROM 'No' OR coalesce(v.state, '') = ''
           OR upper(trim(v.state)) = v.customer_state)
      AND (v.cross_state_line IS DISTINCT FROM 'Yes' OR coalesce(v.states_serviced, '') = ''
           OR v.customer_state = ANY (string_to_array(upper(replace(v.states_serviced, ' ', '')), ';')))
    ORDER BY v.zoho_id, m.rank, v.product_modified DESC
) results
ORDER BY match = %(edge_label)s, hot_list DESC, miles
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
VENDOR_DISTANCE_SQL = """
SELECT driving_miles, straight_miles
FROM vendor_zip_driving_distance WHERE vendor_id = %(vendor_id)s AND zip_code = %(zip)s
"""
VENDOR_ZONING_SQL = """
SELECT zone_a_range, zone_b_range, zone_c_range, services_term, cross_state_line, states_serviced
FROM vendor_zoning WHERE vendor_id = %(vendor_id)s AND task_type ILIKE %(task_type)s AND is_deleted = 0
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

# Every product name vendors can be found for (same product rules as SEARCH_SQL),
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
