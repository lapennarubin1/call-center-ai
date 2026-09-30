# PRODUCTION_DEPLOYMENT_PLAN.md
**Plan de paso a producción, ruta por ruta.**
Fase BUILD · 21/09/2026 · `NO EJECUTADO`

> Este documento describe lo que **habría que hacer**. En esta fase no se
> ejecutó ningún paso.

---

## 0. Principio del despliegue

> **v1 y V2 conviven. Se migra una RUTA por vez, no el sistema entero.**

El interruptor de ruta es lo que hace esto posible: mientras `IN_PROVEEDOR1`
llama por V2, `IN_STRINGEE` puede seguir llamando por v1. Y volver atrás es
apagar una ruta en el panel, no revertir un despliegue.

**Regla que no se negocia:** una ruta está en v1 **o** en V2, nunca en las dos.
El claim atómico protege la base de V2; **no** puede evitar que v1 —que no lo
usa— tome el mismo lead.

---

## 1. Requisitos antes de empezar

- [ ] staging corrió el ciclo completo al menos **una semana**
- [ ] `tests/run_all_report.py` → 604 PASS · 0 FAIL · 0 SKIP en staging
- [ ] `PENDING_VERIFICATION.md` revisado; los bloqueantes resueltos o aceptados
- [ ] `ROLLBACK_PLAN.md` leído por quien vaya a ejecutar
- [ ] backup de la base de producción **de hoy**, verificado restaurable
- [ ] ventana acordada: **fuera del horario de llamadas** del país que se migra
- [ ] alguien mirando el panel durante las primeras 2 horas

---

## 2. Fase A — Migración de esquema (sin efecto visible)

La migración es aditiva: crea tablas nuevas y **no altera ninguna de v1**.
Se puede aplicar con el sistema corriendo.

```bash
# A1 · backup
mysqldump --single-transaction --quick --no-tablespaces \
  -u root -p asterisk | gzip > /backup/asterisk_$(date +%F_%H%M).sql.gz
gunzip -t /backup/asterisk_*.sql.gz && echo "backup OK"

# A2 · ver qué tablas de v1 ya existen (para saber si 003 hará algo)
mysql -u root -p asterisk -e "
  SELECT TABLE_NAME FROM information_schema.TABLES
   WHERE TABLE_SCHEMA='asterisk'
     AND TABLE_NAME IN ('crm_leads','crm_conversions','stringee_calls',
                        'panel_sync_log','wf10_sent_recordings','wf2_provider_config')"

# A3 · aplicar
mysql -u root -p asterisk < sql/migration.sql

# A4 · verificar
mysql -u root -p asterisk -e "
  SELECT migration_id, applied_at FROM schema_migrations ORDER BY applied_at;
  SELECT iso, country_name, enabled FROM countries;
  SELECT code, adapter_key, enabled FROM voice_providers;
  SELECT route_key, iso, enabled, archived_at FROM call_routes;"
```

**Punto de control A:** las tablas nuevas existen, el seed entró, y **ninguna
tabla de v1 cambió de forma ni perdió filas**.

> Si alguna tabla de v1 ya existía, `003` es un no-op para ella: usa
> `CREATE TABLE IF NOT EXISTS` y no ejecuta ningún `ALTER`.

**Efecto sobre v1: ninguno.** v1 no conoce estas tablas.

---

## 3. Fase B — Panel V2 (sin efecto sobre las llamadas)

> **Esta fase se reescribió por completo.** La versión anterior decía que
> sólo cambiaban «3 líneas en `server.py` y 2 en `base.html`». Era falso, y
> peligroso: estaba escrita contra el panel de la FUNDACIÓN, no contra el
> que corre en producción.
>
> El panel de producción **no tiene** los módulos de V2. Copiar `server.py`
> sin ellos deja el panel **sin arrancar**, porque importa módulos que no
> existen. El inventario exacto está en
> `PANEL_PRODUCTION_PATCH_MANIFEST.md`, generado comparando contra
> `landmark-panel-safe.tar.gz`.

### Lo que realmente hay que copiar

| | Ficheros |
|---|---:|
| **Nuevos** (no existen en producción) | **23** |
| **Modificados** | **4** |
| Sin cambios | 15 |

Los nuevos incluyen los 13 módulos de la aplicación
(`routes_config.py`, `call_jobs.py`, `followup_engine.py`,
`ops_events.py`, `analytics_v2.py`, `v2_suite.py`, `billing.py`,
`legacy_mode.py`, `payments.py`, `wf_settings.py`, `crm_notes.py`,
`tool_requests.py`, `recording_ledger.py`) y las 7 plantillas nuevas.

### B1 · Ver qué va a cambiar, sin tocar nada

