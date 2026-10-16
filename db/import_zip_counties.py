"""Load zip code -> county from the US Census Bureau's 2020 ZCTA-to-county file into zip_counties.

County matching (vendor_zoning.county_a/b/c) needs the customer's county; the widget gets it
from Google. A zip that spans counties gets the county covering most of its land.

    python db/import_zip_counties.py
"""
import csv
import io
import os
import urllib.request
import psycopg
from dotenv import load_dotenv

load_dotenv()
VENDOR_DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/primvmon_zoho_vendors")
CENSUS_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
# Vendor county lists use bare names ("Henry"), not "Henry County"
SUFFIXES = (" County", " Parish", " City and Borough", " Borough", " Census Area", " Municipality")


def bare_county(name):
    return next((name[:-len(suffix)] for suffix in SUFFIXES if name.endswith(suffix)), name)


def main():
    largest = {}  # zip -> (county, land area of the zip inside that county)
    with urllib.request.urlopen(CENSUS_URL) as response:
        for row in csv.DictReader(io.TextIOWrapper(response, encoding="utf-8-sig"), delimiter="|"):
            zip_code, land = row["GEOID_ZCTA5_20"], int(row["AREALAND_PART"] or 0)
            if zip_code and land >= largest.get(zip_code, ("", -1))[1]:
                largest[zip_code] = (bare_county(row["NAMELSAD_COUNTY_20"]), land)

    with psycopg.connect(VENDOR_DB_URL) as conn:
        conn.execute("DROP TABLE IF EXISTS zip_counties")
        conn.execute("CREATE TABLE zip_counties (zip_code varchar(10) PRIMARY KEY, county text NOT NULL)")
        with conn.cursor().copy("COPY zip_counties (zip_code, county) FROM STDIN") as copy:
            for zip_code, (county, _) in largest.items():
                copy.write_row((zip_code, county))
    print(f"zip_counties: {len(largest)} zips")


if __name__ == "__main__":
    main()
