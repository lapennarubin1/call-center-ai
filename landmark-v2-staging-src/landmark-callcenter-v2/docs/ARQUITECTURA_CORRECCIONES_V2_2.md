# ARQUITECTURA_CORRECCIONES_V2_2.md
**Última corrección de la fundación antes de los templates n8n.**
21/09/2026 · Reemplaza a `ARQUITECTURA_CORRECCIONES_V2_1.md` donde se contradigan.

---

## 1. Resumen de V2.1 → V2.2

| # | Tema | V2.1 | V2.2 |
|---|---|---|---|
| 1 | Interruptores | proveedor + ruta | **país + proveedor + ruta**, independientes, con `blocked_by` explícito |
| 2 | Proveedor vs implementación | `provider_kind` ENUM(`sip`,`stringee`) | **`adapter_key` VARCHAR** + catálogo `ADAPTERS` en la app |
| 3 | PROVEEDOR1 | un proveedor, rutas IN y NP | un proveedor, rutas **IN, NP, MX, CO, VE…** (probado) |
| 4 | Errores técnicos | `REJECTED` **consumía** el intento (D-4) | `TECHNICAL_ERROR` → **`RELEASED`**, no consume, backoff 1…60 min |
| 5 | Intento | `attempt` único | **`attempt` (negocio) + `tech_retry_count` (técnico)** |
| 6 | Doble llamada | `UNIQUE(lead_id, attempt)` | + **`UNIQUE(inflight_lead)`**: una llamada en vuelo por lead, impuesto por la base |
| 7 | Origen de `attempt` (R-4) | diseñado, no implementado | **`next_attempt()`** implementado y probado |
| 8 | Readiness | una sola validación | **ROUTE READY · COUNTRY READY · TOOLS READY** por separado |
| 9 | Tools | bloqueaban la **ruta** | bloquean el **país** (son del país) |
| 10 | Analytics | dependía de WF14 / LeadStudio | **event store local** + hecho por llamada + capa de consultas SQL |
| 11 | Resultado de llamada | solo en el ledger | **hecho en `wf_call_jobs`** (primer escritor gana) + evento `CALL_RESULT` |
| 12 | WF14 | fuente de analytics | **reconciliación** (`wf_reconciliation_issues`) |
| 13 | Correlación WF9 | `route_key` en dynamic_variables (bloqueante R-1) | `call_job_id` **o** la única llamada en vuelo del `lead_id` |
| 14 | Timestamps | `NOW()` | **`UTC_TIMESTAMP()`** en todo lo que escribe V2 |

---

## 2. Tres interruptores

```
countries.enabled         PAÍS       India OFF → IN_PROVEEDOR1 e IN_STRINGEE no llaman
voice_providers.enabled   PROVEEDOR  PROVEEDOR1 OFF → sus rutas en IN/NP/MX/CO/VE no llaman
call_routes.enabled       RUTA       IN_PROVEEDOR1 OFF → IN_STRINGEE sigue

calling_now = país ON ∧ proveedor ON ∧ ruta ON ∧ no archivados
              ∧ ROUTE READY ∧ COUNTRY READY ∧ horario
```

La API devuelve **por qué** una ruta no llama, con nombres estables:
`COUNTRY_DISABLED`, `PROVIDER_DISABLED`, `ROUTE_DISABLED`, `COUNTRY_ARCHIVED`,
`ROUTE_ARCHIVED`, `ROUTE_NOT_READY`, `COUNTRY_NOT_READY`, `OUTSIDE_SCHEDULE`,
`ZERO_CAPACITY`, `INVALID_TIMEZONE`. Si hay varios, aparecen todos.

**READY ≠ ON.** READY es configuración completa; ON es decisión operativa. Apagar India no
toca la configuración de sus rutas: siguen READY y vuelven a llamar al encenderla.

### Decisiones de activación (definidas)

