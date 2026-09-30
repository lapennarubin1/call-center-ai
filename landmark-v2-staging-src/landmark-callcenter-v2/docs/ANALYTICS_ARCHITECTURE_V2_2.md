# ANALYTICS_ARCHITECTURE_V2_2.md
**Capa local de datos operativos del Landmark Panel**
21/09/2026 · Implementación de referencia: `app/call_jobs.py`, `app/ops_events.py`,
`app/analytics_v2.py` · Contrato: `ANALYTICS_EVENT_CONTRACT_V2_2.json`

---

## 1. El cambio

| Antes (v1) | Ahora (V2.2) |
|---|---|
| El dashboard depende de WF14, que copia LeadStudio → MySQL cada 5 min | Cada workflow escribe **localmente** lo que pasa, **cuando** pasa |
| Correlación por `RIGHT(phone,10)` y prefijo | IDs explícitos: `call_job_id`, `lead_id`, `route_key`, `provider`, `country_iso`, `conversation_id` |
| Si LeadStudio falla, el dashboard se queda sin datos | El dashboard no depende de LeadStudio |
| WF14 es la fuente de analytics | WF14 **reconcilia**: compara y abre issues, no es la fuente |

Regla: **el panel nunca llama a LeadStudio para un KPI, un gráfico, un refresh o una vista
por país.** Probado: `test_http_v2_2.py` bloquea `socket.connect` durante la request a
`/callcenter/analytics.json` y verifica cero intentos de conexión.

---

## 2. El mínimo correcto: 2 tablas nuevas, 1 ampliada

Antes de crear nada revisé lo existente:

| Tabla existente | ¿Sirve como fuente V2? | Decisión |
|---|---|---|
| `crm_leads` | réplica del CRM, correlacionada por teléfono | **se conserva** como réplica para reporting del estado del lead; no es fuente de llamadas |
| `crm_conversions` | réplica de cuentas desde el CRM | **se conserva**; WF14 la usa para **reconciliar** contra `ACCOUNT_CREATED` local |
| `stringee_calls` | call-log del worker; `lead_id` por `RIGHT(phone,10)` | **se conserva** para reconciliar volumen/duración de Stringee; no es fuente V2 |
| `wf_call_followups` | followups de v1 correlacionados por teléfono | **no se usa en V2**: `wf_call_jobs.followup_id` + ledger lo reemplazan |
| `wf_call_jobs` (V2.1) | ya existe, una fila por llamada | **se amplía** y pasa a ser el hecho de llamadas |

Resultado:

| Tabla | Tipo | Para qué |
|---|---|---|
| **`wf_call_jobs`** (ampliada) | hecho, 1 fila por llamada | métricas de **llamadas**: intentos, resultado, duración. Resultado y duración se escriben **una sola vez** |
| **`wf_events`** (nueva) | log append-only, idempotente | métricas de **negocio** (cuentas, pagos, tools, follow-ups, grabaciones) + **traza** de cada llamada |
| **`wf_reconciliation_issues`** (nueva) | estado mutable | diferencias local ↔ CRM/proveedor, con ciclo OPEN → RESOLVED/IGNORED |

**No** creé `wf_call_events` y `wf_business_events` por separado: un solo `wf_events` con
`event_domain` (CALL, FOLLOWUP, TOOL, RECORDING) cubre ambos con los mismos índices.
**Sí** separé los issues: tienen ciclo de vida, y un log append-only no debe editarse.

### Por qué las llamadas se miden del hecho y no de los eventos

Si "minutos hablados" fuera `SUM(duration)` sobre eventos, un evento duplicado duplicaría
minutos. En `wf_call_jobs` la duración se escribe con `WHERE result IS NULL`: el primer
escritor gana y los siguientes no pueden sumar. **La idempotencia la da la forma del dato,
no la disciplina de quien escribe.** Probado con 40 escritores simultáneos.

### Por qué un solo `CALL_RESULT` y no `CALL_ANSWERED` / `CALL_NO_ANSWER` / …

Con un tipo por resultado, un webhook que dice ANSWERED y un polling que dice NO_ANSWER
quedarían guardados los dos, y el dashboard contaría una llamada contestada **y** una no
contestada. Con una clave por llamada (`CALL_RESULT:{call_job_id}`), el segundo choca con el
UNIQUE; si contradice al primero, se abre un issue `RESULT_CONFLICT`.

