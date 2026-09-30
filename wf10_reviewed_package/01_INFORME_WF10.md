# WF10: informe (causas, cambios, riesgos, validación)

Alcance: se tocaron WF10 y el productor Asterisk. **No** se tocaron WF2, WF9, `wf_call_events`, las 891 filas `undefined`, las reglas 3x3/9, el lifecycle ni la configuración de Stringee. WF10 no crea Activities: solo adjunta el audio a la Activity que ya creó WF2 (Asterisk) o WF9 (Stringee), usando `wf_call_followups`.

Límite de esta entrega: no tengo acceso a tu VPS. Todo se probó en local con las mismas versiones y una réplica de tus 3 contextos:

- n8n 1.123.82 real
- MariaDB 10.11
- Asterisk 20.6 real

La prueba con llamadas reales nuevas la hace `VALIDATE_WF10_AFTER_DEPLOY.sh` en tu servidor.

---

## ROOT CAUSE ASTERISK

Tu dialplan hace `MixMonitor(${RECORDING},b)` → `Dial(...)` → `StopMixMonitor()` → `System(wf10-enqueue.py … ${DIALSTATUS})`.

**`Dial()` no tiene la opción `g`.** Por eso, cuando una llamada **contestada** termina (cuelgue quien cuelgue), el canal se cierra dentro de `Dial()` y las prioridades siguientes **nunca se ejecutan**. `System()` solo corre cuando `Dial` vuelve sin contestar (NOANSWER/BUSY/…). Con la opción `b` ese WAV tiene 44 bytes y el enqueue lo descarta (acepta solo `ANSWER` y WAV > 44).

Resultado: casi ninguna contestada llega a la cola. Eso coincide con lo que viste: enqueue.log sin actividad, `queue=0`, `connected=41` / `synced=4`. Los pocos synced salen probablemente del cron viejo, que solo toma billsec > 60 (inferencia; el CDR del preflight lo muestra).

Lo reproduje en Asterisk 20.6 con tus 3 contextos: 3 llamadas contestadas (WAV de 259 KB, 96 KB y 364 B) dieron **0 encoladas**.

**Fix: el 3.er argumento de `MixMonitor`, el comando de post-proceso.**

- **Por qué este mecanismo.** Asterisk lo ejecuta en **todas** las llamadas, después de cerrar el WAV (verificado: el tamaño coincide con el header RIFF), sin importar quién cuelga.
- **Qué cambia.** Se modifica **en su lugar** la línea existente: `MixMonitor(${RECORDING},b,/usr/local/bin/wf10-mixmon-post.sh <los mismos args del System>)`.
- **Qué hace el post-script.** Llama al **mismo** `wf10-enqueue.py`, que es idempotente por UNIQUEID, y solo si el WAV tiene audio. Con `b`, un WAV > 44 bytes implica que hubo bridge, o sea, que fue contestada.

Por qué no usé extensión `h` ni hangup handler:

- `h` corre al colgar el canal, mientras MixMonitor puede seguir cerrando el archivo en su hilo, así que el WAV podría estar incompleto.
- Un hangup handler exige **insertar** una prioridad antes de `Dial`. Verifiqué que insertar líneas y hacer reload con llamadas en curso hace que, al volver de `Dial`, el canal **vuelva a marcar al cliente**.
- Modificar una línea existente no corre la numeración, así que el reload es seguro. Probado con una llamada activa: sin re-marcado y con el dialplan idéntico.

---

## ROOT CAUSE STRINGEE

Verificado en n8n real con la configuración original de los nodos.

1. **La causa principal (tu hipótesis era correcta).** `🔗 Attach Audio + Token (Stringee)` corría en *Run Once for All Items* con `$input.item` y devolvía **un** objeto. n8n emite **1 item por ejecución**: 3 entradas dan 1 salida (probado). Resultado: 1 PUT por ciclo y el ledger avanzando "de a una".
2. **Ledger sin confirmar el PUT.** El ledger se armaba con los items de Attach, no con el resultado del PUT. Una grabación con PUT fallido quedaba "enviada" y **no se reintentaba nunca**. PREFLIGHT §9 cuenta cuántas quedaron así; no se reprocesan sin tu aprobación.
3. **PUT a `undefined`.** El `try/catch` de Attach devolvía `{_attach_error}` sin `followup_id`, y eso terminaba en un PUT a `/calls/recording/undefined`.
4. **Solo "hoy UTC".** Lo que no se subía antes de las 00:00 UTC quedaba afuera para siempre.
5. **Lookup sin relación con la hora de la grabación.** Tomaba la última fila pendiente del teléfono con `call_status='ANSWERED'`.
   - Con 2 llamadas al mismo número, el audio podía ir al followup equivocado.
   - Los buzones (`NO_ANSWER` con `recording_synced=0`) nunca se emparejaban.
6. **Errores ocultos.** `continueOnFail` en Lookup y Attach convertía los errores en items "normales".

---

## CAMBIOS (WF10_FINAL_PRODUCTION.json, 35 nodos)