```bash
cd /ruta/al/paquete/landmark-callcenter-v2
sudo DRY_RUN=1 bash panel/upgrade_callcenter_v2.sh
```

Lista fichero por fichero qué es nuevo y qué se modifica. **No escribe un
solo byte.** Revisar esa lista contra
`PANEL_PRODUCTION_PATCH_MANIFEST.md` antes de seguir.

### B2 · Preparar las credenciales

Esto es **nuevo y obligatorio**: las cuatro contraseñas que antes tenían
un valor por defecto escrito en el código ya no lo tienen.

```bash
sudo grep -c . /opt/landmark-panel/.env        # ¿existe y tiene contenido?
for V in LM_DB_PASS LM_PASS LM_MASTER_PASS LM_SUPPORT_PASS; do
  sudo grep -q "^$V=." /opt/landmark-panel/.env && echo "OK   $V" || echo "FALTA $V"
done
```

Cualquier `FALTA` hay que resolverlo **antes** de actualizar: el panel no
arrancará. Los valores que estaban en el código eran
`LM_DB_PASS`, `LM_PASS`, `LM_MASTER_PASS` y `LM_SUPPORT_PASS`; si nunca se
pusieron en el `.env`, el panel venía usando los del código. Sácalos del
`server.py` anterior —que sigue en el respaldo— **y aprovecha para
cambiarlos**: llevan tiempo en un fichero de código.

Además, para que el modo de operación pueda verificar contra n8n:

```
LM_N8N_BASE_URL=
LM_N8N_API_KEY=
```

Sin estas dos, el panel no podrá confirmar si el despachador legacy está
apagado, y el cambio a `V2_PRIMARY` quedará **bloqueado**. Es deliberado.

Plantilla completa con todas las variables: `panel/.env.example`.

### B3 · Actualizar

```bash
sudo bash panel/upgrade_callcenter_v2.sh
```

El script, por orden:

1. comprueba que `/opt/landmark-panel` es lo que parece
2. **respalda el panel entero** en `/opt/landmark-panel-backups/<fecha>`
3. copia módulos, plantillas y estáticos a un área de trabajo
4. **conserva** `.env`, `venv`, `logs`, `.session_key` y los datos
5. **valida antes de tocar el panel vivo**: compila todo el Python,
   importa el servidor de verdad, comprueba que el cerrojo del modo quedó
   instalado y que todas las plantillas Jinja parsean
6. sólo si todo eso pasa, aplica y reinicia
7. si el servicio no arranca, imprime el log y **cómo volver atrás**

**Si la validación falla, el panel no se toca y sigue corriendo con el
código anterior.**

### B4 · Comprobar

```bash
systemctl status landmark-panel
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/health          # 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/               # 302 sin sesión
```

Y en el navegador, que lo de siempre siga ahí: **Dashboard**, **SIP
Balance**, **Extensions**, **Support**, **Call Center** con sus grupos y
horarios. Más las pantallas nuevas: **Countries**, **Billing**,
**Legacy Backup**, **Analytics**, **Reconciliation**, **Settings**.

### B5 · El modo de operación

Tras la migración el modo es **`LEGACY_BACKUP`**. Es deliberado: el
sistema que está autorizado a llamar sigue siendo el de siempre.

En **Call Center → Legacy Backup** debería verse:

- modo `LEGACY_BACKUP`
- la columna **Running now** con el estado real de cada grupo legacy

Si ahí sale «Cannot reach n8n», faltan `LM_N8N_BASE_URL` o
`LM_N8N_API_KEY`. Resolverlo ahora, no el día del cutover.

**Esta fase no cambia ni una llamada.** V2 no puede llamar mientras el
modo sea `LEGACY_BACKUP`, aunque se configuren países y rutas.

### B6 · Vuelta atrás de esta fase

```bash
sudo systemctl stop landmark-panel
sudo rm -rf /opt/landmark-panel
sudo cp -a /opt/landmark-panel-backups/<fecha> /opt/landmark-panel
sudo systemctl start landmark-panel
```

El respaldo incluye el `.env`. La base de datos no se toca en esta fase,
así que no hay nada que revertir ahí.

## 4. Fase C — Configuración (sin efecto: todo nace apagado)

Desde el panel, **sin encender nada todavía**:

1. **Countries** — revisar prefijo, huso, idioma de cada país
2. **Providers** — confirmar `adapter_key` y endpoint
3. **Routes** — por cada ruta que vaya a migrar:
   - `elevenlabs_agent_id`, `elevenlabs_phone_number_id` / `caller_id`
   - `capacity_default` = **la capacidad real** (no el `BATCH_SIZE` de v1)
   - franjas horarias, si el negocio las quiere
   - política de follow-up
   - grabaciones: mínimo de duración, destino de Telegram
   - debe decir **READY**
