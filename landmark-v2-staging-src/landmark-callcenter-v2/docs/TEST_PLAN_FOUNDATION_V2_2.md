# TEST_PLAN_FOUNDATION_V2_2.md

```bash
cd landmark-panel-multicountry-v2.2
./tests/run_all.sh
```

Las suites de MariaDB se marcan **SKIP** (no FAIL) si no hay servidor. Variables:
`LM_TEST_MYSQL_USER`, `_PASS`, `_HOST`, `_SOCKET`. **Nunca contra producción**: las suites
**crean y borran** bases (`mig_*`, `fnd_*`, `ev_*`, `conc_v22`).

## Resultado de esta entrega

| Suite | Motor | PASS | FAIL | SKIP |
|---|---|---|---|---|
| `test_foundation_v2_2.py` | sqlite **y** MariaDB (56 × 2) | **112** | 0 | 0 |
| `test_ops_analytics_v2_2.py` | sqlite **y** MariaDB (21 × 2) | **42** | 0 | 0 |
| `test_http_v2_2.py` | Flask + sqlite | **15** | 0 | 0 |
| `test_migration_v2_2.py` | MariaDB 10.11 | **13** | 0 | 0 |
| `test_concurrency_v2_2.py` | MariaDB, 40 hilos | **10** | 0 | 0 |
| **Total** | | **192** | **0** | **0** |

Sin MariaDB: 56 + 21 + 15 = 92 PASS, 0 FAIL, 4 SKIP (verificado: los backends y suites de MariaDB se saltan, no fallan).

El backend MariaDB de las suites de fundación y analytics aplica **el `.sql` real** de la
migración y prueba el código encima: esquema y código no pueden divergir sin que falle.

## Cobertura del punto 24 del brief

| Requisito | Caso | Estado |
|---|---|---|
| PROVEEDOR1 con IN, NP, MX, CO, VE: mismo provider_id, distintas route_key | `PROVEEDOR1: UN proveedor, rutas IN/NP/MX/CO/VE` | ✅ |
| COUNTRY OFF: India OFF → IN_PROVEEDOR1 e IN_STRINGEE no llaman; NP/MX siguen | `país OFF` (unit) + `switches por UI` (HTTP) | ✅ |
| ROUTE OFF: IN_PROVEEDOR1 OFF, IN_STRINGEE sigue | `ruta OFF` | ✅ |
| PROVIDER OFF: PROVEEDOR1 OFF → todas sus rutas; Stringee sigue | `proveedor OFF` (5 países) + HTTP | ✅ |
| mismo CALL_ANSWERED ×2 → una métrica | `mismo CALL_ANSWERED ×2` + 40 hilos | ✅ |
| la duración no se duplica | `la duración no se duplica ni se pisa` + 40 hilos | ✅ |
| ACCOUNT_CREATED duplicado → una cuenta | `ACCOUNT_CREATED ×3` + 40 hilos | ✅ |

## Mediciones de rendimiento (no son tests automáticos)

Base sintética: 5,2 M llamadas/año + 1,35 M eventos en un mes. Resultados en
`ANALYTICS_ARCHITECTURE_V2_2.md §9`. Se hizo una vez para fundamentar los índices; no
forma parte de `run_all.sh` porque la carga tarda ~2,5 min.

## Casos por suite

### `test_foundation_v2_2.py` — Fundación: config, switches, adapters, franjas, política, claims
*sqlite + MariaDB (56 casos × 2)*

