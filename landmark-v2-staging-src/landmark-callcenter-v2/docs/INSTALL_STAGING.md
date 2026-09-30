# INSTALL_STAGING.md
**Instalar el suite V2 en un entorno de STAGING.**
Fase BUILD · 21/09/2026

> Producción no se toca en ningún paso de este documento.
> v1 sigue corriendo, intacto, todo el tiempo.

---

## 0. Antes de empezar

| requisito | verificación |
|---|---|
| MariaDB 10.11+ (probado en 10.11.14) | `mysql -e "SELECT VERSION()"` |
| base de staging **separada** de producción | nombre distinto, usuario distinto |
| n8n con acceso a esa base y al panel por red interna | ver §4 |
| Python 3.11+ para el panel y los tests | `python3 -V` |
| Node (solo para correr los tests del motor) | `node -v` |

**Lo que NO hace falta:** tocar Asterisk, tocar el worker Stringee, tocar los
workflows v1, tocar la base de producción.

---

## 1. Copiar la base de producción a staging (recomendado)

El suite convive con tablas de v1 (`crm_leads`, `stringee_calls`,
`panel_sync_log`). Probar sobre una copia real evita sorpresas de forma.

```bash
# en el VPS, como el usuario que ya tiene acceso de lectura
mysqldump --single-transaction --quick --no-tablespaces \
  -u <user> -p asterisk > /tmp/asterisk_snapshot.sql

mysql -u root -p -e "CREATE DATABASE asterisk_staging CHARACTER SET utf8mb4"
mysql -u root -p asterisk_staging < /tmp/asterisk_snapshot.sql
```

Si no se puede copiar producción, la migración crea versiones mínimas de esas
tablas (`003_legacy_compat_tables.sql`). **Ver PV-14**: en ese caso hay que
comparar las definiciones con las reales antes de sacar conclusiones.

---

## 2. Aplicar la migración

```bash
cd landmark-callcenter-v2

# 2.1 revisar qué va a hacer (no ejecuta nada)
less sql/migration.sql

# 2.2 aplicar
mysql -u <user> -p asterisk_staging < sql/migration.sql

# 2.3 aplicar OTRA VEZ: debe ser un no-op
mysql -u <user> -p asterisk_staging < sql/migration.sql

# 2.4 verificar
mysql -u <user> -p asterisk_staging -e "
  SELECT migration_id, applied_at FROM schema_migrations ORDER BY migration_id;
  SELECT COUNT(*) AS paises FROM countries;
  SELECT COUNT(*) AS proveedores FROM voice_providers;
  SELECT COUNT(*) AS rutas FROM call_routes;
  SELECT COUNT(*) AS settings FROM wf_settings;"
```

Esperado:

```
001_multi_country_config_v2_2
001_multi_country_config_v2_2:seed
002_callcenter_suite_v2
002_callcenter_suite_v2:seed
003_legacy_compat_tables
paises 2 · proveedores 2 · rutas 3 · settings 12
```

> La migración es **idempetente, rerunnable y no destructiva**: no contiene
> ningún `DROP`, `TRUNCATE` ni `DELETE` ejecutable, y el seed corre **una sola
> vez** en la vida de la base. Una config editada desde el panel sobrevive a
> cualquier reejecución. Verificado en `tests/test_migration_suite_v2.py`.

### Permisos (ajustar a los usuarios reales — PV-2)

```sql
-- panel: lee todo, escribe configuración, resuelve issues
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.countries        TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.voice_providers  TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.followup_policies TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.call_routes      TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.country_tool_configs TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_settings      TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk_staging.route_capacity_windows  TO 'panel_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk_staging.route_telegram_targets TO 'panel_rw'@'%';
GRANT SELECT, INSERT ON asterisk_staging.route_audit TO 'panel_rw'@'%';
GRANT SELECT ON asterisk_staging.wf_call_jobs, asterisk_staging.wf_events TO 'panel_rw'@'%';
GRANT SELECT, UPDATE ON asterisk_staging.wf_reconciliation_issues TO 'panel_rw'@'%';

-- n8n: escribe ejecución y eventos, NUNCA configuración
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_call_jobs           TO '<n8n_user>'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_conversation_ledger TO '<n8n_user>'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_tool_requests       TO '<n8n_user>'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_recording_ledger    TO '<n8n_user>'@'%';
GRANT SELECT, INSERT, UPDATE ON asterisk_staging.wf_reconciliation_issues TO '<n8n_user>'@'%';
GRANT SELECT, INSERT ON asterisk_staging.wf_events TO '<n8n_user>'@'%';   -- append-only
GRANT SELECT ON asterisk_staging.wf_settings TO '<n8n_user>'@'%';         -- solo lectura

-- nadie borra call_routes ni wf_events
```

