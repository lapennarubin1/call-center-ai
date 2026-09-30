# PENDING_VERIFICATION.md
**Lo que este build NO pudo confirmar, y qué hacer con cada cosa.**
Fase PARCHE PRE-STAGING r2-final2 · 21/09/2026

> **Resueltos en r2** (detalle al final): PV-15, PV-14 y PV-3.
> **Nuevos en r2**: PV-20 … PV-24.
> **Nuevos en r2-final2**: PV-27, PV-28, PV-29.

> **Qué cambió en esta lista con r2-final2.** El enclavamiento del modo de
> operación estaba aquí como "verificado por lectura del código". Ya no: se
> ejecuta el `analytics.run_due_schedules()` real con el guard puesto, y la
> respuesta real del panel se mete en el nodo real de WF2
> (`tests/test_real_interlock_v2.py`, 32 comprobaciones). Lo que sigue en esta
> lista es lo que **sigue sin poder probarse aquí**, casi todo porque depende
> de un sistema que no está en este contenedor: n8n, LeadStudio, OkPay, el
> worker Stringee o el VPS.

---

## 0. Cómo leer esta lista

| severidad | qué significa |
|---|---|
| 🔴 **BLOQUEANTE** | hay que resolverlo antes de activar en staging |
| 🟡 **ANTES DE PRODUCCIÓN** | staging funciona igual; producción no debería empezar sin esto |
| 🟢 **DECISIÓN** | el sistema funciona con el valor elegido; hay que confirmar que es el que el negocio quiere |
| ⚪ **INFORMATIVO** | queda anotado; no bloquea nada |

**Ninguno de estos puntos impide instalar el paquete en staging y correr la
batería de tests.** Lo que hay que resolver antes de que el suite **llame a un
cliente real** está marcado 🔴.

---

## 🔴 BLOQUEANTES PARA ACTIVAR EN STAGING

### PV-12 · ¿n8n llega al panel por la red interna?

> **El puerto.** El panel de producción corre en **8080**, y es el valor por
> defecto de `LM_PANEL_URL` (`http://172.18.0.1:8080`) que usan los workflows.
> Si se instala una copia de staging en 8081 —como recomienda
> `INSTALL_STAGING.md` para no pisar producción— hay que poner ESE puerto en
> `LM_PANEL_URL` del n8n de staging. Un `LM_PANEL_URL` que apunte al puerto
> equivocado es la causa más probable de que esta verificación falle.
> Nada del paquete asume una IP o un puerto más allá de ese valor por defecto
> documentado.

**Qué falta:** confirmar, desde dentro del contenedor de n8n, que el panel
responde en la interfaz del bridge Docker.

```bash
docker exec -it <n8n> sh -c \
  'wget -qO- --header="X-Service-Token: <token>" http://172.18.0.1:8080/api/routes/active | head -c 200'
```

**Por qué importa:** los siete workflows leen su configuración de ahí. Si no
llega, WF2 no despacha (fail-closed, correcto) y nada funciona.

**Evidencia a favor:** WF2/WF10/WF14 v1 ya alcanzan al worker Stringee en
`172.18.0.1:8091`, así que la ruta de red existe. Falta confirmar el **puerto del
panel**.

**Si falla:** se ajusta la variable de entorno `LM_PANEL_URL` de n8n. **No se
edita ningún nodo.**

---

### PV-11 · El enum exacto de `POST /leads/{id}/followups`

**Qué falta:** confirmación documental de los valores válidos de `outcome` y
`callStatus` en LeadStudio.

**Lo que se usa** (sale de WF9 v1, que funciona en producción):

| `outcome` | `CONNECTED` · `NO_ANSWER` · `CALLBACK` · `WRONG_NUMBER` · `FAILED` |
| `callStatus` | `ANSWERED` · `NO_ANSWER` · `FAILED` |

**Cómo confirmarlo:** una llamada de prueba en staging con cada valor, y mirar
que el follow-up quede bien en el CRM. Si alguno se rechaza, se corrige el mapeo
en **un solo nodo** (`[CRM] Build Followup Body` del motor).