| Pregunta | Respuesta |
|---|---|
| ¿Un país nuevo nace encendido? | **No.** `countries.enabled` DEFAULT 0 (fail-closed) |
| ¿Se puede encender un país sin rutas READY? | **Sí (staging).** No llama; el panel lo marca "ON sin rutas READY" |
| ¿Qué exige encender un país? | ISO, prefijo, huso, idioma válidos + **todas sus tools enabled completas** |
| ¿Se puede activar una ruta con el país apagado? | **Sí.** Queda READY+ON pero `blocked_by: COUNTRY_DISABLED`. Permite dejar todo listo y encender el país al final |
| ¿Qué exige activar una ruta? | configuración de la ruta y requisitos de su **adapter** (no los interruptores) |
| ¿Upgrade desde V2.1? | países existentes quedan **ON** (ya llamaban); los nuevos, OFF |

Probado en el flujo HTTP de México: ruta completa y activada → no llama
(`COUNTRY_DISABLED`) → se enciende el país → llama.

---

## 3. Proveedor comercial vs adapter técnico

```
voice_providers.code        proveedor1 · stringee · proveedor2…   (contrato, cuenta, interruptor)
voice_providers.adapter_key ELEVENLABS_SIP · STRINGEE_WORKER      (cómo se le habla)
```

- `adapter_key` es **VARCHAR**. El catálogo `routes_config.ADAPTERS` declara, por adapter,
  qué exige a una ruta (`route_requires`) y si el proveedor necesita endpoint.
- La validación de rutas es **genérica**: recorre `route_requires` del adapter. No hay
  `if sip` ni `if stringee` en el código.
- Un adapter fuera del catálogo se puede **guardar apagado** pero **no activar**, ni el
  proveedor ni sus rutas (`UNKNOWN_ADAPTER`).
- **PROVEEDOR2 SIP = cero código**: se da de alta con `ELEVENLABS_SIP` y hereda sus
  requisitos. Probado.
- Upgrade: `sip → ELEVENLABS_SIP`, `stringee → STRINGEE_WORKER`. `provider_kind` queda
  NULL-able (la migración no borra columnas).

---

## 4. Técnico vs negocio

| | Intento de negocio | Reintento técnico |
|---|---|---|
| columna | `attempt` | `tech_retry_count`, `next_tech_retry_at` |
| lo consume | una llamada que **pudo salir** | nada |
| lo ve | la política (1 → +2h … 9 → CLOSE) | WF2 y el panel |
| causas | NO_ANSWER, ANSWERED, SIP 603… | 401/403, credencial, config, worker caído, PATCH ATTEMPTING fallido |
| estado del job | `COMPLETED` | **`RELEASED`** |
| siguiente | `scheduleNextAt` | backoff **1, 2, 4, 8, 16, 32, 60, 60…** min |

**Sin bucles:** el backoff evita reintentos en caliente, y AUTH/CONFIG cortan el proveedor
en ese ciclo. El intento liberado se **retoma con el mismo número**, desde cualquier ruta
(probado: RELEASED en `IN_PROVEEDOR1` → re-claim por `IN_STRINGEE`, attempt 1,
tech_retry_count 1).

**UNKNOWN nunca es técnico:** si pudo salir, `NEEDS_RECONCILIATION` y jamás re-dispatch.

Esto **revierte mi decisión D-4 de V2.1** ("REJECTED consume el intento"). El brief tiene
razón: un 401 no es el cliente sin contestar.

---

## 5. Una llamada en vuelo por lead

Columna generada `inflight_lead` = `lead_id` mientras el job está en CLAIMED, DISPATCHING,
DISPATCHED, UNKNOWN o NEEDS_RECONCILIATION; **UNIQUE**. Consecuencias:

1. **Nunca dos llamadas simultáneas a la misma persona**, en ningún intento y desde
   ninguna ruta. Lo garantiza la base (error 1062), no el código. Probado con 10 intentos
   simultáneos del mismo lead: gana 1.
2. **Correlación de WF9 sin depender del proveedor**: `lead_id` → la única llamada en vuelo
   → `call_job_id`, `route_key`, `attempt`, `provider`. Esto **baja el riesgo R-1 de
   bloqueante a verificable**: ya no hace falta que el worker Stringee propague
   `route_key`/`call_job_id`; alcanza con `lead_id`.