---

## 3. Instalar el panel V2

El panel V2 **no reescribe** el de la fundación: agrega 5 módulos, 3 plantillas y
un hook de 3 líneas en `server.py`.

```bash
# 3.1 copiar a una instancia de STAGING (nunca sobre /opt/landmark-panel)
sudo mkdir -p /opt/landmark-panel-staging
sudo cp -r panel/* /opt/landmark-panel-staging/

# 3.2 entorno
cd /opt/landmark-panel-staging
cp .env.example .env
chmod 600 .env
# y rellenar. Plantilla completa en panel/.env.example; lo mínimo es:
cat > .env <<'EOF'
# ── base de datos ──────────────────────────────────────────────────
LM_DB_HOST=localhost
LM_DB_PORT=3306
LM_DB_USER=panel_rw
LM_DB_PASS=<clave de panel_rw>
LM_DB_NAME=asterisk_staging

# ── servicio ───────────────────────────────────────────────────────
# 8081, NO 8080: 8080 es producción. Este puerto tiene que coincidir
# con el LM_PANEL_URL del n8n de staging (§4).
LM_PORT=8081

# ── los TRES roles ─────────────────────────────────────────────────
# Ninguna de estas contraseñas tiene valor por defecto en el código.
# Si falta una, el panel NO arranca y dice cuál. Es deliberado.
LM_USER=admin
LM_PASS=<clave del rol viewer>
LM_MASTER_USER=master
LM_MASTER_PASS=<clave del rol master>
LM_SUPPORT_USER=support
LM_SUPPORT_PASS=<clave del rol support>

# ── sesión y API de servicio ───────────────────────────────────────
LM_SECRET=<openssl rand -hex 32>
LM_ROUTES_API_TOKEN=<openssl rand -hex 32>

# ── API de n8n ─────────────────────────────────────────────────────
# Necesaria para el modo de operación: volver de LEGACY_BACKUP a
# V2_PRIMARY verifica contra n8n que ningún workflow legacy conflictivo
# siga corriendo. Sin esto, ese cambio queda BLOQUEADO.
LM_N8N_BASE_URL=<url de n8n>
LM_N8N_API_KEY=<api key de n8n>
EOF
chmod 600 .env

# Comprobar que no falta ninguna obligatoria antes de arrancar:
for V in LM_DB_PASS LM_PASS LM_MASTER_PASS LM_SUPPORT_PASS; do
  grep -q "^$V=." .env && echo "OK    $V" || echo "FALTA $V"
done

# 3.3 arrancar
set -a; . ./.env; set +a
gunicorn --chdir app --bind 0.0.0.0:8081 --workers 3 server:app
```

**Archivos que agrega el panel V2** (todos nuevos, salvo dos hooks mínimos):

```
app/v2_suite.py            pantallas y API nuevas
app/wf_settings.py         parámetros operativos
app/crm_notes.py           catálogo de textos del CRM (en inglés)
app/tool_requests.py       claim de las tools de país
app/recording_ledger.py    claim y correlación de grabaciones
app/templates/analytics.html
app/templates/issues.html
app/templates/settings.html
app/server.py              + import v2_suite  + v2.register(...)   (3 líneas)
app/templates/base.html    + 2 links de navegación
```

### La clave de sesión: `LM_SECRET`

**Ponla en el entorno.** Si no la pones, el panel se la inventa al arrancar y
**la escribe en `app/.session_key`**, con permisos 600, para que todos los
workers de gunicorn compartan la misma y el usuario no se desloguee al azar.