**Riesgo si está mal:** los follow-ups se rechazan y se abren issues
`FOLLOWUP_NEEDS_RECONCILIATION`. No se pierde el resultado de la llamada —ya está
guardado localmente— pero el CRM queda desactualizado.

---

### ~~PV-15~~ · El endpoint de link de pago — **RESUELTO en r2**

**Cómo se resolvió:** leyendo el WF7+WF8 real de producción.

`POST /api/leads/{id}/payment-link` **no existe y nunca se usó**. El WF7 vivo
cobra en India **directamente contra OkPay** (`api.wpay.one/v1/Collect`) con
petición firmada en MD5, y tiene la rama de Nepal marcada
`[DISABLED - CONFIGURE]`.

**Qué se hizo:** el endpoint se eliminó del paquete. No se sustituyó por otra
suposición. Los dos modos que sí existen —`DIRECT_PROVIDER` y
`UNIVERSAL_ROUTER`— están en `PAYMENT_INTEGRATION_GUIDE.md`, y hay un test que
recorre todo el paquete buscando cualquier uso vivo de esa URL.

---

## 🔴 NUEVOS EN r2 — BLOQUEANTES PARA ACTIVAR PAGOS

### PV-20 · Las credenciales de OkPay

**Qué falta:** el merchant id y la clave de firma de OkPay.

En el WF7 de producción están **en claro dentro del JSON del workflow**. §61
prohíbe copiarlos al paquete, así que no viajan: se leen de
`LM_OKPAY_MCH_ID` y `LM_OKPAY_SIGN_KEY`.

**Por eso India se entrega con el pago DESHABILITADO.** Sin la clave, WF7
devuelve `CONFIG_ERROR` y no intenta cobrar. Encenderla sin credencial haría
que el agente fallase delante del cliente.

**Cómo resolverlo:** sacar los dos valores del WF7 actual, cargarlos como
credential de n8n / variables de entorno, y encender la tool desde el panel.

**Recomendación aparte:** esa clave lleva tiempo viajando en un JSON
exportable. Conviene rotarla con OkPay.

---

### PV-21 · El formato real del callback de OkPay

**Qué falta:** confirmar contra la pasarela el cuerpo exacto que OkPay manda a
`payment-callback-v2`.

El parseo está tomado del WF8 vivo, pero nunca se ha recibido un callback real
**en V2**.

**Riesgo si está mal:** un pago confirmado por el cliente no se marcaría como
confirmado. El dinero entra igual —OkPay ya cobró— pero el CRM no lo refleja y
el lead parece no haber pagado. Es de los errores más caros de detectar tarde.

**Cómo confirmarlo:** un pago de prueba de importe mínimo en staging, con el
webhook apuntando a `payment-callback-v2`.

---

### PV-25 · El acceso del panel a la API de n8n

**Qué falta:** confirmar que `N8N_API_URL` y `N8N_API_KEY` están puestos y
que el panel puede consultar el estado de los workflows.

Desde r2-final, volver de `LEGACY_BACKUP` a `V2_PRIMARY` **verifica contra
n8n** que ningún workflow legacy conflictivo sigue corriendo. Si n8n no
responde, el cambio se **bloquea**.

**Cómo confirmarlo:** abrir Call Center → Legacy Backup. La columna
*Running now* tiene que mostrar el estado de cada grupo. Si sale un aviso
de "Cannot reach n8n", falta la configuración.

**Riesgo si falta:** no se podrá volver a V2 desde el panel. Es un bloqueo
deliberado, no un fallo — pero conviene resolverlo antes de necesitarlo con
prisa.

---

### PV-26 · El enclavamiento no llega a la interfaz de n8n

**Qué es:** el guard del modo de operación vive en el panel. Cubre el
encendido manual, el masivo y el programado, porque los tres pasan por la
misma función. **No** cubre a alguien que entre directamente a n8n y active
un workflow desde allí: n8n no sabe qué es el modo de operación.