**Se mantiene igual:**

- Mismo id `TW1CHksqyf66MjQz`, nombre y settings.
- Mismo `POST /webhook/send-recording` (mismo webhookId).
- Mismas credenciales: MySQL `4NiWyzzJ4JbMkIjT` y Telegram `aZWABzEfC4SEy3oC`.
- Misma regla del Schedule.
- Mismos Login.
- Mismos chats y la misma regla de Telegram (≥ 60 s).

**Se sacó:**

- El nodo huérfano `⏸️ Asterisk Desactivado (log)`.
- El `pinData`. Solo se usa en pruebas manuales, y el pin del Webhook era una grabación real de un cliente (3,3 MB) que una prueba manual podía volver a subir.

**Regla del dueño** (una llamada física = un followup). La grabación va a la **primera** fila del **mismo proveedor** y el mismo teléfono (últimos 10 dígitos) creada desde el inicio de la llamada:

| Proveedor | Referencia de inicio | Ventana |
|---|---|---|
| Asterisk | UNIQUEID | −2 min … +15 min |
| Stringee | timestamp del archivo | −2 min … +90 min |

- Se ignoran las filas `undefined`.
- Si el dueño ya tiene `recording_synced=1`, no se sube.
- `provider='asterisk'` y `'stringee'` nunca se cruzan (probado en las dos direcciones).

**Rama Asterisk (webhook):**

- **Emparejamiento.**
  - `Convert` valida el WAV y extrae el UNIQUEID de `uniqueid` o de `test-<UNIQUEID>.wav`.
  - Si el payload no trae UNIQUEID (el cron viejo manda `<tel>_<fecha>.wav`), se busca en la franja "ahora − duración − 30 min". Se prefiere la fila pendiente; una fila vieja **nunca** se asigna.
- **Decisión** (`🧮 Decidir Match`):
  - `attach`
  - `not_pending` (200, ya subida)
  - `followup_not_found` (503, reintentar)
  - `invalid_audio` (200)
- **Subida.** `Attach` va por ítem y sin `.first()` → PUT → `🧾 Evaluar PUT`. Cuenta como OK **solo** con 2xx + `hasRecording=true` + `followUpId` coincidente.
- **Respuesta al worker.** El webhook ahora responde **al final**, con el estado real:
  - 200 `attached` / `not_pending`.
  - 503 `put_failed`, con reintento.
  - 200 `put_rejected` recién al **2.º** rechazo permanente de LeadStudio (400/404/410/413/415/422), para que el worker no reintente para siempre.

**Rama Stringee (schedule):**

- **Todas las pendientes por ciclo.** Hasta 10 subidas por ciclo, cada una con **su propio** audio (verificado por hash), la más vieja primero.
- **Un solo archivo por llamada.** Un followup recibe solo el archivo más viejo de su llamada, aunque ese archivo haya fallado antes.
- **Espera sin dueño.** Sin fila de WF9 todavía, espera sin marcar nada.
- **Reintentos.** Una falla no corta el ciclo: el PUT tiene *On Error: continue*.
  - Espera creciente: próximo ciclo, luego 5 min, 15 min y 60 min.
  - Después de 2 rechazos permanentes deja de intentar.
  - Las grabaciones sin fallas previas van primero, así que una que falla siempre no bloquea a las nuevas.
- **Sin backfill.** Nunca toma grabaciones de más de 30 min antes del primer ciclo del WF10 nuevo. El backlog se sube solo con `BACKLOG_FROM` y tu aprobación. Alcance rodante: 72 h, sin el corte de medianoche UTC.

**En ambas ramas:**

- `recording_synced=1` **solo** después de que LeadStudio confirma. El `UPDATE` exige `provider` exacto y que siguiera en 0.
- **Ledger y Telegram salen una sola vez.** Solo para la ejecución que pasó la fila de 0 a 1 (`ROW_COUNT()`), aunque el worker y el cron entreguen la misma llamada a la vez (probado).
- **El ledger es solo de PUT confirmados.** Una grabación cuyo dueño ya está sincronizado se omite: sin PUT y sin ledger.
- **Errores visibles.** Se quitó `continueOnFail` en Lookup y Attach, y en Mark se quitó el reintento automático. Un error de MySQL corta la ejecución sin marcar nada.

**Detalle de n8n 1.123 encontrado en las pruebas.** Un *Respond to Webhook* cuyo body usa `$('Otro nodo')` responde **vacío**. Por eso el body se arma en el Code node `🧾 Resultado Subida (Asterisk)`.

**Asterisk.** `ASTERISK_WF10_FIX.sh` hace lo siguiente:

- `check` es solo lectura.
- `apply`:
  - Hace backup.
  - Aborta ante cualquier formato inesperado o si hay ediciones en disco sin cargar.
  - Hace escritura atómica y `dialplan reload`, sin restart.
  - Verifica que el dialplan cargado sea idéntico salvo las líneas MixMonitor, o se revierte solo.