Eso funciona, pero deja un secreto dentro del directorio de la aplicación. Si
después alguien copia ese directorio, lo mete en git o lo empaqueta, el
secreto viaja con él. Con `LM_SECRET` en el entorno el fichero **no llega a
crearse**.

```bash
LM_SECRET=$(openssl rand -hex 32)          # añádelo al .env
```

El paquete se entrega **sin** `app/.session_key`, y el `.gitignore` de la raíz
lo excluye junto con `__pycache__` y `.env`. Si ves aparecer ese fichero en
staging, es que falta `LM_SECRET`.

> Cambiar `LM_SECRET` invalida las sesiones abiertas: todo el mundo vuelve a
> la pantalla de login. Nada más. No afecta a datos ni a llamadas.

Verificación:

```bash
curl -s localhost:8081/callcenter/analytics -o /dev/null -w '%{http_code}\n'   # 302 sin sesión
curl -s -H "X-Service-Token: <token>" localhost:8081/api/routes/active | head -c 200
curl -s -H "X-Service-Token: <token>" localhost:8081/api/settings | head -c 200
```

---

## 4. Conectividad n8n → panel

`/api/*` **no se consume por HTTP público**: el panel devuelve `403 insecure
transport` si el pedido llega por HTTP plano desde una IP pública.

Permitido: red interna Docker/VPS, o HTTPS (directo o con un proxy que ponga
`X-Forwarded-Proto: https`).

```bash
# desde DENTRO del contenedor de n8n
docker exec -it <n8n> sh -c \
  'wget -qO- --header="X-Service-Token: <token>" http://172.18.0.1:8081/api/routes/active | head -c 300'
```

> **PV-12:** hay que confirmar que el panel escucha en la interfaz del bridge
> Docker (`172.18.0.1`). WF2/WF10/WF14 v1 ya alcanzan al worker Stringee en
> `172.18.0.1:8091`, así que la ruta existe; falta confirmar el puerto del panel.

Si la IP o el puerto son otros, se ajusta la variable de entorno de n8n
`LM_PANEL_URL` — **no** se edita ningún nodo.

---

## 5. Credenciales de n8n

Los JSON traen **marcadores**, no IDs reales. Hay que crear estas credenciales y
asociarlas al importar:

| marcador en el JSON | tipo de credential | nombre sugerido | contenido |
|---|---|---|---|
| `__PANEL_TOKEN_CREDENTIAL__` | HTTP Header Auth | `Landmark Panel API` | `X-Service-Token: <LM_ROUTES_API_TOKEN>` |
| `__LEADSTUDIO_LOGIN_CREDENTIAL__` | HTTP Custom Auth | `LeadStudio Login` | `{"body":{"email":"…","password":"…"}}` |
| `__ELEVENLABS_CREDENTIAL__` | HTTP Header Auth | `ElevenLabs API` | `xi-api-key: sk_…` |
| `__MYSQL_CREDENTIAL__` | MySQL | `Landmark MySQL` | host/base/usuario de staging |
| `__TELEGRAM_CREDENTIAL__` | Telegram | `Landmark Telegram` | token del bot |
| `__COUNTRY_TOOL_CREDENTIAL__` | HTTP Header Auth | `Country Tool API` | solo si algún país usa `CUSTOM_ENDPOINT` |
| `__PAYMENT_TOOL_CREDENTIAL__` | HTTP Header Auth | `Payment Provider API` | idem |
| `__SMS_CREDENTIAL__` | HTTP Header Auth | `SMS Gateway` | solo si se entrega link por SMS |

### Variables de entorno de n8n

```
LM_PANEL_URL=http://172.18.0.1:8081
LM_LEADSTUDIO_URL=https://lead-studio-9gnl.onrender.com
LM_STRINGEE_WORKER_URL=http://172.18.0.1:8091
LM_WF9_CALLBACK_URL=https://<n8n>/webhook/stringee-callback-v2
LM_WORKER_CALLBACK_TOKEN=<token largo>     # opcional, autentica el callback
ELEVENLABS_API_KEY=sk_…                    # solo para el polling de WF9 y WF10
ELEVENLABS_WEBHOOK_SECRET=wsec_…           # HMAC del post-call
```