3. Un lead en `NEEDS_RECONCILIATION` **no se vuelve a llamar** hasta que alguien decide.

### Refinamiento de un test del brief

El brief pide "mismo lead, distinto intento → válido". Con esta garantía es válido **en
secuencia** (el intento 2 cuando el 1 terminó) pero **no en simultáneo**: dos intentos en
vuelo a la vez serían una doble llamada al cliente. Ambos casos están probados por
separado. Si la intención era permitir la simultaneidad, avisame: es un cambio de una línea
en el esquema, pero lo considero un error.

---

## 6. Analytics local

Detalle completo en `ANALYTICS_ARCHITECTURE_V2_2.md`. Lo esencial:

- **2 tablas nuevas** (`wf_events`, `wf_reconciliation_issues`) y **1 ampliada**
  (`wf_call_jobs`). No se crearon `wf_call_events` + `wf_business_events` por separado.
- **Llamadas se miden del hecho** (`wf_call_jobs`, resultado y duración escritos una vez);
  **negocio, de los eventos**. Un evento duplicado no puede duplicar minutos.
- **Un solo `CALL_RESULT` por llamada** en vez de un tipo por resultado.
- `event_key` **determinista** + UNIQUE: idempotencia por construcción.
- WF9 escribe el resultado **antes** del CRM: el panel lo ve en segundos.
- El dashboard **nunca abre conexiones de red** (probado bloqueando `socket.connect`).
- **Rendimiento medido** sobre 5,2 M llamadas: hoy 21 ms, 7 días 144 ms, 30 días ~0,5 s
  con índices cubrientes. Sin rollups en la primera versión.

---

## 7. Errores míos encontrados durante V2.2

| Qué | Cómo apareció | Corrección |
|---|---|---|
| **Afirmé un rendimiento sin medirlo** ("milisegundos a cientos de ms") | lo medí antes de entregarlo: 30 días tardaba **5,6 s** y 90 días, minutos (escaneo completo) | índices cubrientes `idx_metrics` e `idx_ev_metrics`, filtro por tipo de evento. Documento con números medidos |
| `open_issue` detectaba "recién creado" con estado en memoria del objeto DB | revisión propia antes de probar: con otra conexión, un issue existente no sumaba ocurrencias | reescrito sin estado; unicidad garantizada por UNIQUE |
| Consulta de diagnóstico de Stringee con columnas inventadas (`answer_duration`) | la contrasté con el SQL real de WF14: son `answered` y `duration_secs`, y hay `lead_id` | corregida y probada en MariaDB |
| Utilidad de parcheo borraba variables en minúscula entre funciones | el backend MariaDB falló con `NameError: _counter` | patrón corregido; verificado por AST que ningún módulo perdió constantes |
| `find_inflight_job` devolvía `{}` en vez de `None` | test de correlación | devuelve `None` explícito |

---

## 8. Decisiones abiertas

| # | Decisión | Estado |
|---|---|---|
| D-1 | `FAILED`/`UNKNOWN` sin regla → `NONE` | abierta (sin cambios) |
| D-2 | tabla `countries` con huso por país | **confirmada** por el brief (country switch) |
| D-3 | grabaciones por ruta | abierta |
| D-4 | `REJECTED` consume intento | **revertida**: técnico no consume |
| D-6 | market India `IND` vs `ATL_IND` | abierta |
| D-7 | `CALLBACK` disabled | abierta |
| D-8 | `provider_kind` ENUM | **reemplazada** por `adapter_key` VARCHAR |
| D-10 | lead en `NEEDS_RECONCILIATION` bloqueado para nuevas llamadas | nueva — conservadora |
| D-11 | país apagado ⇒ WF3/WF7 no ejecutan tools | nueva |
| D-12 | CALLBACK cuenta como contestada en analytics | nueva (PD-3) |
| PD-1/2/4/5 | atribución, conversión, zona horaria de reportes, retención | PENDING_PRODUCT_DECISION |