---

## 3. Quién escribe qué, y cuándo (near real-time)

| Momento | Workflow | Escritura local |
|---|---|---|
| claim | WF2 | fila en `wf_call_jobs` + `CALL_CLAIMED` |
| proveedor acepta | WF2 | `DISPATCHED` + `CALL_DISPATCHED` |
| 401 / 403 / worker caído | WF2 | `RELEASED` + `CALL_TECH_FAILED` (no consume intento) |
| timeout | WF2 | `UNKNOWN` + `CALL_UNKNOWN` |
| SIP 603 inmediato | WF2 | `record_call_result()` → `CALL_RESULT` |
| post-call `ANSWERED, 327 s` | WF9 | `record_call_result()` → **visible en el panel en segundos** |
| followup creado | motor | `FOLLOWUP_CREATED` (+ `CALLBACK_SCHEDULED` / `LEAD_CLOSED`) |
| cuenta creada | WF3 | `ACCOUNT_CREATED` → **visible sin consultar LeadStudio** |
| link de pago | WF7 | `PAYMENT_LINK_CREATED` (monto y moneda) |
| pago confirmado | WF8 | `PAYMENT_CONFIRMED` |
| grabación | WF10 | `RECORDING_ATTACHED` / `_SKIPPED_SHORT` / `_MISSING` |
| cada 5 min | WF14 | **compara** con LeadStudio y call-log → `wf_reconciliation_issues` |

WF9 escribe el resultado **antes** de crear el followup en LeadStudio: si el CRM está caído,
el panel igual muestra la llamada.

---

## 4. Definiciones de métricas (contrato)

Si una definición cambia, cambia el número del panel. Por eso están escritas.

| Métrica | Definición exacta | Fuente |
|---|---|---|
| **calls / attempted** | llamadas que pudieron salir: state ∈ DISPATCHING, DISPATCHED, UNKNOWN, COMPLETED, NEEDS_RECONCILIATION. **Excluye** RELEASED (técnico), CLAIMED y FAILED | jobs |
| dispatched | `dispatched_at` no nulo (aceptadas o con resultado inmediato) | jobs |
| with_result | `result` no nulo | jobs |
| **answered** | **contestó** = result ∈ ANSWERED, CALLBACK (pedir callback implica haber hablado) | jobs |
| no_answer · busy · voicemail · wrong_number · dnc | result = cada valor | jobs |
| callbacks | result = CALLBACK (subconjunto de answered) | jobs |
| **answer_rate** | answered / with_result — **no** / attempted: las llamadas en curso no deben bajar la tasa | jobs |
| **talk_seconds / minutes** | SUM(duration_seconds) de las answered | jobs |
| avg_talk_seconds | talk_seconds / answered con duración | jobs |
| failed_technical | eventos `CALL_TECH_FAILED` (no son llamadas) | events |
| needs_reconciliation | state ∈ UNKNOWN, NEEDS_RECONCILIATION | jobs |
| in_flight | state ∈ CLAIMED, DISPATCHING, DISPATCHED | jobs |
| **accounts_opened** | eventos `ACCOUNT_CREATED` (uno por lead y proveedor) | events |
| account_requests / failures / already_existed | eventos respectivos | events |
| payment_link_requests / created / failures · payments_confirmed | eventos respectivos | events |
| followups_created · callbacks_scheduled · leads_closed | eventos del motor | events |
| **closed_max_attempts** | `LEAD_CLOSED` con result = MAX_ATTEMPTS | events |
| recordings_attached / below_min / missing | eventos de WF10 | events |
| **conversion_rate** | accounts_opened / answered del mismo rango — **PENDING_PRODUCT_DECISION** | ambos |

Filtros de tiempo: llamadas por `created_at` (hora del claim), eventos por `occurred_at`.
Todo en **UTC**; "hoy" se calcula en la zona que pida el panel (`day_range_utc(tz)`).
Probado en Asia/Kolkata: el "hoy" local del 21/09 es `2026-09-20 18:30 → 2026-09-21 18:30 UTC`.

---

## 5. Atribución de cuentas — analítica, no hecho

Una cuenta se abre **por** un lead, no **por** una llamada. Relacionarla con una ruta o
proveedor de voz es una **convención de reporting**.