> **Importante:** con `ELEVENLABS_WEBHOOK_SECRET` sin configurar, WF9 **rechaza**
> todo post-call por webhook (fail-closed, a propósito). v1 procesaba igual con
> la firma inválida; V2 no.

---

## 6. Importar los workflows

**Orden obligatorio** — el motor primero, porque los demás lo referencian:

```
1. TEMPLATE_FOLLOWUP_ENGINE_V2.json
2. TEMPLATE_WF2_CALL_DISPATCHER_V2.json
3. TEMPLATE_WF9_POST_CALL_HANDLER_V2.json
4. TEMPLATE_WF3_ACCOUNT_CREATION_V2.json
5. TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2.json
6. TEMPLATE_WF10_RECORDINGS_V2.json
7. TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2.json
```

Los siete vienen con `active: false`: **no arrancan al importarse**.

Después de importar el motor, hay que **reapuntar los nodos `Execute Workflow`**
de WF2 y WF9 al ID real que n8n le asignó (en el JSON viene el marcador
`__WORKFLOW_ID_TEMPLATE_FOLLOWUP_ENGINE_V2__`).

### Checklist post-import, por workflow

- [ ] todas las credenciales asociadas (ningún nodo en rojo)
- [ ] el nodo MySQL apunta a la base de **staging**
- [ ] los `Execute Workflow` apuntan al motor importado
- [ ] los webhooks tienen la URL que espera cada origen (§7)
- [ ] sigue **desactivado**

---

## 7. URLs de webhook

| workflow | path | quién lo llama |
|---|---|---|
| WF9 | `/webhook/elevenlabs-postcall-v2` | ElevenLabs (post-call) |
| WF9 | `/webhook/stringee-callback-v2` | el worker Stringee (`callback_url`) |
| WF3 | `/webhook/create-account-v2` | tool del agente |
| WF7 | `/webhook/payment-link-v2` | tool del agente |
| WF7 | `/webhook/callback-request-v2` | tool del agente |
| WF8 | `/webhook/payment-callback-v2` | proveedor de pagos |
| WF2 | `/webhook/dispatch-now-v2` | manual, para probar |

> **En staging, NO apuntar ElevenLabs ni el proveedor de pagos a estas URLs**
> mientras v1 siga atendiendo las suyas: se duplicaría el procesamiento. Probar
> con `curl` contra el webhook de staging (§8).

---

## 8. Prueba en seco, antes de llamar a nadie

### 8.1 la batería completa

```bash
cd landmark-callcenter-v2
LM_TEST_MYSQL_USER=<user> LM_TEST_MYSQL_PASS=<pass> \
  python3 tests/run_all_report.py
```

Esperado: **604 PASS · 0 FAIL · 0 SKIP** (412 del suite V2 + 192 de la
fundación V2.2).

### 8.2 la API responde lo que WF2 espera

```bash
curl -s -H "X-Service-Token: <token>" \
  http://127.0.0.1:8081/api/routes/active | python3 -m json.tool | head -40
```

Con todo apagado, `routes` debe venir **vacío**. Eso es correcto: fail-closed.

### 8.3 una ruta de prueba, con capacidad 1

1. Panel → Countries & Routes → crear/editar la ruta
2. Completar hasta que diga **READY**
3. `capacity_default = 1`
4. Encender: ruta ON → proveedor ON → país ON
5. Confirmar en `/api/routes/active` que aparece con `calling_now: true`

### 8.4 un ciclo de WF2, a mano

```bash
curl -X POST https://<n8n-staging>/webhook/dispatch-now-v2
```

Y mirar:

```sql
SELECT call_job_id, lead_id, route_key, provider, attempt, state, result,
       error_code, created_at
  FROM wf_call_jobs ORDER BY created_at DESC LIMIT 10;

SELECT event_type, COUNT(*) FROM wf_events GROUP BY event_type;
```

### 8.5 un post-call simulado

```bash
curl -X POST https://<n8n-staging>/webhook/stringee-callback-v2 \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <LM_WORKER_CALLBACK_TOKEN>' \
  -d '{"job_id":"test-1","lead_id":"<uuid del lead en vuelo>",
       "final_status":"NO_ANSWER","answered":false,"sip_code":"408",
       "answered_at":null,"finished_at":"2026-09-21T10:00:00.000Z"}'
```

