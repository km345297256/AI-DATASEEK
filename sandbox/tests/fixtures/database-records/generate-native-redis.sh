#!/bin/sh
# Run ONLY inside the separately authorized throwaway Redis 7.2.7 test container.
# Network must be disabled, no dataset/business mounts, readonly root, /tmp tmpfs,
# memory 256 MiB, CPU 1, pids 64, outer timeout 60s. No existing Redis instance.
set -eu
redis-server --version | grep -q 'v=7.2.7 '
test ! -e /tmp/dataseek-fixture.sock
redis-server --port 0 --unixsocket /tmp/dataseek-fixture.sock --unixsocketperm 600 \
  --daemonize yes --pidfile /tmp/dataseek-fixture.pid --dir /tmp --dbfilename dataseek-native.rdb \
  --save '' --appendonly no --protected-mode yes --maxmemory 32mb --logfile /tmp/dataseek-fixture.log
cleanup() { redis-cli -s /tmp/dataseek-fixture.sock shutdown nosave >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM
redis-cli -s /tmp/dataseek-fixture.sock set precise 9007199254740993 >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock pexpireat precise 9223372036854775807 >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock rpush list first -9223372036854775808 科学 >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock sadd set alpha beta >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock hset hash a '' b '<img src=x onerror=alert(1)>' >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock zadd zset 1.25 member 1e308 large >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock -n 2 set other '第二组' >/dev/null
# Exercise native LZF without sending large output or restoring external input.
printf '%01024d' 0 | redis-cli -s /tmp/dataseek-fixture.sock -x set compressed >/dev/null
redis-cli -s /tmp/dataseek-fixture.sock save >/dev/null
test "$(wc -c < /tmp/dataseek-native.rdb)" -lt 65536
redis-check-rdb /tmp/dataseek-native.rdb >/dev/null
# Only this original synthetic file is exported as bounded base64 to caller.
printf 'NATIVE_REDIS_RDB_BEGIN\n'
base64 /tmp/dataseek-native.rdb
printf '\nNATIVE_REDIS_RDB_END\n'
