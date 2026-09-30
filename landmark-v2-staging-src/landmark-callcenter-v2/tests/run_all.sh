#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  Landmark Call Center V2 — batería completa
#
#  Corre las 15 suites del suite V2 y, si viajan al lado, las 5 de la
#  fundación V2.2 (panel/tests/). Requisitos: python3, pymysql, node.
#
#    LM_TEST_MYSQL_USER / _PASS / _HOST / _PORT   otra instancia de MariaDB
#
#  Una suite que necesita MariaDB y no la encuentra se SALTA (skip), y el
#  skip se cuenta y se enseña: no hay saltos silenciosos.
#
#  Y una suite que muere antes de imprimir su resultado —un import que
#  falla, una credencial que falta, un SystemExit— cuenta como FALLO.
#  Antes ese caso salía por pantalla como si no hubiera pasado nada.
# ════════════════════════════════════════════════════════════════
set -u
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")"
rc=0
mudas=()
SUITE_V2=(test_engine_parity_v2.py test_package_hygiene_v2.py test_crm_english_v2.py
          test_panel_base_regression_v2.py test_billing_v2.py
          test_legacy_backup_v2.py test_payments_v2.py test_workflow_standard_v2.py
          test_workflow_sql_v2.py test_suite_behaviour_v2.py test_migration_suite_v2.py
          test_analytics_filters_v2.py test_interlock_v2.py
          test_real_interlock_v2.py test_panel_v2.py)
FUNDACION=(test_foundation_v2_2.py test_ops_analytics_v2_2.py test_http_v2_2.py
           test_migration_v2_2.py test_concurrency_v2_2.py)

correr() {                                  # correr <directorio> <suite>
  local dir="$1" s="$2" salida
  echo; echo "████ $s"
  salida=$(cd "$dir" && python3 "$s" 2>&1)
  local suyo=$?
  echo "$salida" | grep -E "^  (ok|FAIL|ERROR|skip) |PASS ·|^   [x✗]" || true
  if ! echo "$salida" | grep -q "PASS ·"; then
    mudas+=("$s")
    echo "  ▲ esta suite NO imprimió resultado: murió antes de empezar"
    echo "$salida" | tail -5 | sed 's/^/    /'
    rc=1
  elif [ $suyo -ne 0 ]; then
    rc=1
  fi
}

echo "════════════════════════════════════════════════════════════════"
echo "  LANDMARK CALL CENTER V2 · batería completa"
echo "════════════════════════════════════════════════════════════════"
for s in "${SUITE_V2[@]}"; do correr . "$s"; done

if [ -d ../panel/tests ]; then
  echo; echo "████████ FUNDACIÓN V2.2 (panel/tests) ████████"
  for s in "${FUNDACION[@]}"; do
    [ -f "../panel/tests/$s" ] && correr ../panel/tests "$s"
  done
fi

echo
echo "════════════════════════════════════════════════════════════════"
if [ ${#mudas[@]} -gt 0 ]; then
  echo "  SUITES QUE NO LLEGARON A CORRER: ${mudas[*]}"
fi
[ $rc -eq 0 ] && echo "  RESULTADO: todo verde" || echo "  RESULTADO: hay fallos"
echo "════════════════════════════════════════════════════════════════"
exit $rc
