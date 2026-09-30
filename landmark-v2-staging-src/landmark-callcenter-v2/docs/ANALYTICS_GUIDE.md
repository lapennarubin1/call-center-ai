# ANALYTICS_GUIDE.md
**De dónde sale cada número del panel, y por qué no sale del CRM.**
Fase BUILD · 21/09/2026

---

## 0. El cambio

| | v1 | V2 |
|---|---|---|
| fuente | LeadStudio, consultado en cada carga | **MySQL local** |
| quién la llena | WF14, cada 5 min | cada workflow, **en el momento** |
| si el CRM se cae | el panel no muestra nada | el panel sigue andando |
| si WF14 no corre | los números se congelan | los números siguen bien |
| rol de WF14 | **era** la fuente | ahora **reconcilia** |

---

## 1. Dos capas

```
wf_call_jobs    UNA fila por llamada.
                Resultado y duración viven acá, UNA sola vez.
                → intentos, tasa de respuesta, minutos, duración media

wf_events       LOG append-only e idempotente de todo lo demás.
                → cuentas, pagos, tools, follow-ups, grabaciones, trazabilidad
```

**Por qué separadas:** si los minutos hablados salieran de un log de eventos, un
evento duplicado los contaría dos veces. Saliendo de la fila de la llamada, es
imposible: la fila es una.

---

## 2. Idempotencia por construcción

`wf_events.event_key` es **UNIQUE** y **determinista**:

```
CALL_RESULT:{call_job_id}
FOLLOWUP_CREATED:{call_job_id}
ACCOUNT_CREATED:{account_provider}:{lead_id}
PAYMENT_LINK_CREATED:{payment_provider}:{order_ref}
RECORDING_ATTACHED:{call_job_id}
```

El mismo hecho escrito dos veces —webhook y polling, reintento de un nodo,
re-ejecución de n8n— **choca con el UNIQUE y se ignora**.

> **Nunca un UUID aleatorio como `event_key`.** Eso anula la idempotencia: es la
> diferencia entre "el pago se contó una vez" y "el pago se contó cada vez que el
> proveedor reenvió su callback".

**Un solo `CALL_RESULT` por llamada**, no un tipo por resultado. Si existieran
`CALL_ANSWERED` y `CALL_NO_ANSWER` separados, un webhook que dice una cosa y un
polling que dice otra quedarían **los dos** guardados. Con una clave por llamada,
el segundo choca y —si contradice al primero— abre un issue `RESULT_CONFLICT`.

---

## 3. Qué escribe cada workflow, y cuándo

| workflow | eventos | momento |
|---|---|---|
| **WF2** | `CALL_CLAIMED`, `CALL_DISPATCHED`, `CALL_TECH_FAILED`, `CALL_UNKNOWN`, `CALL_RESULT` (solo FINAL) | en cada transición |
| **WF9** | el hecho en `wf_call_jobs` + `CALL_RESULT` | **al recibir el post-call, ANTES del CRM** |
| **Motor** | `FOLLOWUP_CREATED`, `CALLBACK_SCHEDULED`, `LEAD_CLOSED` | tras el `POST /followups` |
| **WF3** | `ACCOUNT_REQUESTED`, `ACCOUNT_CREATED` / `_ALREADY_EXISTS` / `_FAILED` | con la respuesta del proveedor |
| **WF7/8** | `PAYMENT_LINK_*`, `PAYMENT_CONFIRMED` | con la respuesta del proveedor |
| **WF10** | `RECORDING_ATTACHED` / `_SKIPPED_SHORT` / `_MISSING` | al procesar la grabación |
| **WF14** | **ninguno** — reconcilia | |

El orden importa: **primero local, después el CRM**. Si LeadStudio está caído, el
panel muestra el resultado igual.

---

## 4. Las definiciones (son contrato)

Si alguna cambia, cambia el número del panel. Están en
`panel/app/analytics_v2.py`.

| métrica | definición |
|---|---|
| **attempted** | `state ∈ DISPATCHING, DISPATCHED, UNKNOWN, COMPLETED, NEEDS_RECONCILIATION`. **Excluye** `RELEASED` (técnico: no salió), `CLAIMED` (todavía no) y `FAILED` (dato inválido) |
| **dispatched** | `dispatched_at` no nulo |
| **with_result** | tiene resultado final registrado |
| **answered** | `result ∈ ANSWERED, CALLBACK` — pedir un callback implica haber hablado |
| **no_answer / busy / voicemail / wrong_number / dnc** | por `result` |
| **answer_rate** | `answered / with_result` |
| **talk_seconds** | `SUM(duration_seconds)` de las contestadas |
| **avg_talk_seconds** | `talk_seconds / contestadas con duración` |
| **failed_technical** | eventos `CALL_TECH_FAILED` — **no consumen intento de negocio** |
| **needs_reconciliation** | `state ∈ UNKNOWN, NEEDS_RECONCILIATION` |
| **in_flight** | `state ∈ CLAIMED, DISPATCHING, DISPATCHED` |
| **accounts_opened** | eventos `ACCOUNT_CREATED` |
| **conversion_rate** | `accounts_opened / answered` del mismo rango |

