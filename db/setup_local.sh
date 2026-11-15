#!/usr/bin/env bash
# One-time local PostgreSQL setup for this project.
#
# Run from the project root:
#   sudo bash db/setup_local.sh
#
# It creates the role/database named in DATABASE_URL, switches local TCP
# authentication from ident to scram-sha-256 (backing up pg_hba.conf first),
# and applies db/create_schema.sql.

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run this with sudo: sudo bash db/setup_local.sh" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: $ENV_FILE not found. Copy .env.example to .env first." >&2
  exit 1
fi

# Read the three pieces of DATABASE_URL without printing the password.
eval "$(
  python3 - "$ENV_FILE" <<'PY'
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

url = values.get("DATABASE_URL", "")
if not url:
    sys.exit("DATABASE_URL is missing from .env")

parsed = urlparse(url)
if parsed.scheme not in ("postgres", "postgresql"):
    sys.exit("DATABASE_URL must start with postgresql://")

user = unquote(parsed.username or "")
password = unquote(parsed.password or "")
database = (parsed.path or "/").lstrip("/")
host = parsed.hostname or "localhost"
port = parsed.port or 5432

if not user or not password or not database:
    sys.exit("DATABASE_URL needs a user, password, and database name")

print(f"DB_USER={shlex.quote(user)}")
print(f"DB_PASS={shlex.quote(password)}")
print(f"DB_NAME={shlex.quote(database)}")
print(f"DB_HOST={shlex.quote(host)}")
print(f"DB_PORT={shlex.quote(str(port))}")
PY
)"

echo "Setting up role '$DB_USER' and database '$DB_NAME' on $DB_HOST:$DB_PORT"

systemctl is-active --quiet postgresql || systemctl start postgresql

# Create the login role (or reset its password), then the database.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
  -v role="$DB_USER" -v pass="$DB_PASS" -v db="$DB_NAME" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role', :'pass')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db')
\gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db', :'role')
\gexec
SQL

# Fedora initializes host auth as ident. The app connects over TCP with a
# password, so switch the two loopback lines to scram-sha-256.
PG_HBA="$(runuser -u postgres -- psql -tAc 'SHOW hba_file')"
if grep -Eq '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+ident' "$PG_HBA"; then
  BACKUP="$PG_HBA.bak.$(date +%Y%m%d%H%M%S)"
  cp "$PG_HBA" "$BACKUP"
  sed -i -E 's#^([[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+)ident#\1scram-sha-256#' "$PG_HBA"
  echo "Updated $PG_HBA (backup: $BACKUP)"
  systemctl reload postgresql
fi

# Verify password auth before applying the schema.
if ! PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$DB_NAME" -v ON_ERROR_STOP=1 -tAc 'SELECT 1' >/dev/null; then
  echo "error: password connection failed. Check pg_hba.conf." >&2
  exit 1
fi

PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
  -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$ROOT_DIR/db/create_schema.sql"

echo
echo "Done. Next:"
echo "  .venv/bin/python check.py"
echo "  .venv/bin/python ingest/fetch_and_load.py"