- el seed reproduce la config v1 y las rutas activas están READY
- IN_PROVEEDOR1 (5) + IN_STRINGEE (3) activas a la vez = 8, límites propios
- dos rutas IN + proveedor1 pueden coexistir (sin UNIQUE iso/proveedor)
- ruta nueva nace DISABLED y lista lo que falta
- SIP sin agent_id → no activa
- SIP sin phone_number_id → no activa
- Stringee sin agent_id → no activa
- Stringee sin caller_id → no activa
- ruta completa → se activa
- timezone inválida → rechazo (y fail-closed si llega corrupta)
- capacidad inválida → rechazo
- tool opcional disabled e incompleta → la ruta se activa igual
- payment enabled e incompleta → país NOT READY: no enciende y sus rutas no llaman
- editar una ruta ACTIVA no puede dejarla incompleta
- fail-closed en runtime: activa que queda inválida no llama
- proveedor deshabilitado → sus rutas no llaman
- deshabilitar una ruta no afecta a la otra del mismo país
- archivada: fuera de active/all, resoluble por clave, solo lectura, clave no reutilizable
- no existe borrado físico de rutas
- A · Mon 20-02 + Tue 01-03 → REJECT
- B · Mon 20-02 + Tue 02-03 → ACCEPT
- C · Mon 20-02 + Mon 21-23 → REJECT
- D · Mon 20-02 + Tue 20-02 → ACCEPT
- Sun 22-02 da la vuelta a Mon: Mon 01-03 REJECT, Mon 02-03 ACCEPT
- editar una franja no choca consigo misma
- resolución cross-midnight en México
- DST Madrid: misma hora local en invierno y verano
- India 5/8/5 y cambio en vivo 8→3
- sin franjas = capacity_default 24/7 (comportamiento actual)
- intentos 1-9: 2h/3h/2bd/2h/3h/3bd/2h/3h/CLOSE; 10+ CLOSE
- resultados: aliases, COMPLETE, CLOSE, CALLBACK, SIP 603 vía política, DISPATCHED rechazado
- Stringee usa la misma política: decisiones idénticas 1-9
- políticas malformadas rechazadas
- política compartida por 3 rutas; no se archiva si está en uso
- días hábiles calculados en el huso del país
- CONFIG_ROUTER / CUSTOM_ENDPOINT: requisitos y payload solo con enabled
- secretos en config_json o credential_ref → rechazados
- auditoría con actor y valores; no-cambios no se registran
- payload con todos los campos y sin secretos
- PROVEEDOR1: UN proveedor, rutas IN/NP/MX/CO/VE, todas llamando
- país OFF: India no llama por ningún proveedor; NP/MX/CO/VE siguen
- ruta OFF: IN_PROVEEDOR1 apagada, IN_STRINGEE sigue
- proveedor OFF: PROVEEDOR1 apagado en los 5 países; Stringee sigue
- los tres bloqueos se informan juntos y se levantan juntos
- adapter desconocido: se guarda apagado, no se activa
- PROVEEDOR2 reutiliza ELEVENLABS_SIP con sus mismos requisitos
- país en staging: ON con 0 rutas READY, no llama
- país nuevo nace OFF
- mismo lead + mismo intento → gana uno
- leads distintos: ambos · mismo lead intento 2: SKIP en vuelo, OK en secuencia
- máquina de estados: no se re-despacha
- crash tras DISPATCHING → NEEDS_RECONCILIATION, sin re-llamada
- error técnico 401: RELEASED, no consume intento, backoff, re-claim del MISMO intento
- error permanente: FAILED terminal, no se re-reclama
- ledger: webhook gana, polling pierde
- ledger: CLAIMED viejo → NEEDS_RECONCILIATION, sin retry ciego

### `test_ops_analytics_v2_2.py` — Eventos, analytics, atribución, técnico vs negocio
*sqlite + MariaDB (21 casos × 2)*

- mismo CALL_ANSWERED ×2 → una métrica, un evento
- la duración no se duplica ni se pisa
- resultado contradictorio → primero gana + issue RESULT_CONFLICT
- ACCOUNT_CREATED ×3 → una cuenta
- links de pago distintos cuentan; el repetido no
- clave de evento incompleta o tipo desconocido → VALIDATION_ERROR
- post-call tardío resuelve NEEDS_RECONCILIATION
- post-call sin job → issue ORPHAN_POSTCALL
- 401 no consume intento ni cuenta como llamada; sí como failed_technical
- backoff 1,2,4…60 min
- 3 fallos técnicos: tech_retry_count 1→3, attempt fijo, una sola fila
- next_attempt con CRM desfasado (R-4)
- intento 2 bloqueado con el 1 en vuelo; lead_id → call_job_id
- globales: attempted, answered, answer_rate, minutos
- por país, proveedor y ruta
- timeseries por hora y por día
- overview vacío: sin divisiones por cero
- overview con cuentas y conversión
- rango de "hoy" en Asia/Kolkata
- última llamada conectada previa; posterior ignorada; sin evidencia → UNATTRIBUTED
- CRM no refleja la cuenta → issue único, occurrences, dato local intacto

### `test_http_v2_2.py` — HTTP: API, switches, roles, UI, analytics sin red
*Flask + sqlite*