**No es resoluble desde este paquete** sin tocar n8n, que está fuera de
alcance.

**Mitigación:** la pantalla de Legacy Backup muestra el estado real de cada
grupo, así que un encendido por fuera se ve. WF14 puede detectar el
solapamiento por duplicados en el CDR.

---

## 🔴 NUEVOS EN r2-final2 — BLOQUEANTES PARA ARRANCAR EL PANEL

### PV-27 · Las cuatro contraseñas del panel, en el `.env` del VPS

**Qué cambió:** ninguna contraseña tiene ya valor por defecto en el código.
`LM_DB_PASS`, `LM_PASS`, `LM_MASTER_PASS` y `LM_SUPPORT_PASS` se leen del
entorno, y si falta alguna el panel **no arranca**: escribe qué falta y sale.

**Qué falta:** comprobar en el VPS que el `.env` las tiene. En una instalación
que ya funciona, `LM_DB_PASS` y `LM_PASS` estarán; `LM_MASTER_PASS` y
`LM_SUPPORT_PASS` pueden no estar si nadie los configuró, porque hasta ahora
el código traía un valor por defecto que los tapaba.

```bash
grep -c '^LM_MASTER_PASS=.\+' /opt/landmark-panel/.env    # tiene que dar 1
```

**Por qué no se puede probar aquí:** lo que se prueba en este paquete es que
el panel se niega a arrancar sin ellas (y se prueba de verdad, arrancándolo).
Lo que no se puede saber desde aquí es qué hay en el `.env` de ese servidor.

**Si falta:** `upgrade_callcenter_v2.sh` no toca el `.env` y el servicio no
levantará. Se añaden las dos líneas al `.env` y se reinicia. **No se resetea
ninguna contraseña existente.**

---

### PV-28 · El cutover a V2 es un acto deliberado, y nadie lo ha hecho

**Qué cambió:** la migración deja el sistema en **`LEGACY_BACKUP`**. Antes lo
dejaba en `V2_PRIMARY` apoyándose en que todas las rutas estaban apagadas —
bastaba que alguien encendiera país, proveedor y ruta para empezar a despachar
sin que nadie hubiera decidido nada.

**Qué significa:** recién instalado, **V2 no llama**, aunque los tres
interruptores estén encendidos. `/api/routes/active` devuelve cero rutas y WF2
aborta con `OPERATING_MODE_BLOCKED`.

**Qué falta:** que un MASTER haga el cutover desde Call Center → Legacy Backup,
escribiendo `SWITCH TO V2`, cuando el legacy esté apagado. El panel lo
**verifica contra n8n** antes de dejarlo (ver PV-25).

**Esto no es un pendiente que arreglar: es el procedimiento.** Está en
`PRODUCTION_DEPLOYMENT_PLAN.md` §5b.

---

### PV-29 · El script de actualización, contra el panel real

**Qué falta:** `panel/upgrade_callcenter_v2.sh` se ha probado contra una
**copia** del panel de producción (`panel-share`), no contra
`/opt/landmark-panel`. Preserva `.env`, `.session_key`, `venv`, `logs`, `run`
e `instance`, valida que el panel importa antes de reemplazar nada y deja el
respaldo con el comando de rollback impreso.

**Cómo reducir el riesgo:** correrlo primero con `DRY_RUN=1`, que enseña
exactamente qué ficheros tocaría y **no escribe nada** (probado:
`tests/test_real_interlock_v2.py`, sección K).

```bash
sudo DRY_RUN=1 bash panel/upgrade_callcenter_v2.sh
```

**Lo que no puede saber este paquete:** si ese VPS tiene ficheros añadidos a
mano dentro de `app/` que el script no conoce. El respaldo previo cubre ese
caso.

---

## 🟡 ANTES DE PRODUCCIÓN

### PV-14 · Las definiciones reales de las tablas de v1 — **PARCIAL en r2**