4. **Tool configs** — `CREATE_ACCOUNT`, `CREATE_PAYMENT_LINK`, `CALLBACK` por país
5. **Settings** — revisar los umbrales de reconciliación

> Una ruta puede quedar `READY` y apagada indefinidamente. `READY` es
> "configuración completa"; `ON` es "empezá a llamar". Son decisiones distintas.

**Punto de control C:** `/api/routes/active` sigue devolviendo `{"routes": []}`.

---

## 5. Fase D — n8n: importar y NO activar

Seguir `INSTALL_STAGING.md` §5–§7, pero apuntando a **producción**:

- credenciales de producción
- `LM_PANEL_URL` al panel de producción (**puerto 8080**)
- base MySQL de producción
- los siete workflows **importados y desactivados**

**Punto de control D:** ningún workflow V2 activo. v1 sigue llamando normal.

---

## 5b. Fase D2 — EL CUTOVER: `LEGACY_BACKUP` → `V2_PRIMARY`

Este es **el momento** del despliegue. Hasta aquí nada llamó a nadie por
V2, hiciera lo que hiciera el operador: el modo era `LEGACY_BACKUP` y
`/api/routes/active` devolvía cero rutas invocables.

Cambiar el modo es lo único que habilita a V2 a llamar. Por eso el panel
lo trata como un evento deliberado, no como un interruptor.

### D2.1 · Qué comprueba el panel, por ti

Al pulsar el cambio, el panel **consulta a n8n** el estado real de cada
grupo legacy y **bloquea** si:

- algún grupo conflictivo (`DISPATCH`, `FOLLOWUP`, `ACCOUNT`, `PAYMENT`,
  `POST_CALL`) sigue **encendido**
- algún grupo sigue **sin clasificar**
- **n8n no responde** — no saber si el despachador viejo está encendido no
  es lo mismo que saber que está apagado

Los grupos compatibles (`CRM_SYNC`, `ANALYTICS`, `RECORDING`) pueden
quedarse corriendo.

### D2.2 · Antes de pulsar

```bash
# ¿alguna ruta V2 quedaría llamando en cuanto cambie el modo?
mysql <base> -e "
  SELECT r.route_key FROM call_routes r
    JOIN voice_providers vp ON vp.id = r.provider_id
    JOIN countries c ON c.iso = r.iso
   WHERE r.enabled=1 AND r.archived_at IS NULL
     AND vp.enabled=1 AND c.enabled=1 AND c.archived_at IS NULL;"
```

Con el modo aún en `LEGACY_BACKUP` esa consulta puede devolver filas: son
las rutas que empezarán a llamar **en cuanto se cambie el modo**. Si hay
más de la que se quiere estrenar, apagar el resto ahora.

### D2.3 · El cambio

**Call Center → Legacy Backup**, sólo rol `master`:

1. comprobar que **Running now** no muestra ningún grupo conflictivo en ON
2. teclear exactamente `SWITCH TO V2`
3. escribir el motivo
4. cambiar

Queda auditado: quién, cuándo, desde qué modo, hacia cuál y por qué.

### D2.4 · Comprobar

```bash
curl -s -H "X-Service-Token: <token>" \
  http://127.0.0.1:8080/api/routes/active | python3 -m json.tool | head -20
```

Tiene que decir `"operating_mode": "V2_PRIMARY"` y
`"dispatch_allowed": true`. Si no, WF2 **no llamará** — y eso es correcto,
no un fallo.

### D2.5 · Vuelta atrás

Volver a `LEGACY_BACKUP` es el mismo circuito en sentido contrario, y
exige que ninguna ruta V2 esté viva. Es la forma rápida y reversible de
parar V2 entero sin borrar nada.

---

## 6. Fase E — Primera ruta, la más chica

> Con el modo ya en `V2_PRIMARY` (Fase D2). Mientras siga en
> `LEGACY_BACKUP`, encender una ruta no hace que llame nadie.

Elegir la ruta de **menor volumen** (p. ej. `NP_PROVEEDOR1`, hoy apagada en v1).

### E1 · desconectar esa ruta de v1

```sql
-- si la ruta corresponde a un proveedor completo de v1
UPDATE wf2_provider_config SET enabled = 0 WHERE provider = '<proveedor>';
```

Si v1 no puede apagar esa ruta por separado, **hay que desactivar el WF2 v1
entero** y migrar todas sus rutas a la vez. Decidirlo **antes** de empezar.

### E2 · confirmar que v1 dejó de llamar