- sin LM_ROUTES_API_TOKEN → 503 CONFIG_ERROR
- token ausente o incorrecto → 401 AUTH_ERROR
- HTTP plano desde IP pública → 403 insecure transport
- IP pública con HTTPS (X-Forwarded-Proto) → 200
- red interna Docker/loopback → 200
- by-key resuelve archivadas; active/all las excluyen; include_archived las incluye
- tools por país: solo enabled; 404 país desconocido
- ninguna respuesta contiene secretos de los workflows v1
- sin sesión → login en todas las pantallas
- viewer y support: 4 pantallas y 18 mutaciones bloqueadas, nada cambia
- master: listado (4 filtros) y página de país con todas las secciones
- alta de México por UI: NOT READY → READY+ON sin llamar → país ON → llama
- switches por UI: país OFF/ON, proveedor OFF/ON, auditados
- pantallas existentes siguen funcionando
- /callcenter/analytics.json: métricas locales, CERO conexiones de red, solo master

### `test_migration_v2_2.py` — Migración
*MariaDB 10.11*

- seed = config de los JSON v1; UN proveedor SIP; país nace OFF por default
- UNIQUEs, índices de analytics, adapter_key VARCHAR, inflight generada
- la BASE rechaza la 2ª llamada en vuelo del mismo lead (1062)
- run #2 y #3 son NO-OP funcional
- no pisa ediciones: capacity, notas, Telegram, switches de país/proveedor, adapter
- instalación limpia cortada en 8 puntos + reanudación = idéntica
- upgrade con datos ×2: switches, adapter_key, RELEASED, inflight, sin re-seed
- upgrade cortado en 8 puntos + reanudación = idéntico (config, jobs y esquema)
- base actualizada ≡ instalación limpia (columnas, tipos, índices)
- 2 llamadas en vuelo → aborto explícito; decisión humana; reanuda OK
- sin DROP / TRUNCATE / DELETE ejecutables
- 6 tablas productivas: DDL y datos idénticos tras 2 ejecuciones
- cliente mariadb < archivo.sql, dos veces

### `test_concurrency_v2_2.py` — Concurrencia real
*MariaDB, 40 hilos*

- mismo lead + mismo intento, 2 rutas del mismo país → 1 ganador de 40
- 40 leads distintos → 40 ganadores
- mismo lead, intentos 1..10 simultáneos → 1 ganador (una llamada en vuelo)
- 10 leads × 4 competidores → exactamente 10 ganadores
- mark_dispatching concurrente → 1 solo despacho de 40
- re-claim de intento RELEASED (401) tras backoff → 1 ganador de 40, attempt fijo
- webhook vs polling misma conversación → 1 ganador de 40
- complete concurrente → 1 solo followup_id registrado
- ACCOUNT_CREATED ×40 simultáneos → 1 fila
- resultado ×40 simultáneos → 1 RECORDED, 1 evento, 1 duración

## Pruebas de que los tests no son decorativos

- **Mutación de la migración**: se quitó a propósito el paso que deja `adapter_key NOT NULL`
  en el upgrade → `base actualizada ≡ instalación limpia` **falló** señalando la columna.
- **Cortes en 8 puntos** de la instalación limpia **y** del upgrade, incluidos el backfill
  de `adapter_key` y el agregado de columnas de `wf_call_jobs`.
- La comparación de esquemas recorre 175 columnas y 55 índices reales.
- La prueba "analytics sin red" reemplaza `socket.connect` durante la request.

## Lo que ningún test local puede demostrar

| Qué | Cómo se verifica |
|---|---|
| el worker Stringee reenvía `lead_id` | `STRINGEE_WORKER_AUDIT_V2_2.md §5` |
| n8n alcanza el panel por red interna | PV-12 · `wget` desde el contenedor de n8n |
| efectos de `POST /followups` en LeadStudio | PV-1 |
| rendimiento con el buffer pool de producción | slow log tras F7 |
| render visual real de las pantallas | revisión manual en la copia (F2) |

## Nivel B — manual sobre una copia instalada

Los casos B1–B23 de V2.1 siguen valiendo, más:

| # | Caso | Esperado |
|---|---|---|
| B24 | "Turn off" en India desde Countries | badge COUNTRY OFF; rutas IN muestran `COUNTRY_DISABLED` |
| B25 | "Turn off" en PROVEEDOR1 | todas sus rutas `PROVIDER_DISABLED`; Stringee sigue |
| B26 | alta de país nuevo | nace OFF; "Turn on" funciona aunque no tenga rutas |
| B27 | proveedor con adapter `TWILIO_VOICE` | se guarda; "Turn on" da error "no soportado" |
| B28 | `/callcenter/analytics.json?tz=Asia/Kolkata` | JSON con overview, sin consultar LeadStudio |