**Modelo `LAST_CONNECTED_CALL_V1`** (`analytics_v2.accounts_attributed`):
- la última llamada del **mismo lead** con resultado ANSWERED o CALLBACK,
- completada **antes o en el momento** de la apertura,
- dentro de los **30 días** previos.
- Si no existe → **`UNATTRIBUTED`**. No se inventa.

Se calcula **al consultar**, no se guarda: cambiar el modelo no exige reescribir datos.
Cada fila lleva `attribution_model` para que el panel pueda rotularlo.

Probado: lead con dos llamadas contestadas → se atribuye a la última previa; llamada
**posterior** a la apertura → ignorada; lead sin llamada contestada → UNATTRIBUTED; llamada
de hace 40 días → UNATTRIBUTED.

**PENDING_PRODUCT_DECISION:** modelo (última vs. primera vs. todas) y ventana (30 días).

---

## 6. Autoridad por dominio (source of truth)

No existe "MySQL es la fuente de verdad de todo". Cada dominio tiene su autoridad:

| Dominio | Autoridad | Copia local | Si difieren |
|---|---|---|---|
| **Configuración de routing** (países, rutas, proveedores, políticas, tools) | **MySQL local (panel)** | — | no aplica: es la única |
| **Ejecución de llamadas** (claim, despacho, estado) | **`wf_call_jobs`** | — | no aplica |
| **Resultado final de llamada** | evento post-call normalizado → **`wf_call_jobs`** | call-log del proveedor (`stringee_calls`) | issue; lo local no se corrige solo |
| **Estado comercial del lead** (status, attempts, owner) | **LeadStudio** | `crm_leads` (réplica para reporting) | manda LeadStudio |
| **Creación de cuenta** | respuesta real del **account provider** (CashStudio) → evento `ACCOUNT_CREATED` | LeadStudio / `crm_conversions` | issue `CRM_ACCOUNT_MISSING`; el evento local se conserva |
| **Links y pagos** | respuesta del **payment provider** (OkPay) → eventos | — | issue |
| **Grabación** | **adjunto en LeadStudio** | estado local (`RECORDING_*`) | issue |
| **Follow-up** | **LeadStudio** (followup creado) | `followup_id` en jobs/ledger | NEEDS_RECONCILIATION si el POST fue ambiguo |

---

## 7. Reconciliación (el nuevo papel de WF14)

WF14 conserva lo que hoy hace bien (LeadStudio → `crm_leads`, call-log → `stringee_calls`)
y agrega la comparación:

| Comparación | Issue si difiere |
|---|---|
| `ACCOUNT_CREATED` local vs `accountOpened` en LeadStudio | `CRM_ACCOUNT_MISSING` |
| llamada `COMPLETED` local vs estado del lead en CRM | `CRM_STATUS_MISMATCH` |
| jobs `NEEDS_RECONCILIATION` | `CALL_NEEDS_RECONCILIATION` |
| ledger `NEEDS_RECONCILIATION` | `FOLLOWUP_NEEDS_RECONCILIATION` |
| post-call sin job | `ORPHAN_POSTCALL` (lo abre el motor) |
| resultado contradictorio | `RESULT_CONFLICT` (lo abre `record_call_result`) |

Reglas: un issue por `issue_key` (re-detectar suma `occurrences`); **nunca se borra ni se
corrige un dato local porque el CRM falle o esté atrasado**. Probado.

---

## 8. Capa de consultas del dashboard

`app/analytics_v2.py` — el dashboard visual se construye sobre estas funciones:

| Vista | Función |
|---|---|
| **Overview "Today"**: calls, answered, answer rate, talk minutes, accounts, conversión | `overview(db, start, end)` |
| **By Country** | `call_metrics(..., group_by='country')` + `business_metrics(..., 'country')` |
| **By Provider** | `call_metrics(..., 'provider')` · cuentas: `accounts_by(..., 'provider')` |
| **By Route** | `call_metrics(..., 'route')` · cuentas: `accounts_by(..., 'route')` |
| **Timeseries** calls/h, answered/h, talk min/día, accounts/día | `timeseries(db, start, end, unit, metric)` |
| Atribución detallada | `accounts_attributed(db, start, end)` |

Endpoint ya disponible (solo master): `GET /callcenter/analytics.json?tz=Asia/Kolkata&days=1&group_by=route`.