**Lo que se resolvió:** con `landmark-panel-safe.tar.gz` en la mano, el DDL real
de nueve tablas de v1 salió de `analytics.py`, que las crea al arrancar:

`sip_providers` · `sip_provider_pricing` · `sip_deposits` · `sip_extensions` ·
`n8n_switches` · `n8n_switch_schedules` · `app_settings` · `support_actions` ·
`telegram_sessions`

Está en `sql/legacy/schema_v1_reference.sql` (saneado: la línea `CREATE USER`
traía la contraseña de `panel_rw` en claro y se sustituyó por un marcador).

**Lo que sigue pendiente:** `cdr_panel`, `crm_leads`, `crm_conversions` y
`stringee_calls` **no tienen DDL en ningún fichero del panel** — se crean fuera
(WF14, o a mano en el servidor). `003_legacy_compat_tables.sql` las sigue
reconstruyendo por inferencia de uso.

**Cómo confirmarlo:** `SHOW CREATE TABLE` de esas cuatro en el servidor real y
contrastar contra 003.

**Riesgo si difiere:** un tipo de columna más corto trunca datos en la
sincronización de WF14. No rompe la llamada, corrompe la analítica.

---

### PV-2 · Los usuarios reales de MySQL y sus permisos

**Qué falta:** los `GRANT` de `migration.sql` e `INSTALL_STAGING.md` usan
`panel_rw` y `<n8n_user>` como nombres de ejemplo.

**Regla de diseño que conviene respetar:**

- n8n escribe **ejecución y eventos**, nunca **configuración**
- `wf_events` para n8n es **append-only** (INSERT, sin UPDATE ni DELETE)
- nadie tiene `DELETE` sobre `call_routes` ni sobre `wf_events`

---

### PV-3 · El worker Stringee — **RESUELTO en esta fase**

El audit de la fundación lo dejó como `UNDETERMINED` por no tener el código.
**En esta fase el código se leyó.**

> **`bridge-call.js` reenvía `lead_id` a ElevenLabs.** El suite V2 funciona con
> el worker **tal como está hoy**. Conclusión: `NO_CHANGE_REQUIRED` para operar.

Detalle completo, con el diff opcional de ~14 líneas, en
`STRINGEE_MINIMAL_PATCH_PROPOSAL.md`.

Dos hallazgos del código que el audit no tenía:

- el worker acepta `callback_url` y POSTea el resultado final, **incluido
  `elevenlabs_conversation_id`** → WF9 V2 lo usa como vía rápida para Stringee
- `from_number` y `agent_id` salen del **request** (con `.env` de respaldo) → el
  `caller_id` y el agente que se configuren en la ruta **sí tienen efecto**

**Queda por confirmar en staging:** que `MAX_CONCURRENT_CALLS` del worker sea
**≥** la capacidad configurada en la ruta `IN_STRINGEE`. Si no, el exceso vuelve
como `429` → `RELEASED` → reintento técnico. No se pierde ningún lead, pero se
gasta ciclo.

---

### PV-16 · La ruta de compatibilidad para post-calls de v1

**Qué falta:** decidir el valor de `wf_settings.legacy_compat_route_key`.

Durante la convivencia, un post-call de una llamada lanzada por **v1** llega sin
`route_key`. Hoy el valor está **vacío**, y eso significa **no procesarlo**
(fail-closed) dejando constancia en el log.

**Opciones:**

1. dejarlo vacío — v1 procesa sus propios post-calls; V2 los ignora. **Es lo más
   limpio** si cada ruta está en un solo sistema
2. apuntarlo a una ruta archivada creada a propósito (`LEGACY_V1`) con la política
   estándar — V2 los absorbe

**Nunca se adivina la ruta por el prefijo del teléfono.**

---

### PV-22 · El contrato del router universal

**Qué falta:** nadie ha respondido todavía con la forma que define
`GENERIC_JSON_V1` (`success`, `payment_url`, `payment_id`, `provider`).

