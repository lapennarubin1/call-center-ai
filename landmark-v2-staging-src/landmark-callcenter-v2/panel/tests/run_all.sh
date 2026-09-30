#!/usr/bin/env bash
# Corre las 4 suites. Las de MariaDB se saltan (SKIP, no FAIL) si no hay servidor.
#   LM_TEST_MYSQL_USER / _PASS / _HOST / _SOCKET para apuntar a otra instancia.
set -u
cd "$(dirname "$0")"
rc=0
for s in test_foundation_v2_2.py test_ops_analytics_v2_2.py test_http_v2_2.py test_migration_v2_2.py test_concurrency_v2_2.py; do
  echo; echo "████ $s"
  python3 "$s" 2>&1 | grep -E "^  (ok|FAIL|ERROR|skip) |PASS ·|^   ✗" || true
  python3 "$s" >/dev/null 2>&1 || rc=1
done
exit $rc