**Ojo con `group_by='provider'` en eventos de negocio:** en un `ACCOUNT_CREATED`, `provider` es
`cashstudio`, no el proveedor de voz. Para cuentas por proveedor **de voz** se usa atribución.

---

## 9. Rendimiento

### Índices (todos creados por la migración, verificados por test)

| Consulta | Índice |
|---|---|
| **métricas de llamadas por rango** | **`idx_metrics (created_at, country_iso, route_key, provider, state, result, duration_seconds, dispatched_at)` — cubriente** |
| por país / ruta / proveedor | `(country_iso, created_at)`, `(route_key, created_at)`, `(provider, created_at)` |
| atribución, correlación WF9 | `(lead_id)`, `UNIQUE(inflight_lead)`, `(conversation_id)`, `(provider_job_id)` |
| **métricas de negocio por rango** | **`idx_ev_metrics (event_type, occurred_at, country_iso, route_key, provider, result)` — cubriente** |
| eventos por país / ruta / proveedor | `(country_iso, occurred_at)`, `(route_key, occurred_at)`, `(provider, occurred_at)` |
| trazas | `(lead_id)`, `(call_job_id)`, `(conversation_id)` |

### Medición real (no estimación)

Base sintética en MariaDB 10.11: **5,2 M llamadas en un año** (~14 200/día, 6 rutas) y
**1,35 M eventos en el último mes** (3 por llamada). Funciones reales de
`analytics_v2.py`. Entorno de construcción con `innodb_buffer_pool_size` = 128 MB (el de
producción probablemente sea mayor: los números reales deberían ser iguales o mejores).

| Consulta | Sin índice cubriente | **Con índice cubriente** |
|---|---|---|
| overview · hoy | 76 ms | **21 ms** |
| overview · 7 días | 578 ms | **144 ms** |
| overview · 30 días | 5 611 ms (escaneo completo) | **523 ms** |
| por ruta · 30 días | — | 974 ms |
| timeseries por hora · 30 días | — | 484 ms |
| overview · 90 días | minutos | 1 656 ms |
| por ruta · 90 días | minutos | **3 116 ms** |
| métricas de negocio · 7 días | 2 107 ms | **122 ms** |
| métricas de negocio · 30 días | 2 340 ms | **536 ms** |
| atribución de cuentas · 7 días | — | 90 ms |

Lo que mostró la medición: sin el índice cubriente, a partir de ~30 días MariaDB
**descarta el índice de fecha y recorre la tabla entera** (EXPLAIN: `key: NULL`, 5,1 M
filas), porque un índice no cubriente obliga a una lectura aleatoria por fila. Con el
cubriente el plan es `Using index`. Para `wf_events` además se filtra por los tipos de
negocio, que dejan fuera los eventos de llamadas (2/3 del volumen).

### Cuándo introducir rollups

**Primera versión: sin rollups.** Hoy, 7 y 30 días quedan por debajo de ~1 s.

Introducir `analytics_daily` (día × país × ruta × proveedor) cuando se cumpla **cualquiera**:
- el dashboard ofrece rangos de **90 días o más** de forma habitual (medido: 1,7–3,1 s),
- una consulta de hasta 30 días supera **1 s** sostenido en producción (slow log),
- `wf_events` supera ~**20 M filas**,
- vistas con auto-refresh de varios paneles cada pocos segundos.

Cuando se introduzcan se **recalculan** desde `wf_call_jobs`/`wf_events` (que siguen siendo
la verdad) con un job idempotente que reescribe los días cerrados. Nunca los escriben los
workflows en línea.

### Retención
Sin política todavía. `wf_events` es append-only; si crece, se particiona por mes
(`PARTITION BY RANGE (TO_DAYS(occurred_at))`) antes que borrar. **PENDING_PRODUCT_DECISION.**

---

## 10. Pendientes de decisión de producto

| # | Tema | Default actual |
|---|---|---|
| PD-1 | Modelo y ventana de atribución | LAST_CONNECTED_CALL_V1, 30 días |
| PD-2 | Definición de conversión | accounts / answered, mismo rango |
| PD-3 | ¿CALLBACK cuenta como contestada? | sí |
| PD-4 | Zona horaria de los reportes | parámetro `tz`; buckets de timeseries en UTC |
| PD-5 | Retención de eventos | indefinida |
