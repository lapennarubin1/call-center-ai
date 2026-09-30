#!/usr/bin/env bash
# Corre toda la suite. Cada test arma su propia base sintética con
# números verificables a mano; ninguno depende de datos reales.
set -u
cd "$(dirname "$0")/.."
FAILED=0
python3 tests/fixtures.py /tmp/jtest.db >/dev/null 2>&1
for t in metrics brain server control; do
  printf '\n\033[36m━━━ %s ━━━\033[0m\n' "$t"
  if python3 "tests/test_$t.py" >/tmp/out_$t.txt 2>&1; then
    printf '\033[32m  PASÓ\033[0m\n'
  else
    printf '\033[31m  FALLÓ\033[0m\n'; tail -20 /tmp/out_$t.txt; FAILED=1
  fi
done
printf '\n'
[ $FAILED -eq 0 ] && printf '\033[32m✓ TODO VERDE\033[0m\n' || printf '\033[31m✗ HAY FALLOS\033[0m\n'
exit $FAILED