No bloquea nada: el modo `UNIVERSAL_ROUTER` sólo se usa si un país se configura
así, y hoy ninguno lo está. Cuando exista ese backend, hay que contrastar su
respuesta real contra el contrato antes de encenderlo.

---

### PV-23 · El vínculo de SIP Balance con más de un proveedor legacy

**Qué falta:** hoy `sip_providers` tiene **una sola fila** en la instalación
real, así que la migración la vincula automáticamente a `proveedor1`.

Si en el servidor real hubiera más de una, la migración **no adivina**: deja
`legacy_sip_provider_id` en NULL y el panel lo muestra como `NOT LINKED` para
que alguien elija. No es un error, es una decisión que no corresponde al script.

**Cómo confirmarlo:** `SELECT id, name FROM sip_providers;` en producción.

---

### PV-24 · El filtro de proveedor en el dashboard legacy

**Qué es:** en el dashboard que ya existía, el selector de proveedor cambia el
embudo y las cuentas, pero **no** el gráfico Call Traffic ni el export diario:
ésos leen `cdr_panel`, que es tráfico de Asterisk y no distingue proveedor.

**No se ha cambiado**, a propósito: §32 pide no alterar en silencio la
semántica existente. Queda anotado para que nadie lo lea como un fallo nuevo.

En la analítica **V2** el filtro de proveedor sí afecta a todo, porque ahí cada
llamada guarda por qué proveedor salió.

---

## 🟢 DECISIONES DE NEGOCIO

### D-1 · ¿Qué se hace con `FAILED` y `UNKNOWN`?

v1 se contradecía: WF2 los trataba sin reintento y WF9 los mapeaba a `NO_ANSWER`
(y por tanto reintentaba).

**V2 no elige:** la política los deja en `unmatched_action: NONE` — se registra el
resultado y no se programa ni se cierra nada.

Para que reintenten como un no-contesta, se agrega a la política **desde el
panel**:

```json
{ "result": "FAILED", "attempt": "*", "action": "RETRY", "delay": "+3h" }
```

Sin tocar n8n. Ver `FOLLOWUP_POLICY_GUIDE.md` §6.

---

### D-2 · `conversion_rate` y el modelo de atribución

**Lo que hace hoy:**

- `conversion_rate = accounts_opened / answered` del **mismo rango**
- atribución `LAST_CONNECTED_CALL_V1`: última llamada contestada del lead, ventana
  30 días; sin llamada previa, la cuenta queda `UNATTRIBUTED`

**Lo que hay que decidir:** si se quiere por **cohortes** (atribuir la cuenta al
día de la llamada, no al de la apertura) y si 30 días es la ventana correcta.

Ambas cosas responden preguntas distintas. El modelo viaja declarado en la
respuesta para que nadie compare números de modelos diferentes.

---

### D-3 · Feriados en los días hábiles

`+2bd` salta **sábados y domingos**. No conoce Diwali, Dashain ni el 12 de
octubre.

Si hace falta, se agrega un calendario de feriados por país. Hoy **no existe**, y
eso significa que un reintento puede caer en un feriado local.

---

### D-4 · Capacidad y franjas iniciales

El seed usa los valores que v1 tenía **hardcodeados**:

| ruta | capacidad | de dónde sale |
|---|---|---|
| `IN_PROVEEDOR1` | 6 | `BATCH_SIZE = 10` limitado a 6 en WF2 v1 |
| `IN_STRINGEE` | 1 | `BATCH_SIZE = 1` |
| `NP_PROVEEDOR1` | 6 | (la ruta estaba apagada en v1) |

**No se sembraron franjas horarias**, a propósito: hoy v1 llama 24/7 y sembrarlas
cambiaría el comportamiento en silencio.

Hay que decidir si el negocio quiere una ventana operativa (p. ej. 9–20 hora
local) y cargarla desde el panel.

---

### D-5 · Los markets de CashStudio

Confirmados: `IND` (India) y `NPL` (Nepal).