```sql
SELECT MAX(last_call_time) FROM crm_leads;   -- debe dejar de avanzar
```

Y en n8n: Executions de WF2 v1 sin despachos nuevos.

### E3 · activar V2, en este orden

```
1. TEMPLATE_FOLLOWUP_ENGINE_V2
2. TEMPLATE_WF9_POST_CALL_HANDLER_V2
3. TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2
4. TEMPLATE_WF10_RECORDINGS_V2
5. TEMPLATE_WF3 / TEMPLATE_WF7_8
6. TEMPLATE_WF2_CALL_DISPATCHER_V2   ← el último
```

### E4 · encender la ruta con capacidad 1

Panel → esa ruta → `capacity_default = 1` → ON → proveedor ON → país ON.

**Una llamada por ciclo.** Mirar la primera de punta a punta:

```sql
SELECT call_job_id, lead_id, route_key, provider, attempt, state, result,
       duration_seconds, followup_id, error_code, created_at, completed_at
  FROM wf_call_jobs ORDER BY created_at DESC LIMIT 5;
```

Debe recorrer `CLAIMED → DISPATCHING → DISPATCHED → COMPLETED`, con
`followup_id` al final.

En el CRM: el follow-up existe, tiene `scheduleNextAt` correcto y la nota está
**en inglés**.

### E5 · subir la capacidad, de a poco

```
1 → 2 → 5 → la capacidad real
```

Entre cada escalón, revisar:

```sql
SELECT state, COUNT(*) FROM wf_call_jobs
 WHERE created_at > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR) GROUP BY state;

SELECT error_code, COUNT(*) FROM wf_call_jobs
 WHERE state='RELEASED' AND updated_at > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)
 GROUP BY error_code;
```

- muchos `RELEASED` → el proveedor está rechazando: **bajar la capacidad**
- `NEEDS_RECONCILIATION` creciendo → **parar** y revisar `/callcenter/issues`
- `answer_rate` muy distinto al de v1 → comparar antes de seguir

**Punto de control E:** 24 horas con una ruta en V2, `answer_rate` comparable al
de v1, cero issues sin explicar.

---

## 7. Fase F — El resto de las rutas

Una por vez, repitiendo E1–E5. Entre rutas, **al menos un día hábil completo**.

Orden sugerido: de menor a mayor volumen. `IN_PROVEEDOR1` (la de más tráfico) va
al final.

---

## 8. Fase G — Apagar v1

Cuando **todas** las rutas estén en V2 y hayan pasado **30 días**:

1. desactivar los workflows v1 en n8n (**no borrarlos**)
2. dejar las tablas de v1 intactas
3. revisar que no quede nada apuntando a los webhooks de v1

> v1 no se borra. Mínimo 30 días desactivado, y después es una decisión aparte.

---

## 9. Qué mirar los primeros días

| cada | qué |
|---|---|
| 15 min, primer día | `/callcenter/analytics` — intentos, tasa de respuesta, fallos técnicos |
| 1 h, primera semana | `/callcenter/issues` — que no crezca |
| diario | `answer_rate` de V2 vs el histórico de v1, por ruta |
| diario | `SELECT state, COUNT(*) FROM wf_call_jobs GROUP BY state` |
| diario | follow-ups creados vs llamadas con resultado (deberían ir a la par) |

### Umbrales para frenar

| señal | acción |
|---|---|
| `answer_rate` cae > 20 % vs v1 | **parar la ruta**, comparar la clasificación de resultados |
| > 10 `NEEDS_RECONCILIATION` en una hora | **parar**, revisar issues |
| > 30 % de despachos en `RELEASED` | bajar capacidad; si sigue, parar |
| follow-ups duplicados en el CRM | **parar todo**, es el escenario que más cuesta revertir |
| leads llamados dos veces seguidas | **parar todo**: v1 y V2 en la misma ruta |

---

## 10. Lo que NO cambia en producción

- Asterisk: ni `pjsip.conf` ni el dialplan
- el worker Stringee: ni una línea (ver `STRINGEE_MINIMAL_PATCH_PROPOSAL.md`)
- los workflows v1: no se editan ni se renombran
- las tablas de v1: `crm_leads`, `crm_conversions`, `stringee_calls`,
  `wf_call_followups`, `wf10_sent_recordings`, `wf2_provider_config`,
  `n8n_switches`, `sip_*`, `app_settings`
- ElevenLabs: agentes, prompts, voces y knowledge base

---

## 11. Rollback

Cualquier punto de control que falle → `ROLLBACK_PLAN.md`.

El resumen: **apagar la ruta en el panel**. Efecto en el ciclo siguiente de WF2,
sin tocar n8n, sin tocar la base, sin revertir nada.
