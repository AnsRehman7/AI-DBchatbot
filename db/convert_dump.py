"""Convert the INSERT data of a phpMyAdmin/MariaDB dump into PostgreSQL SQL.

Only data is emitted, to stdout. Tables come from schema.sql and keys from
indexes.sql, loaded before and after this output:

    (cat db/schema.sql; python3 db/convert_dump.py dump.sql; cat db/indexes.sql) \
        | psql -d primvmon_zoho_vendors -v ON_ERROR_STOP=1 --single-transaction -q

Per-table row counts are printed to stderr for verification.
"""
import re
import sys

HEADER = re.compile(rb"^INSERT INTO `(\w+)` \((.*)\) VALUES\s*$")

# vendors_list.Created_At holds MySQL zero dates, which Postgres rejects.
ZERO_DATE = b"'0000-00-00'"
ZERO_DATE_TABLES = {b"vendors_list"}


def convert(src, out):
    # The dump escapes strings MySQL-style (\' \n \\); with standard strings off,
    # Postgres reads those escapes the same way.
    out.write(b"SET standard_conforming_strings = off;\n")
    out.write(b"SET escape_string_warning = off;\n")
    out.write(b"SET client_encoding = 'UTF8';\n")

    counts = {}
    table = None
    for line in src:
        if table is None:
            m = HEADER.match(line)
            if not m:
                continue  # DDL, comments, SET/ALTER statements
            table = m.group(1).lower()
            cols = m.group(2).replace(b"`", b"").lower()
            out.write(b"INSERT INTO " + table + b" (" + cols + b") VALUES\n")
            continue

        # phpMyAdmin writes one row tuple per line, ending in "," or ";".
        if not line.startswith(b"("):
            sys.exit(f"unexpected line in {table.decode()} data: {line[:100]!r}")
        if table in ZERO_DATE_TABLES:
            line = line.replace(ZERO_DATE, b"NULL")
        out.write(line)
        counts[table] = counts.get(table, 0) + 1
        if line.rstrip().endswith(b";"):
            table = None

    for name, n in sorted(counts.items()):
        print(f"{name.decode()}: {n}", file=sys.stderr)


if __name__ == "__main__":
    with open(sys.argv[1], "rb", buffering=1 << 20) as src:
        convert(src, sys.stdout.buffer)