Verificar que:

- el job pasó a `COMPLETED` con `result = 'NO_ANSWER'`
- hay un `CALL_RESULT` en `wf_events`
- hay un follow-up creado con `scheduleNextAt` = ahora + 2 h (intento 1)
- la nota en el CRM está **en inglés**

### 8.6 Analytics

`http://<staging>:8081/callcenter/analytics` debe mostrar los números de arriba,
**sin** que LeadStudio intervenga.

---

## 8b. El modo de operación arranca en LEGACY_BACKUP

Tras la migración, el modo es **`LEGACY_BACKUP`**. Significa que el
sistema autorizado a llamar es el legacy, no V2.

**Mientras el modo sea `LEGACY_BACKUP`, V2 no llama a nadie**, aunque se
enciendan país, proveedor y ruta: `/api/routes/active` devuelve cero
rutas invocables y WF2 termina antes de pedir leads.

Eso es deliberado. Se puede configurar V2 entero —países, rutas, tarifas,
tools, políticas— sin riesgo de que empiece a marcar. Para que llame hay
que hacer el cutover explícito en **Call Center → Legacy Backup**, que
verifica contra n8n que el despachador viejo está apagado.

En staging, si no hay ningún workflow legacy conflictivo encendido, el
cutover pasa sin más. Pero **sigue haciendo falta hacerlo**, y conviene
practicarlo aquí antes que en producción.

Comprobación rápida:

```bash
curl -s -H "X-Service-Token: <token>" \
  http://127.0.0.1:8081/api/routes/active | python3 -m json.tool | head -12
# "operating_mode": "LEGACY_BACKUP"  ·  "dispatch_allowed": false  ·  count 0
```

---

## 9. Orden de activación en staging

Uno por vez, verificando entre cada paso:

```
1. TEMPLATE_FOLLOWUP_ENGINE_V2     (sub-workflow: no corre solo)
2. TEMPLATE_WF9_POST_CALL_HANDLER  (solo recibe; no llama a nadie)
3. TEMPLATE_WF14_RECONCILIATION    (solo lee y reconcilia)
4. TEMPLATE_WF10_RECORDINGS        (solo lee grabaciones)
5. TEMPLATE_WF3 / WF7_8            (solo responden a tools)
6. TEMPLATE_WF2_CALL_DISPATCHER    ← el ÚLTIMO: es el único que llama
```

WF2 va al final a propósito: hasta que se active, **nadie marca un teléfono**.

---

## 10. Señales de que algo está mal

| síntoma | dónde mirar |
|---|---|
| WF2 no despacha nada | `/api/routes/active` devuelve `[]` → revisar los 3 interruptores y `blocked_by` |
| `403 insecure transport` | n8n llega por HTTP público → usar la red interna (§4) |
| `503 CONFIG_ERROR` en la API | falta `LM_ROUTES_API_TOKEN` en el panel |
| WF9 rechaza todos los webhooks | falta `ELEVENLABS_WEBHOOK_SECRET` (es a propósito) |
| jobs atascados en `CLAIMED` | WF2 muere entre el claim y el dispatch → revisar Executions |
| muchos `RELEASED` | el proveedor rechaza: `SELECT error_code, COUNT(*) FROM wf_call_jobs WHERE state='RELEASED' GROUP BY 1` |
| `NEEDS_RECONCILIATION` creciendo | `/callcenter/issues` |
| grabaciones `ORPHAN` | el post-call no llegó antes que la grabación; revisar WF9 |

---

## 11. Qué NO hacer en staging

- ❌ apuntar el panel de staging a la base de **producción**
- ❌ usar el puerto **8080** (es el de producción)
- ❌ reapuntar los webhooks de ElevenLabs de producción a staging
- ❌ activar WF2 V2 con las mismas rutas que v1 está llamando: los dos sistemas
  tomarían los mismos leads (el claim protege la base de V2, **no** a v1)
- ❌ tocar `wf2_provider_config` (es de v1)