Para cualquier país nuevo, el `market` hay que **pedirlo**. El suite **no lo
inventa**: un `market` vacío es `CONFIG_ERROR` y la tool queda apagada.

---

## ⚪ INFORMATIVO

### PV-13 · Semántica de `GET /api/leads/queue`

WF2 V2 pide **una sola cola por país** con `limit = demanda`, asumiendo que
`/queue` es una **lectura** y no una reserva.

**Evidencia:** v1 llamaba `/queue` en paralelo desde dos ramas con los mismos
parámetros, y el riesgo conocido era justamente que ambas tomaran el mismo lead —
comportamiento de lectura.

**Si resultara que reserva:** el reparto sigue siendo correcto (el claim atómico
es por lead), pero convendría revisar el `limit` para no reservar de más. Está
documentado en la sticky note 02/03/04 de WF2.

---

### PV-1 · ¿LeadStudio mueve `status` y `attempts` solo?

No está confirmado si el CRM actualiza esos campos al recibir un follow-up.

**Cómo está resuelto por diseño:**

- `next_attempt()` usa `max(attempts del CRM, MAX local) + 1`, así que si el CRM
  no incrementa, el `UNIQUE(lead_id, attempt)` **no bloquea** al lead
- el `PATCH` de estado del motor es **opcional**: si se confirma que el CRM lo
  hace solo, se apaga con `crm_patch_enabled = false` sin tocar nada más

---

### PV-17 · El formato de error de ElevenLabs SIP

`[ELEVENLABS_SIP] Normalize Dispatch` extrae el código SIP con una expresión
regular sobre el campo `message`, igual que v1 (`/SIP\s*(\d{3})/i`). El formato
exacto de error de ElevenLabs no está documentado en el material disponible.

**Riesgo acotado:** si el regex no matchea, el resultado cae en `UNKNOWN`
(conservador), no en un `NO_ANSWER` falso. Se reconcilia a mano en vez de
consumir un intento de negocio indebidamente.

**Cómo confirmarlo:** juntar respuestas de error reales en staging y ajustar el
patrón si hace falta.

---

### PV-18 · El campo `sip_code` en el post-call de ElevenLabs

`[RESULT] Normalize Call Result` busca el código SIP en
`metadata.sip_code`, `metadata.sip_status_code` y
`metadata.termination_reason_details.sip_code`.

Si ElevenLabs lo expone con otro nombre, el resultado queda `FAILED` sin
`sip_code` y **la política no lo reclasifica** a `NO_ANSWER` — es decir, no
reintenta. Conservador, pero conviene confirmarlo con un post-call real.

---

### PV-19 · Idempotencia del lado de LeadStudio

LeadStudio no documenta una idempotency key ni un lookup de follow-up por
`conversation_id`.

Por eso, cuando un `POST /followups` se envía y la respuesta no llega, el motor
marca `NEEDS_RECONCILIATION` y **no reintenta**. Si el CRM ofreciera un lookup
verificable, ese caso pasaría a resolverse solo.

---

## Resumen

| | |
|---|---|
| 🔴 bloqueantes para staging | **2** — PV-12, PV-11 |
| 🔴 bloqueantes para activar pagos | **2** — PV-20, PV-21 |
| 🔴 bloqueantes para arrancar el panel | **1** — PV-27 |
| 🟡 antes de producción | **8** — PV-14 (parcial), PV-2, PV-16, PV-23, PV-25, PV-26, PV-28, PV-29 |
| 🟢 decisiones de negocio | **5** — D-1 … D-5 |
| ⚪ informativos | **6** — PV-13, PV-1, PV-17, PV-18, PV-19, PV-24 |
| ✅ resueltos | **3** — PV-15, PV-3, y PV-14 en su mayor parte |



**Ninguno impide instalar el paquete y correr los 604 tests.**

**Lo que esta lista NO contiene, a propósito:** nada que los tests de este
paquete verifiquen de verdad. Un punto sale de aquí cuando hay una prueba que
ejecuta el camino real, no cuando la documentación afirma que está bien.
