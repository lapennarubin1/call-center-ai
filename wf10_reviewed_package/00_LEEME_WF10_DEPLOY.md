# WF10 — pasos de deploy (copiar y pegar por SSH en el VPS)

Orden obligatorio: **1 auditoría (solo lectura) → 2 Asterisk → 3 WF10 en n8n → 4 validar con llamadas reales nuevas → 5 retirar el cron viejo**.
Nada de esto hace backfill, borra filas, toca WF2/WF9, `wf_call_events` ni las 891 filas `undefined`.

Archivos:

| Archivo | Qué es |
|---|---|
| `WF10_FINAL_PRODUCTION.json` | WF10 completo (mismo id `TW1CHksqyf66MjQz`, mismo webhook `POST /webhook/send-recording`, mismas credenciales) |
| `ASTERISK_WF10_FIX.sh` | `check` / `apply` / `status` / `finalize` / `rollback` del productor durable de Asterisk |
| `PREFLIGHT_WF10_READONLY.sh` | Auditoría solo lectura (antes del deploy) |
| `VALIDATE_WF10_AFTER_DEPLOY.sh` | Validación solo lectura (después del deploy, con llamadas nuevas) |
| `WF10_ORIGINAL_para_rollback.json` | El WF10 que me mandaste, sin cambios (para el rollback) |
| `01_INFORME_WF10.md` / `02_ROLLBACK_WF10.md` | Causas, cambios, riesgos, pruebas / rollback exacto |

---

## PASO 0 — Subir los archivos (desde tu computadora)

```bash
ssh root@TU_VPS 'mkdir -p /root/wf10'
scp WF10_FINAL_PRODUCTION.json WF10_ORIGINAL_para_rollback.json ASTERISK_WF10_FIX.sh PREFLIGHT_WF10_READONLY.sh VALIDATE_WF10_AFTER_DEPLOY.sh root@TU_VPS:/root/wf10/
```

## PASO 1 — Auditoría solo lectura (NO cambia nada)

```bash
cd /root/wf10 && chmod +x *.sh
bash PREFLIGHT_WF10_READONLY.sh 180
./ASTERISK_WF10_FIX.sh check
```

`check` es solo lectura: muestra línea por línea qué cambiaría (`antes:` / `después:`). Si termina en `❌ CHECK con problemas`, no sigas.

**Frenos: mirá esto antes de seguir** (si algo no cierra, mandame `/root/wf10_preflight_*.txt`, que ya sale con las contraseñas enmascaradas):

1. **§2 del worker.** WF10 empareja por UNIQUEID. Buscá en `¿qué manda el worker en el POST?` que viaje `uniqueid`, o `filename` = `test-<UNIQUEID>.wav`.
   - Si manda `<tel>_<fecha>.wav`, igual funciona, pero con un emparejamiento por franja horaria menos exacto. En ese caso mandame el worker: agregarle el UNIQUEID es un cambio de 1 línea.
   - En `¿cómo trata el worker la respuesta?` tiene que reintentar ante un **503** y dejar el archivo en la cola.
   - Su timeout HTTP tiene que ser **≥ 90 s**, porque ahora WF10 responde después de subir.
2. **§11 Tiempos.** Las filas `ASTERISK` y `STRINGEE` tienen que decir `dentro de [...] = N/N`. Si aparece `⚠️ fuera de la ventana`, no publiques WF10 todavía y mandámelo.
3. **§9 Stringee.** `fuera del ledger ... followup PENDIENTE=N` son contestadas anteriores que **no** se van a subir (no hay backfill). Si las querés en el CRM, ver "Opcional" al final.

## PASO 2 — Asterisk: productor durable (backup + dialplan reload, sin reiniciar)

```bash
cd /root/wf10
./ASTERISK_WF10_FIX.sh apply
./ASTERISK_WF10_FIX.sh status
```