- `finalize` exige al menos 3 encoladas por el mecanismo nuevo y comenta el cron viejo.
- `rollback` restaura exacto, también con Asterisk caído.

---

## RIESGOS (y cómo se controlan)

1. **Contrato del `wf10-worker.py` real.** No vi su código.
   - WF10 necesita que el worker reintente ante un 503 y que su timeout HTTP sea ≥ 90 s.
   - Para el match exacto, el payload tiene que llevar el UNIQUEID. Sin él se usa la franja horaria.
   - Control: PREFLIGHT §2 lo muestra, y VALIDATE detecta "en done/ pero `recording_synced=0`".
2. **Ventanas de tiempo** (Asterisk 15 min, Stringee 90 min). Suponen que WF2/WF9 escriben la fila poco después de la llamada. PREFLIGHT §11 lo mide con tu CDR. Si algo cae afuera, se ajustan 2 constantes **antes** de publicar.
3. **Llamada Asterisk sin fila de WF2 y re-marcada en menos de 15 min al mismo número.** El audio de la primera podría ir a la fila de la segunda. Es muy raro con el ciclo 3x3 (≥ 2 h entre intentos). El preflight cuenta los `ASTERISK_GAP` de WF9.
4. **Llamadas en curso en el momento del `apply`** siguen con el MixMonitor viejo. Las de más de 60 s las cubre el cron viejo hasta `finalize`; las más cortas de esos minutos no se suben (no hay backfill).
5. **Floor de Stringee.** Se guarda en el *static data* del workflow. Si reimportás el workflow borrando ese static data, el piso se corre a la nueva fecha.
6. **Clasificación de WF9.** Stringee con `recording_synced=1` puesto por WF9 ("no contestada / sin conversación") no se sube: se respeta la clasificación de WF9. VALIDATE las muestra como `OMIT`.
7. **Auto-rollback del script de Asterisk.** Restaura tus archivos, pero no puede "descargar" ediciones ajenas que ya estaban en disco. Por eso el `apply` se niega de entrada si detecta ediciones sin cargar.
8. **Telegram (preexistente, sin cambios).** Las dos ramas de Telegram mandan Mexico al **mismo** chat (`-1003616406932`), y "otros países" también dos veces al mismo chat (`-1003984044945`). Queda igual que en producción hasta que me digas el ruteo correcto.

---

## VALIDACIÓN (hecha en local)

- **Estructura: 28/28.**
  - JSON importable (importado en n8n y re-exportado idéntico).
  - IDs y conexiones válidos; toda referencia `$('nodo')` existe; todos los Code nodes compilan.
  - Credenciales originales y un solo webhook.
  - Sin `$'` en SQL, sin `continueOnFail` en nodos críticos y sin `pinData`.
- **n8n real + MariaDB: 48/48.** Stringee:
  - Varias subidas en el mismo ciclo, cada una con su audio (hash); multipart `file` con token.
  - Sin dueño → espera. PUT 500 / `hasRecording=false` → no marca ni entra al ledger, y se reintenta.
  - Corte de red, HTML 502 y 404 en el mismo ciclo no cortan las demás. 404 dos veces → deja de intentar.
  - Llamada con 2 archivos → sube el más viejo aunque haya fallado.
  - Mismo teléfono con 2 llamadas → cada audio a su followup.
  - Sin cruce de proveedor, filas `undefined` ignoradas, buzón subido.
  - Tope de 10 por ciclo; sin backfill al publicar; backfill aprobado acotado.
  - Telegram solo ≥ 60 s y una sola vez.

  Asterisk:
  - `attached` → `not_pending` al reenviar; fila tardía → 503 y después 200.
  - PUT 500, corte de red y HTML 502 → 503 y reintento. 404 → 503 y luego 200 `put_rejected`.
  - WAV vacío. Sin cruce de proveedor. Orden por UNIQUEID.
  - Sin UNIQUEID: fila reciente → OK, fila de hace 2 h → no se asigna.
  - Doble entrega simultánea → Telegram una vez.
- **End-to-end con Asterisk real: 22/22.**
  - `apply` sobre la réplica de tus 3 contextos, que se niega ante ediciones sin cargar y se autorrevierte si el reload carga algo extra.
  - Llamadas reales:
    - contestada con cuelgue del cliente;
    - contestada con cuelgue del agente;
    - contestada muy corta;
    - no contestada;
    - contestada con fila de WF2 tardía.
  - Recorrido: post-proceso → enqueue → cola → worker → WF10 real → PUT → `synced=1`.
  - 3 Stringee en el **mismo** ciclo programado.
  - VALIDATE: sin fallas en el caso sano, y detecta 6 fallas plantadas (incluidas el bug viejo del ledger y el de "una por ciclo").
  - `finalize` y `rollback` exacto, también con Asterisk caído.
- **Revisión independiente.** Otro agente revisó todo sin haberlo escrito. Sus hallazgos se corrigieron y están cubiertos por los tests de arriba. Quedan como decisiones tuyas los puntos de RIESGOS 1, 2 y 8, y el backlog.