### Dos decisiones que conviene entender

**`answer_rate` se calcula sobre `with_result`, no sobre `attempted`.**
Una llamada despachada hace 30 segundos, todavía sin resultado, no debe bajar la
tasa. Si el denominador fuera `attempted`, la tasa parecería peor cuanto más
rápido despacha el sistema.

**`failed_technical` está fuera de `attempted`.**
Un 401 no es una llamada. Mezclarlo hundiría la tasa de respuesta cada vez que el
proveedor tiene un problema — que es justo cuando hay que mirar el número.

---

## 5. Atribución de cuentas

**`accounts_opened` por país/proveedor/ruta usa un modelo de atribución
declarado**, porque una cuenta no tiene ruta: la tiene la llamada que la generó.

```
LAST_CONNECTED_CALL_V1
  la última llamada del MISMO lead con resultado ANSWERED o CALLBACK,
  completada ANTES o EN el momento de la apertura,
  dentro de los 30 días previos.
```

Si no existe esa llamada, la cuenta queda **`UNATTRIBUTED`**. No se inventa una
ruta.

> Es una **convención de reporting**, no un hecho. Está marcada como
> `PENDING_PRODUCT_DECISION`: el modelo (última llamada vs primera vs cohortes) y
> la ventana (30 días) son decisiones de negocio. El modelo viaja en la respuesta
> para que quien lea el número sepa de dónde sale.

Lo mismo con `conversion_rate`: `accounts_opened / answered` del mismo rango
mezcla cuentas de llamadas de días anteriores. La alternativa —cohortes por fecha
de llamada— responde otra pregunta. Ninguna es "la correcta": hay que elegir.

---

## 6. La pantalla

`Panel → Call Center → Analytics` (solo rol master)

```
?days=7&tz=Asia/Kolkata
```

| bloque | qué muestra |
|---|---|
| **salud operativa** | llamadas a reconciliar, issues abiertos, grabaciones huérfanas — solo aparece si hay algo |
| **indicadores** | intentadas · contestadas + tasa · no contestadas · minutos + promedio · cuentas + conversión · fallos técnicos |
| **negocio** | follow-ups, callbacks, cierres, cuentas, pagos, grabaciones |
| **desgloses** | por país · por proveedor · por ruta, con las mismas columnas |
| **serie temporal** | por hora (1–2 días) o por día (más) |

Export: `/callcenter/analytics.csv?days=7&group_by=route`

---

## 7. Consultas directas

```sql
-- el día de hoy, por ruta
SELECT route_key, provider,
       SUM(state IN ('DISPATCHING','DISPATCHED','UNKNOWN','COMPLETED',
                     'NEEDS_RECONCILIATION'))                     AS attempted,
       SUM(result IS NOT NULL)                                    AS with_result,
       SUM(result IN ('ANSWERED','CALLBACK'))                     AS answered,
       ROUND(SUM(result IN ('ANSWERED','CALLBACK')) /
             NULLIF(SUM(result IS NOT NULL), 0) * 100, 1)         AS answer_rate,
       ROUND(SUM(CASE WHEN result IN ('ANSWERED','CALLBACK')
                      THEN COALESCE(duration_seconds,0) END)/60, 1) AS talk_minutes
  FROM wf_call_jobs
 WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)
 GROUP BY route_key, provider;

-- errores técnicos por ruta (NO consumen intento)
SELECT route_key, error_code, COUNT(*) FROM wf_call_jobs
 WHERE state='RELEASED' AND updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)
 GROUP BY route_key, error_code ORDER BY 3 DESC;

-- embudo de negocio
SELECT event_type, COUNT(*) FROM wf_events
 WHERE occurred_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
 GROUP BY event_type ORDER BY 2 DESC;

-- la vida completa de un lead
SELECT 'job' AS src, created_at AS ts, call_job_id, attempt, state, result, NULL AS event
  FROM wf_call_jobs WHERE lead_id = '<lead>'
UNION ALL
SELECT 'event', occurred_at, call_job_id, attempt, NULL, result, event_type
  FROM wf_events WHERE lead_id = '<lead>'
 ORDER BY ts;
```

---

## 8. Rendimiento

Dos índices cubrientes, del diseño de la fundación:

```
wf_call_jobs.idx_metrics
  (created_at, country_iso, route_key, provider, state, result,
   duration_seconds, dispatched_at)
  → medido: 30 días sobre 5,2 M filas: 5,6 s → 0,5 s

wf_events.idx_ev_metrics
  (event_type, occurred_at, country_iso, route_key, provider, result)
  → medido: 30 días, 1,35 M eventos: 2,3 s → 0,5 s
```

`business_metrics()` filtra por los tipos que realmente usa, así que los eventos
de llamada (2/3 del volumen) ni se leen.

---

## 9. WF14: qué hace ahora

**No alimenta el dashboard.** Hace cuatro cosas:

1. **reconcilia estados locales** — lo que quedó a medias (§10)
2. **abre issues** — con `issue_key` determinista: re-detectar el mismo desajuste
   suma `occurrences`, no crea filas
