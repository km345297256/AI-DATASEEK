#!/usr/bin/env bash
# Development/test utility only; production does not contain a JVM or these JARs.
set -euo pipefail

fixture_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture_output_dir="${1:?Supply a new output directory; existing fixtures are never overwritten}"
fixture_runtime_dir="$(mktemp -d -t dataseek-jackcess.XXXXXX)"

# Use fixed upstream artifacts; no execution of downloaded setup scripts.
curl --fail --silent --show-error --location --max-time 120 \
  -o "$fixture_runtime_dir/jackcess.jar" \
  https://repo.maven.apache.org/maven2/com/healthmarketscience/jackcess/jackcess/4.0.11/jackcess-4.0.11.jar
curl --fail --silent --show-error --location --max-time 120 \
  -o "$fixture_runtime_dir/commons-lang3.jar" \
  https://repo.maven.apache.org/maven2/org/apache/commons/commons-lang3/3.18.0/commons-lang3-3.18.0.jar
curl --fail --silent --show-error --location --max-time 120 \
  -o "$fixture_runtime_dir/commons-logging.jar" \
  https://repo.maven.apache.org/maven2/commons-logging/commons-logging/1.2/commons-logging-1.2.jar

verify_sha256() {
  local fixture_digest
  fixture_digest="$(sha256sum "$fixture_runtime_dir/$1" | cut -d ' ' -f 1)"
  [[ "$fixture_digest" == "$2" ]] || { echo "Checksum mismatch for $1" >&2; exit 1; }
}
verify_sha256 jackcess.jar de4e6daafd27163ab4f8f3fdb086f9a5a5e2044d93db24d00eebe9946bf09f7d
verify_sha256 commons-lang3.jar 4eeeae8d20c078abb64b015ec158add383ac581571cddc45c68f0c9ae0230720
verify_sha256 commons-logging.jar daddea1ea0be0f56978ab3006b8ac92834afeefbd9b7e4e6316fca57df0fa636

javac -cp "$fixture_runtime_dir/*" -d "$fixture_runtime_dir" "$fixture_source_dir/GenerateAccessFixtures.java"
java -cp "$fixture_runtime_dir:$fixture_runtime_dir/*" GenerateAccessFixtures "$fixture_output_dir"
# Kept only in /tmp for inspection; the documented --rm container discards them.
echo "Generated synthetic fixtures and passed independent Jackcess read-only checks."
