# Auditoría: Talk time 0 s en llamadas Asterisk contestadas

**Estado: solo auditoría + propuesta + diff + tests. No se tocó producción, ni WF10, ni Stringee, ni se hizo backfill.**
Todo se probó en local (nodos reales de n8n 1.123.82 y de WF2/WF9/WF10, MariaDB, tabla `cdr` con el esquema estándar de Asterisk).

---

## 0. Veredicto en cinco líneas

1. **Causa raíz confirmada:** `🏁 Guardar Resultado (Asterisk)` (WF2) hace el `POST /followups` con `durationSeconds: 0` **al contestar**, porque la API de ElevenLabs responde al contestar, no al colgar. En el caso real: Activity 06:44:20, fin de la llamada ~06:45:24 → el billsec (88 s) todavía no existía.
2. **LeadStudio no permite corregirlo después** (`PATCH /followups/{id}` → 400 "Nothing to update"), así que **la Activity tiene que nacer cuando la llamada ya terminó**.
3. **Solución mínima y segura = Opción A, en su forma más pequeña:** para Asterisk *contestada*, WF2 solo despacha, deja el lead `ATTEMPTING` y anota la llamada en `wf_call_events`; **WF9 crea la ÚNICA Activity al terminar** con `CDR.billsec` (respaldo: duración de ElevenLabs; 0 si no hubo conversación). WF2 sigue creando las Activities de NO_ANSWER / FAILED / etc.
4. **Ownership:** *no* se mantiene para ese caso. **"Asterisk Activity owner = WF2" se conserva para NO_ANSWER/FAILED/agotado; para Asterisk ANSWERED el dueño de la Activity terminal pasa a WF9.**
5. **107/107 tests anteriores siguen pasando** con el candidato (43 + 21 + 43) y hay **66 tests nuevos** (A-DUR-1..10 + consecuencias). WF10 y Stringee: sin cambios.

---

## 1. Diagnóstico

| Pieza | Evidencia |
|---|---|
| El POST lleva `durationSeconds: 0` | Código de `🏁 Guardar Resultado (Asterisk)` (el que pegaste) y test **BUG-REPRO**: con el WF2 del paquete anterior, una llamada contestada genera 1 POST `CONNECTED/ANSWERED` con `durationSeconds = 0` |
| Se crea antes de colgar | ElevenLabs sip-trunk devuelve al contestar (`success:true` + `conversation_id`). Además el nodo HTTP procesa en lotes de 5 (timeout 60 s): la Activity sale cuando el lote entero resolvió. Caso real: +62 s desde el inicio, la llamada duró 126 s |
| No se puede corregir | `PATCH /followups/{id}` con `{"durationSeconds":88}` → 400 (probado por ti) |
| Dato correcto | `CDR.billsec` = 88 = WAV 88,8 s. `cdr.duration` (126) incluye el timbrado → **nunca** se usa |
| WF9 ya sabía crear Activities con duración | `applyCall()` usa `durationSec`; solo le faltaba el CDR y ser dueño de estas llamadas |

Por eso cambiar `durationSeconds: 0` por una consulta al CDR dentro de WF2 **no sirve**: la llamada sigue activa.

## 2. Opciones evaluadas

| | Opción | Resultado |
|---|---|---|
| **A** | WF2 difiere solo las **contestadas**; WF9 crea la Activity al terminar con `CDR.billsec` | **Recomendada.** Reutiliza lo que WF9 ya tiene (inbox durable `wf_call_events`, claim con lease, idempotencia por `providerCallId`/`Ref:`, 3x3, stale). Cambios acotados y por nodo (ver diff): 2 nodos nuevos por workflow |
| **B** | Mantener a WF2 como dueño y que espere el fin de la llamada | **Descartada.** WF2 tendría que sondear ElevenLabs hasta que termine (minutos) dentro de un flujo por lotes con timeouts, sin lease ni idempotencia durable; o duplicar en WF2 el clasificador de conversaciones (buzón/silencio/callback) y el 3x3 que ya viven en WF9 → **dos implementaciones del mismo ciclo de vida**. Es más grande y menos seguro que A |
| **C** | Otro endpoint de LeadStudio que edite `durationSeconds` | **No demostrada; no se propone.** Lo único probado es que `PATCH /followups/{id}` lo rechaza. El campo `talkTimeSeconds` del lead existe, pero no se sabe si es escribible, y aunque lo fuera no arreglaría el `durationSeconds` de la Activity (lo que ve el CRM). `PROBE_CDR_READONLY.sh` §8 busca un OpenAPI/Swagger **solo con GET** por si existe; no ejecuta ningún PATCH/PUT/POST |

