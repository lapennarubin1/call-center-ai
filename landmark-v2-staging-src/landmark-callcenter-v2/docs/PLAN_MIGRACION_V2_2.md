# PLAN_MIGRACION_V2_2.md
**Reemplaza a `PLAN_MIGRACION_V2_1.md`.** Producción no cambia hasta F7 y todo se revierte
desde el panel sin entrar a n8n.

## Cambios respecto de V2.1

| Antes | Ahora |
|---|---|
| R-1 (Stringee) bloqueante: hacía falta que el worker propagara `route_key` | alcanza con `lead_id` (una llamada en vuelo por lead). Verificable con el script de la auditoría |
| rollback: deshabilitar rutas | rollback: **apagar el país** (un clic por país) |
| WF14 V2 = migración de template | WF14 V2 = **reconciliación** (nuevo rol) |
| — | **eventos locales** en cada template desde el primero (WF2) |

---

## F0 · Fundación V2.2 — ESTA ENTREGA
Construida, probada (192/192), **no desplegada**.

## F1 · Verificaciones de solo lectura — UN bloque para pegar por SSH

Nada de esto escribe. Los secretos se enmascaran.

```bash
echo "════════ PV-3 · worker Stringee (ver STRINGEE_WORKER_AUDIT_V2_2.md §5 para el script completo)"
W=/opt/stringee-ai-worker; ls -la "$W"
grep -rn --include=*.js --include=*.ts --include=*.py -E "dynamic_variables|dynamicVariables|conversation_initiation_client_data" -A 15 "$W" --exclude-dir=node_modules | head -60

echo "════════ PV-3b · ¿WF9 pierde los post-call de Stringee? (últimas 500 contestadas)"
mysql -u panel_rw -p asterisk -e "
SELECT COUNT(*) contestadas, SUM(s.lead_id IS NULL) sin_lead_en_crm,
       SUM(s.lead_id IS NOT NULL AND f.lead_id IS NULL) con_lead_sin_followup_wf9
FROM (SELECT call_id, lead_id FROM stringee_calls WHERE answered=1 ORDER BY start_time DESC LIMIT 500) s
LEFT JOIN (SELECT DISTINCT lead_id FROM wf_call_followups WHERE provider='stringee') f ON f.lead_id=s.lead_id;"

echo "════════ PV-12 · ¿n8n llega al panel por la red interna?"
N8N=$(docker ps --format '{{.Names}}' | grep -i n8n | head -1); echo "contenedor n8n: $N8N"
docker exec "$N8N" sh -c 'wget -qO- -T 5 http://172.18.0.1:8080/health || echo FALLO_172.18.0.1'
ss -ltnp | grep -E ':8080|:8091'

echo "════════ PV-1 · ¿POST /followups mueve status/attempts?"
mysql -u panel_rw -p asterisk -e "
SELECT lead_id, status, call_attempts, next_follow_up_at, last_contacted_at
FROM crm_leads WHERE call_attempts BETWEEN 1 AND 5 ORDER BY last_contacted_at DESC LIMIT 20;"

echo "════════ PV-5 · esquemas que tocarán WF9/WF10 V2"
mysql -u panel_rw -p asterisk -e "SHOW CREATE TABLE wf_call_followups\G SHOW CREATE TABLE wf10_sent_recordings\G SHOW CREATE TABLE stringee_calls\G"

echo "════════ PV-9 · usuario real del panel y zona horaria de MySQL"
grep -E 'LM_DB_USER|LM_PORT' /opt/landmark-panel/.env
mysql -u panel_rw -p -e "SELECT @@global.time_zone, @@system_time_zone, NOW(), UTC_TIMESTAMP(), @@version;"

echo "════════ ¿alguna tabla V2 ya existe en producción? (debería estar vacío)"
mysql -u panel_rw -p asterisk -e "SHOW TABLES LIKE 'wf_call_jobs'; SHOW TABLES LIKE 'wf_events'; SHOW TABLES LIKE 'countries'; SHOW TABLES LIKE 'call_routes';"
```

La consulta de zona horaria importa: V2 escribe en UTC (`UTC_TIMESTAMP()`). Si el servidor
no está en UTC, los `DEFAULT CURRENT_TIMESTAMP` de columnas informativas (`created_at` de
eventos) quedarán en hora local; las columnas que usa analytics se escriben explícitamente
en UTC.

## F2 · Panel V2.2 en una copia (puerto y base aparte)