3. **compara local ↔ CRM** — cuenta local sin reflejo en el CRM
   (`CRM_ACCOUNT_MISSING`), estado divergente (`CRM_STATUS_MISMATCH`)
4. **conserva la sincronización legacy** — `crm_leads`, `stringee_calls`, para
   las pantallas viejas y para poder comparar

**Nunca borra ni corrige un dato local.** Una diferencia abre un issue; la
decisión es humana.

---

## 10. Reconciliación

`Panel → Call Center → Reconciliation`

| tipo | qué pasó |
|---|---|
| `CALL_NEEDS_RECONCILIATION` | la llamada pudo salir y no sabemos el resultado |
| `FOLLOWUP_NEEDS_RECONCILIATION` | el `POST /followups` pudo ejecutarse; no hay `followup_id` |
| `RESULT_CONFLICT` | dos fuentes dan resultados distintos para la misma llamada |
| `ORPHAN_POSTCALL` | llegó un resultado de una llamada que V2 no registró |
| `CRM_ACCOUNT_MISSING` | hay `ACCOUNT_CREATED` local y el CRM no lo refleja |
| `CRM_STATUS_MISMATCH` | el CRM dice `ATTEMPTING` y localmente ya terminó |
| `TECH_RETRY_REPEATED` | una ruta acumula reintentos técnicos: es el proveedor |

**Resolver** = lo revisé y está corregido. **Ignorar** = no es un problema real.
Ninguna de las dos toca los datos locales.

---

## 11. Lo que el panel NO hace

- ❌ llamar a LeadStudio para un KPI, un gráfico o un refresh
- ❌ recalcular métricas a partir del CRM
- ❌ corregir datos locales para que cuadren con el CRM
- ❌ borrar o editar eventos

Hay un test (`test_panel_v2.py`) que **bloquea el socket** y exige que Analytics
y Reconciliation sigan funcionando. Si alguien agrega una llamada de red a una
vista, ese test falla.

---

## Filtros y rangos (r2-final)

### Un solo recorte alimenta todo

La pantalla construye **un** objeto de recorte —rango, zona horaria, país,
proveedor, ruta— y ese mismo objeto alimenta:

- las tarjetas de KPI
- los desgloses por país, proveedor y ruta
- las series temporales
- el **Breakdown by Hour**
- las métricas de negocio y las cuentas
- **todos** los CSV

No puede darse que la pantalla diga «India» y el KPI muestre el total
global, ni que diga «Stringee» y el CSV traiga Provider1. Antes sí podía:
`overview()` se calculaba sin filtros, así que las tarjetas mostraban el
total global con un filtro puesto. Es el peor tipo de error de un panel —
no falla, miente con números que parecen correctos.

### Rango

Tres formas, por orden de precedencia:

1. **From / To exactos** — `?start=2026-09-01&end=2026-09-10`, inclusivo
   por los dos extremos, en la zona horaria elegida
2. **Preset** — `?period=today|yesterday|week|month|7d|30d|90d|year`
3. **Días** — `?days=7`, compatibilidad con la versión anterior

Los límites del día se calculan en la **zona horaria pedida**, no en UTC:
«hoy» en `Asia/Kolkata` no empieza a la misma hora que «hoy» en UTC.

**Límite: 366 días.** No es arbitrario. Todas las consultas son agregados
con `GROUP BY` sobre columnas indexadas (`created_at`, `occurred_at`), así
que el coste crece con el número de filas, no de días; un año de un call
center que hace unos miles de llamadas al día son cientos de miles de
filas, que MySQL agrega sin problema. El tope existe para que una URL
escrita a mano (`?days=100000`) no monte una consulta de años. Pedir más
no falla: se recorta a 366.

### Breakdown by Hour

Con un rango corto (≤ 2 días) los buckets son **horarios**; con rangos
mayores, **diarios**. Respeta exactamente los filtros activos, y se puede
descargar: `?group_by=hour`.

El desglose y el total del KPI **siempre suman lo mismo**. Hay un test que
lo comprueba para cinco combinaciones de filtros: si no cuadraran, uno de
los dos estaría mintiendo.

### CSV

Los enlaces de descarga se construyen serializando el recorte activo, así
que es imposible generar una URL que pierda un filtro. El fichero sale
limpio —cabecera en la primera línea, para que Excel y pandas lo abran
bien— y el contexto viaja en cabeceras HTTP:

```
X-Landmark-Filters:   Countries: IN · Providers: stringee
X-Landmark-Range-Utc: 2026-09-01T00:00:00Z..2026-09-11T00:00:00Z
X-Landmark-Source:    LOCAL_MYSQL
```

Un export filtrado lleva `-filtered` en el nombre del fichero, para que no
se confunda con uno completo del mismo día.

### El dashboard de siempre no se tocó

Todo lo anterior es de **Call Center → Analytics** (V2). El dashboard
original, su Breakdown by Hour y sus tres exports siguen exactamente como
estaban. Una nota sobre su filtro de proveedor: cambia el embudo y las
cuentas, pero no el gráfico Call Traffic ni el export diario, porque ésos
leen `cdr_panel`, que es tráfico de Asterisk y no distingue proveedor. Es
la semántica que ya tenía y no se cambió.