## 3. Respuesta explícita sobre ownership

> **Sí: para Asterisk *ANSWERED*, el dueño de la Activity terminal pasa a WF9.** LeadStudio obliga a fijar `durationSeconds` en el POST inicial y el billsec solo existe al colgar; el único componente que ya espera el fin de la llamada de forma durable es WF9.
>
> **WF2 sigue siendo dueño** de las Activities de NO_ANSWER (SIP 603/408/486), FAILED telefónico real, agotado, y de los errores de proveedor (sin Activity ni intento, como hoy).

Salvaguarda de "nunca perder una llamada": WF2 solo difiere si el registro en `wf_call_events` quedó **confirmado** (INSERT + SELECT de verificación). Si MySQL falla, WF2 crea la Activity inmediata como antes y WF9 la reconoce (`ASTERISK_OWNED_BY_WF2`) → una sola Activity (tests C15/C15b).

## 4. La propuesta (mínima) y qué NO cambia

```
ANTES   WF2: dial → contesta → POST /followups (duración 0)  → WF10 sube grabación
AHORA   WF2: dial → contesta → ledger en wf_call_events (lead sigue ATTEMPTING, 0 POST)
        WF9: conversación terminal + 45 s → CDR.billsec → 1 POST /followups → ciclo 3x3 → wf_call_followups
        WF10 (sin cambios): grabación → fila de wf_call_followups → PUT /calls/recording/{followup_id}
```

**WF2 (64 → 66 nodos; ver `DIFF_WF2.patch`)**
- Nuevos: `🧾 Build Deferred Ledger (Asterisk)` (Code) y `💾 Ledger Deferred (Asterisk)` (MySQL, INSERT … ON DUPLICATE + SELECT de confirmación) entre Classify y Guardar.
- `🏁 Guardar Resultado (Asterisk)`: para contestadas confirmadas no hace POST ni PATCH (`deferred_to_wf9: true`). NO_ANSWER/FAILED: idéntico.
- `🧹 Mantenimiento`: la retención de "job sin resolver" (antes solo Stringee) ahora cubre también las filas Asterisk diferidas → no libera el lead mientras WF9 espera.

**WF9 (25 → 27 nodos; ver `DIFF_WF9.patch`)**
- Nuevos: `🧾 Build CDR SQL (Asterisk)` + `🗄️ CDR Asterisk (billsec)` (solo lectura; si falla, `continueRegularOutput` y se usa ElevenLabs).
- `⚙️ Fase 1`: para eventos diferidos crea la Activity con `durationSeconds = billsec`, escribe `wf_call_followups` con `created_at` = inicio real de la llamada (por eso **WF10 no necesita cambios**), y si nunca aparece la conversación (404 / no terminal) tras 2 h / 6 h registra igual UNA Activity NO_ANSWER "sin conversación confirmada".
- Claim: `lead_newer_dispatch` (para no confundir una automatización post-llamada con un despacho nuevo).

**No cambia:** WF10, Stringee (nodos byte a byte idénticos, test C12c), 3x3, máximo 9, `POST /followups` como único que incrementa `attempts`, `nextActionAt` como único mecanismo de `nextFollowUpAt`, `UNRESPONSIVE` como stage.

### Fuente de duración (orden pedido)
1. `CDR.billsec` — se une por **últimos 10 dígitos del teléfono + epoch del uniqueid** (independiente de zona horaria) y se **alinea con la conversación** de ElevenLabs (inicio ±60 s, fin ±90 s). Si hay dos candidatos igual de plausibles (>15 s de diferencia mínima) o ninguno, **no se adivina**. Registros del mismo tramo (mismo uniqueid duplicado, o pata Local con ±2 s / ±3 s) cuentan como uno.
2. `ElevenLabs metadata.call_duration_secs`.
3. `0` solo si no hubo conversación conectada. Nunca `cdr.duration`.
4. Si el CDR aún no se escribió, WF9 espera hasta 150 s tras colgar antes de caer a ElevenLabs.

## 5. Consecuencias que pediste verificar (punto 14)