- `apply` hace backup en `/root/wf10-fix-backups/<fecha>/` e instala `/usr/local/bin/wf10-mixmon-post.sh`. Cambia **solo** la línea `MixMonitor(...)` de cada contexto WF10 y ejecuta `dialplan reload`, sin restart, así que las llamadas en curso no se cortan.
- Después verifica que el dialplan cargado sea idéntico salvo esas líneas. Si no lo es, se revierte solo.
- Se niega a correr si hay ediciones del dialplan en disco que todavía no están cargadas, porque el reload también las activaría. Revisalas primero o usá `FORCE=1`.
- El cron viejo **sigue activo** hasta el paso 5. No duplica: si llega dos veces la misma llamada, WF10 sube una sola vez y Telegram sale una sola vez.

## PASO 3 — Publicar WF10 en el MISMO workflow (n8n)

1. En n8n abrí **WF10** (id `TW1CHksqyf66MjQz`). Dejalo **activo**.
2. `⋯` → **Download**, como backup extra.
3. **Ctrl+A** → **Delete**. **No guardes todavía**: producción sigue con la versión vieja hasta que guardes.
4. `⋯` → **Import from File…** → `WF10_FINAL_PRODUCTION.json`.
5. Revisá que los nodos MySQL digan "MySQL account" y los de Telegram "Telegram account", sin rojo, y que el Webhook diga `POST /send-recording`.
6. **Ctrl+S (Save)**. Queda activo con la versión nueva, en el mismo workflow y la misma URL.
7. En la lista de workflows, filtrá los activos y buscá "WF10": tiene que haber **uno solo**.

> No lo importes por CLI (`n8n import:workflow`) con n8n corriendo: deja la versión vieja cargada en memoria hasta reactivar. Usá la interfaz.

## PASO 4 — Validar con llamadas reales NUEVAS

Hacé llamadas nuevas:

- **Asterisk**: al menos 3 contestadas, idealmente de Mexico, Nepal e India, y 1 no contestada.
- **Stringee**: 2 o 3 contestadas **dentro de los mismos ~5 minutos**, para probar varias en el mismo ciclo.

Esperá unos 10 minutos. Clave de API opcional: n8n → Settings → n8n API → Create API key (borrala al terminar).

```bash
cd /root/wf10
N8N_URL=https://landmarket-n8n.dhsoig.easypanel.host N8N_API_KEY='PEGAR_CLAVE' bash VALIDATE_WF10_AFTER_DEPLOY.sh
# sin clave también funciona:  bash VALIDATE_WF10_AFTER_DEPLOY.sh
```

Éxito = `✅ Sin fallas` en el RESUMEN, con:

- **§2 Asterisk**: cada contestada nueva en `OK` (WAV → cola/done → followup → `synced=1`).
- **§3 Stringee**: las nuevas en `OK` y `[PASS] varias grabaciones en el mismo ciclo`, más `[PASS] n8n: ciclos con 2+ PUT exitosos` si usaste la clave.

Si sale `PEND`, faltan llamadas o tiempo: volvé a correrlo. Si sale `FAIL`, mandame la salida (`/root/wf10_validate_*.txt`).

## PASO 5 — Recién ahora: retirar el cron viejo (un solo productor)

```bash
cd /root/wf10
./ASTERISK_WF10_FIX.sh finalize
crontab -l | grep send_recordings
```

La línea queda **comentada, no borrada**. `finalize` se niega si el mecanismo nuevo encoló menos de 3 grabaciones. Corré `VALIDATE` otra vez después de unas llamadas más.

---

## Opcional (solo con tu aprobación): subir el backlog Stringee anterior

Por defecto WF10 **no** sube grabaciones de antes de su primer ciclo (30 min de margen). Para subir las pendientes anteriores, que se ven en PREFLIGHT §9, como máximo de 10 días atrás:

1. En el nodo `🔄 Fetch & Convert Stringee Recordings` cambiá `const BACKLOG_FROM = '';` por ejemplo a `const BACKLOG_FROM = '2026-09-25T00:00:00Z';` y guardá.
2. Sube 10 por ciclo, cada una a su followup dueño, y solo las que tienen dueño pendiente. Las que el WF10 viejo metió en el ledger sin PUT (§9 "registradas ... SIN PUT confirmado") **no** entran. Eso requiere otra decisión.
3. Cuando termine, volvé a `''`.
