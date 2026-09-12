#!/usr/bin/env bash
# Runs INSIDE a one-off official postgres:18.6 container, not on a user DB.
# Invoke via ./run.sh using SANDBOX_IMAGE=postgres:18.6, entrypoint bash,
# this repository mounted /workspace:ro and THIS fixture directory /fixtures.
# No inherited PG connection settings or TCP listener; no user SQL is loaded.
set -Eeuo pipefail

[[ "$(id -u)" == 0 ]] || { echo 'Fixture generation requires the container root account.' >&2; exit 1; }
[[ "$(pg_dump --version)" == 'pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg13+2)' ]] || { echo 'Unexpected fixture writer version.' >&2; exit 1; }
unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGSERVICE PGSERVICEFILE PGPASSFILE PGOPTIONS
fixture_tmp="$(mktemp -d /tmp/dataseek-pgdump-fixture.XXXXXX)"
chmod 700 "$fixture_tmp"
chown postgres:postgres "$fixture_tmp"
cleanup() {
  runuser -u postgres -- pg_ctl -D "$fixture_tmp/db" -m immediate stop >/dev/null 2>&1 || true
}
trap cleanup EXIT
runuser -u postgres -- initdb --auth-local=trust --auth-host=reject --encoding=UTF8 --locale=C -D "$fixture_tmp/db" >/dev/null
runuser -u postgres -- mkdir "$fixture_tmp/socket"
runuser -u postgres -- pg_ctl -D "$fixture_tmp/db" -l "$fixture_tmp/server.log" -o "-c listen_addresses='' -c unix_socket_directories='$fixture_tmp/socket'" -w start >/dev/null
pgargs=(--host="$fixture_tmp/socket" --username=postgres --dbname=postgres)
runuser -u postgres -- psql "${pgargs[@]}" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.measurements(id bigint, amount numeric(38,18), signal double precision, label text, active boolean, optional text);
CREATE TABLE public.empty(label text);
INSERT INTO public.measurements VALUES
(1,1.230000000000000001,2.5,'观测一',true,NULL),
(9007199254740993,-99999999999999999999.123456789012345678,9,'',false,'内容'),
(3,NULL,NULL,NULL,NULL,'');
SQL
dumpopts=(--no-owner --no-privileges --no-comments --no-publications --no-subscriptions --no-security-labels --no-tablespaces --no-table-access-method)
for compression in none gzip lz4 zstd; do
  runuser -u postgres -- pg_dump "${pgargs[@]}" "${dumpopts[@]}" --format=custom --compress="$compression" --file="$fixture_tmp/synthetic-postgres-$compression.dump"
  runuser -u postgres -- pg_restore --list "$fixture_tmp/synthetic-postgres-$compression.dump" > "$fixture_tmp/synthetic-postgres-$compression.list"
done
runuser -u postgres -- pg_dump "${pgargs[@]}" "${dumpopts[@]}" --format=tar --file="$fixture_tmp/synthetic-postgres.tar"
runuser -u postgres -- pg_restore --list "$fixture_tmp/synthetic-postgres.tar" > "$fixture_tmp/synthetic-postgres-tar.list"
runuser -u postgres -- pg_dump "${pgargs[@]}" "${dumpopts[@]}" --format=plain --file="$fixture_tmp/synthetic-postgres.sql"
runuser -u postgres -- psql "${pgargs[@]}" --no-psqlrc --tuples-only --no-align -c "SELECT json_agg(row_to_json(t)) FROM (SELECT id::text, amount::text, signal, label, active, optional FROM measurements ORDER BY id) t" > "$fixture_tmp/synthetic-postgres-rows.json"

# Separate quoted identifiers exercise length-prefixed metadata boundaries.
runuser -u postgres -- psql "${pgargs[@]}" -v ON_ERROR_STOP=1 <<'SQL'
CREATE SCHEMA "science lab";
CREATE TABLE "science lab"."测量 data"(value integer);
CREATE TABLE "science lab"."/Users/private-name"(value integer);
COMMENT ON TABLE public.measurements IS 'not for display: /private/original/location';
SQL
runuser -u postgres -- pg_dump "${pgargs[@]}" --no-owner --no-privileges --format=custom --file="$fixture_tmp/synthetic-postgres-identifiers.dump"
runuser -u postgres -- pg_restore --list "$fixture_tmp/synthetic-postgres-identifiers.dump" > "$fixture_tmp/synthetic-postgres-identifiers.list"

# Copy ONLY this generator's enumerated outputs. No wildcard or user file copy.
outputs=(synthetic-postgres-none.dump synthetic-postgres-gzip.dump synthetic-postgres-lz4.dump synthetic-postgres-zstd.dump synthetic-postgres.tar synthetic-postgres.sql synthetic-postgres-rows.json synthetic-postgres-none.list synthetic-postgres-gzip.list synthetic-postgres-lz4.list synthetic-postgres-zstd.list synthetic-postgres-tar.list synthetic-postgres-identifiers.dump synthetic-postgres-identifiers.list)
for output in "${outputs[@]}"; do
  [[ ! -e "/fixtures/$output" ]] || { echo 'Refusing to replace an existing fixture.' >&2; exit 1; }
done
for output in "${outputs[@]}"; do
  install -m 0644 "$fixture_tmp/$output" "/fixtures/$output"
done
pg_dump --version
sha256sum "${outputs[@]/#//fixtures/}"