| # | Consecuencia | Resultado | Test |
|---|---|---|---|
| 1 | Mantenimiento no libera el lead mientras WF9 espera (incluso a los 45 min > STALE 30) | ✅ | C1 |
| 2 | `wf_call_events` protege el lead | ✅ | C2 |
| 3 | No hay ventana de re-marcado del mismo lead | ✅ | C3 |
| 4 | `attempts` solo sube cuando WF9 hace el POST (+1 exacto) | ✅ | C4, C4b, A-DUR-3 |
| 5 | Intento 9 contestado → CONTACTED, no UNRESPONSIVE; nunca intento 10 | ✅ | A-DUR-9, 9b–9e |
| 6 | Callback pedido funciona (fecha vía `nextActionAt`, duración = billsec) | ✅ *(cambio de comportamiento: en Asterisk antes se ignoraba)* | C6 |
| 7 | Buzón/silencio/operador → NO_ANSWER sin Activity CONNECTED temporal ni PATCH a CONTACTED | ✅ | A-DUR-8, 8b, 8c |
| 8 | `nextActionAt` sigue siendo el único mecanismo (ningún PATCH escribe `nextFollowUpAt`) | ✅ | C8 |
| 9 | Eventos stale no pisan un ciclo de vida más nuevo (estado cambiado a mano; despacho más nuevo) | ✅ | C9a, C9b, C9c |
| 10 | WF10 encuentra después la fila correcta | ✅ (nodos reales de WF10) | A-DUR-5 |
| 11 | `recording_synced` sigue funcionando (reenvío → `not_pending`) | ✅ | A-DUR-5c |
| 12 | Stringee no se afecta | ✅ | C12, C12b, C12c + 43 tests base |
| 13 | Sigue sin existir intento 10 | ✅ | A-DUR-9c/9d |
| 14 | Rollback: WF2 nuevo + WF9 viejo no pierde llamadas | ✅ (WF9 viejo trata la fila como hueco: 1 Activity con duración ElevenLabs) | C16 |

## 6. Tests nuevos pedidos (A-DUR-1..10)

| ID | Qué prueba | Resultado |
|---|---|---|
| A-DUR-1 | `durationSeconds` del CRM == `CDR.billsec` (88), no 126 ni 90 | ✅ |
| A-DUR-2 | 1 sola Activity por llamada física (1 POST en total; 0 al contestar) | ✅ |
| A-DUR-3 | `attempts` +1 exacto | ✅ |
| A-DUR-4 | `followup_id` de `wf_call_followups` == id de la Activity (y `created_at` = inicio de la llamada) | ✅ |
| A-DUR-5 | WF10 real sube al **mismo** `followup_id`; grabación antes de la Activity → 503 (el worker reintenta) | ✅ |
| A-DUR-6 | CDR no disponible (sin filas / tabla inexistente-sin permiso / ambiguo) → duración de ElevenLabs; CDR tardío; CDR duplicado; FQDN en uniqueid | ✅ |
| A-DUR-7 | NO_ANSWER: Activity de WF2, duración 0, sin ledger; contestada sin conversación → 0 s | ✅ |
| A-DUR-8 | Buzón / silencio: nace NO_ANSWER, nunca CONNECTED temporal | ✅ |
| A-DUR-9 | Intento 9 contestado → CONTACTED; sin conversación → CLOSED + UNRESPONSIVE | ✅ |
| A-DUR-10 | Webhook ×2 + polling ×2 + ledger repetido + reproceso forzado → 1 Activity, `attempts` +1 | ✅ |

**Totales:** `sim_dur.js` **66/66** · base con el candidato: `sim.js` 43/43, `sim_wf2.js` 21/21, `validate_structure.js` 43/43 (**107/107 preservados**; también se corrieron contra el paquete anterior como control).

**Cómo sé que los tests muerden:** con mutantes (desactivar el diferido, usar `cdr.duration`, ignorar el CDR, quitar la tolerancia de ambigüedad, etc.) los tests correspondientes fallan (con el diferido de WF2 apagado fallan más de 25 checks).
**n8n real 1.123.82:** los nodos nuevos (ledger + Guardar + CDR) se ejecutaron en n8n real: CDR con billsec 88, tabla inexistente → item de error y sigue, sin filas → `[{}]`, ledger idempotente en la 2ª ejecución.
**Revisión independiente** (un revisor aparte, con casos adversariales): sin defectos altos; 7 hallazgos menores, **los 7 corregidos y con test** (evento STALE tras Activity de un agente, uniqueid con FQDN, CDR de pata Local, CDR tardío, CDR de otra llamada sin alineación, `lead_id`/`phone` vacíos en ElevenLabs, provider erróneo en el ledger).

## 7. Riesgos, cambios de comportamiento y prerrequisitos (léelos antes de desplegar)