```bash
mkdir -p /opt/landmark-panel-v22 && cd /opt/landmark-panel-v22 && \
tar -xzf /tmp/landmark-panel-multicountry-v2.2.tar.gz --strip-components=1 && \
sha256sum -c CHECKSUMS.sha256 && \
cp /opt/landmark-panel/.env .env && \
sed -i 's/^LM_PORT=.*/LM_PORT=8082/' .env && \
echo "LM_DB_NAME=asterisk_v22test" >> .env && \
echo "LM_ROUTES_API_TOKEN=$(openssl rand -hex 32)" >> .env && \
python3 -m py_compile app/*.py && \
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS asterisk_v22test CHARACTER SET utf8mb4" && \
mysql -u root -p asterisk_v22test < migrations/MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql && \
mysql -u root -p asterisk_v22test < migrations/MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql && \
echo "OK: migración aplicada dos veces en asterisk_v22test"
```

Luego Nivel B del test plan (B1–B28). El panel de producción no se entera.

## F3 · `TEMPLATE_FOLLOWUP_ENGINE_V2`
Oráculo: `app/followup_engine.py` + `app/ops_events.record_call_result`. JSON, no importado.

## F4 · `TEMPLATE_WF2_CALL_DISPATCHER_V2`
**Requiere:** PV-3 (al menos: el worker reenvía `lead_id`), PV-12, PV-1.
Incluye: `next_attempt`, claim, adapters `ELEVENLABS_SIP` y `STRINGEE_WORKER` de 4 pasos,
`DISPATCHING` antes del HTTP, `RELEASED` para técnicos, motor solo en `FINAL`, eventos.

## F5 · `TEMPLATE_WF9_POST_CALL_FOLLOWUP_V2`
Webhook + polling → correlación (`call_job_id` o `find_inflight_job`) →
`record_call_result` → motor. HMAC que **rechaza**.

## F6 · Importar inactivos, probar nodo por nodo contra `asterisk_v22test`

## F7 · Migración en producción + panel V2.2

```bash
mysqldump -u root -p asterisk > /root/backup-pre-v22-$(date +%Y%m%d-%H%M%S).sql && \
mysql -u root -p asterisk < migrations/MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql && \
mysql -u root -p asterisk -e "SELECT migration_id, applied_at FROM schema_migrations ORDER BY applied_at"
```

Solo crea tablas nuevas; ninguna productiva cambia (probado con 6 tablas productivas).
Los workflows v1 siguen intactos y no leen estas tablas.

## F8 · Piloto en un país que v1 no toca (México)

```
1  Panel → alta MX (nace OFF), ruta MX_PROVEEDOR1 completa → ROUTE READY
2  LeadStudio → 2-3 leads +52 de prueba
3  n8n → activar FOLLOWUP_ENGINE_V2, WF2 V2, WF9 V2
4  Panel → ruta MX_PROVEEDOR1 ON (todavía no llama: COUNTRY_DISABLED)
5  Panel → país MX ON  ← el único interruptor que "arranca" el piloto
6  observar en /callcenter/analytics.json: calls, answered, talk_minutes en segundos
7  Panel → país MX OFF
```

Rollback: paso 7.

## F9 · India completa
```
1  wf2_provider_config → asterisk=0, stringee=0   (v1 deja de despachar India)
2  esperar 2 ciclos; confirmar en n8n que v1 no despacha
3  Panel → India ON (ya lo está), rutas IN_PROVEEDOR1 e IN_STRINGEE ON
```
Rollback (≈30 s): Panel → **India OFF** · `wf2_provider_config` → 1 · revisar
`NEEDS_RECONCILIATION` y `wf_reconciliation_issues`.

## F10 · Resto

| Template | Bloqueado por |
|---|---|
| `WF14_RECONCILIATION_V2` | nada |
| `WF10_RECORDINGS_V2` | PV-2, PV-4, PV-5 |
| `WF3_ACCOUNT_CREATION_V2` | D-6 (market IND vs ATL_IND) |
| `WF7_8_PAYMENT_CALLBACK_V2` | contrato Monetix |
| dashboard visual | nada: la capa de consultas ya existe |

| Fase | Toca producción | Reversible |
|---|---|---|
| F0–F6 | **no** | — |
| F7 | crea tablas nuevas + panel | tablas inertes · restaurar panel |
| F8 | sí, solo México de prueba | panel, 1 clic |
| F9 | sí, India | ≈30 s |
| F10 | sí, por template | por template |