1. **La Activity aparece ~1,5–3 min *después de colgar***, no al contestar (necesario: no hay otra forma con duración real). Durante la llamada el lead figura `ATTEMPTING`.
2. **Worker de grabaciones (`wf10-worker.py`):** la fila de `wf_call_followups` ahora existe 1–3 min después de que llega el WAV. WF10 ya responde 503 (`followup_not_found`) y el worker debe **reintentar ≥ 15 min** con timeout HTTP ≥ 90 s. **No lo he visto**: `PROBE §9` lo revisa. Si el worker no manda `uniqueid`, el emparejamiento es por franja horaria y una reintentada tras >30 min no coincidiría.
3. **CDR:** no sé el nombre de la tabla ni los permisos del usuario de la credencial de n8n. Si es `asteriskcdrdb.cdr`, hay que poner `const CDR_TABLE = 'asteriskcdrdb.cdr'` en `🧾 Build CDR SQL (Asterisk)` y dar `GRANT SELECT` (la sonda imprime el comando; **no lo ejecuta**). Sin CDR el sistema **sigue funcionando** pero usa la duración de ElevenLabs.
4. **No hay enlace exacto por uniqueid** entre la conversación de ElevenLabs y Asterisk: se une por teléfono + tiempo (+ alineación). Ante ambigüedad usa ElevenLabs. La sonda §5 mide la tasa de unión real (esperado ≥ 90 %).
5. **WF2 de producción tiene 86 nodos; mi paquete, 64.** Por eso **no entrego un WF2 completo para importar**: entrego un diff por nodo con pre-imagen exacta y un builder que **aborta si el nodo de producción difiere**. Hay que verificar que ningún nodo posterior a `🏁 Guardar Resultado (Asterisk)` en producción (Telegram, hojas, logs…) dependa de `followup_id`/`followup_ok` de las contestadas; ahora vienen `null` con `deferred_to_wf9: true`.
6. **Un evento sin resolver retiene el lead hasta 6 h** como máximo (después WF9 registra igual una Activity). No agregué alarma (Telegram) para eventos atascados; recomendable como siguiente paso.
7. **Callbacks en Asterisk ahora funcionan** (antes se ignoraban): cambio de comportamiento deseado, pero es nuevo.
8. **Sin backfill:** las Activities ya creadas con 0 s quedan como están.

## 8. Rollout y rollback (cuando lo apruebes)

- **Orden:** 1) `PROBE_CDR_READONLY.sh` → 2) ajustar `CDR_TABLE`/GRANT si hace falta → 3) **WF9 primero** (sin filas diferidas se comporta como hoy: 43 tests base) → 4) WF2 → 5) 3 llamadas contestadas reales (Mexico/Nepal/India) + 1 no contestada y comprobar `durationSeconds == billsec`, 1 Activity, grabación subida.
- **Rollback:** reimportar el WF2 anterior (el lead deja de diferir); con WF9 nuevo o viejo las filas ya diferidas se cierran solas: el WF9 viejo espera 20 min y crea UNA Activity con duración de ElevenLabs (C16); el Mantenimiento viejo libera el lead a los 30 min. No hay `DELETE`, `DROP` ni cambios de esquema.

## 9. Lo que necesito de ti (no bloquea la revisión de este paquete)

1. Export de producción de **WF2 (86 nodos)** y **WF9** → correr `node builder/build_dur.js --wf2 WF2_prod.json --wf9 WF9_prod.json --out out_prod`: si algún nodo difiere, dice cuál.
2. `/usr/local/bin/wf10-worker.py` (o la salida de la sonda §9).
3. La salida de `bash PROBE_CDR_READONLY.sh` (solo lectura, teléfonos enmascarados).

```bash
cd /root/wf10 && chmod +x PROBE_CDR_READONLY.sh
# opcional: N8N_MYSQL_USER=<usuario de la credencial "MySQL account" de n8n>
bash PROBE_CDR_READONLY.sh
ls -t /root/probe_cdr_*.txt | head -1
```

## 10. Archivos

| Archivo | Qué es |
|---|---|
| `DIFF_WF2.patch`, `DIFF_WF9.patch`, `CHANGES_dur.json` | Diff exacto por nodo (antes/después) y conexiones cambiadas |
| `builder/` | `build_dur.js` + fuentes: regenera el candidato desde tus exports, con pre-imagen exacta |
| `tests/` + `resultados/` | `sim_dur.js` (66 tests nuevos), los 107 base, esquema `cdr`, y resultados JSON |
| `PROBE_CDR_READONLY.sh` | Sonda de solo lectura (CDR, permisos, unión real, worker, OpenAPI por GET) |
