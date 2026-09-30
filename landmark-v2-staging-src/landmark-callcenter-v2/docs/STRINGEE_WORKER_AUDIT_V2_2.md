# STRINGEE_WORKER_AUDIT_V2_2.md
**Auditoría del worker Stringee (`/opt/stringee-ai-worker/`) — READ-ONLY**
21/09/2026

---

## 0. Conclusión

> ## `UNDETERMINED — PENDING_VERIFICATION`
>
> **El código del worker no fue auditado porque no está disponible en el entorno donde
> se construyó la fundación.** No se subió, no está en ninguno de los `.tar.gz` y no hay
> acceso SSH al VPS. No voy a afirmar `NO_CHANGE_REQUIRED` ni `MINIMAL_CHANGE_REQUIRED`
> sobre un código que no leí.
>
> Lo que sí está hecho: el **contrato observable** del worker, extraído nodo por nodo de
> los JSON v1 (§2), una **hipótesis fuerte** con evidencia (§3), el **script de auditoría
> read-only** listo para pegar por SSH (§5) y la **tabla de decisión** que convierte su
> salida en una de las dos conclusiones (§6).
>
> **Impacto en WF2 V2:** menor que en V2.1. La fundación V2.2 garantiza que un lead tiene
> a lo sumo una llamada en vuelo, así que **alcanza con que el worker reenvíe `lead_id`**
> para que WF9 identifique la llamada sin ambigüedad (§4). La evidencia indirecta dice que
> reenvía variables; falta confirmar cuáles.

---

## 1. Qué se pidió auditar y qué se pudo

| Punto | Estado | Fuente |
|---|---|---|
| `/call` y su payload | **observable** | WF2 v1 `📞 POST → Stringee Worker1` |
| respuesta de `/call` (`job_id`) | **observable** | WF2 v1 `🔍 Classify Result (Stringee)1` |
| dynamic_variables que llegan a ElevenLabs | **inferido** | WF9 v1 `⚙️ Parse Post-Call Data4` |
| conexión con Stringee | **no auditado** | código no disponible |
| conexión con ElevenLabs | **no auditado** | código no disponible |
| `conversation_id` | **no auditado** | el worker podría no conocerlo nunca |
| grabaciones | **observable** | WF10 v1 `🔄 Fetch & Convert Stringee Recordings2` |
| call-log | **observable** | WF14 v1 `📞 GET Call Log (Stringee Worker)` |
| propagación de campos nuevos | **no determinable sin código** | — |

---

## 2. Contrato observable (lo que n8n manda y lee)

### 2.1 `POST http://172.18.0.1:8091/call`

Body que envía WF2 v1 (claves exactas):

| Campo | Valor en v1 | Nota |
|---|---|---|
| `lead_id` | id de LeadStudio | |
| `phone` | E.164 **sin `+`** | el SIP lo manda **con** `+` |
| `full_name` | nombre | |
| `country` | `india` | nombre, no ISO |
| `language` | `hi` | |
| `is_followup` | bool | |
| `call_attempts` | número | |
| `from_number` | `917971730907` | caller id |
| `agent_id` | `agent_5701kramx…` | |

### 2.2 Respuesta de `/call`

WF2 v1 lee `statusCode`, `body.ok`, `body.job_id` **o** `body.jobId` (acepta ambos: el
nombre exacto no está fijado). `job_id` significa solo **DISPATCHED**: no dice nada del
resultado de la llamada.

### 2.3 `GET /recordings` y `GET /recordings/{filename}`

Lista: `filename, phone, size_bytes, timestamp_ms`. Detalle: `ok, audio_base64`.
**No hay `lead_id`, `job_id` ni `conversation_id`**: WF10 v1 correlaciona por teléfono.

### 2.4 `GET /call-log?limit&from_start_time`

`data.calls[]` con `id, to_number, answer_time, answer_duration, start_time, stop_time`.
**Tampoco hay `lead_id` ni `job_id`**: WF14 v1 correlaciona por teléfono.

---

## 3. Evidencia de que el worker reenvía variables a ElevenLabs

1. WF2 manda `phone` **sin `+`** al worker y **con `+`** a ElevenLabs SIP.
2. WF9 lee `dynamic_variables.phone` del post-call de ElevenLabs y trata como Stringee
   las llamadas cuyo teléfono **no empieza con `+`**.
3. Un `phone` sin `+` en las dynamic_variables de ElevenLabs solo puede venir del worker.

**⇒ El worker reenvía al menos `phone` como dynamic variable.** Es evidencia indirecta,
pero no hay otra explicación consistente con el código de WF9.

WF9 v1 lee además `lead_id`, `country`, `call_attempts` y `_source_sheet`. Si el worker
reenvía `phone` es plausible que reenvíe todo el body — o solo una lista fija. **Eso es
exactamente lo que no se puede saber sin leer el código.**

### 3.1 Hipótesis a verificar: el "WF9 no funciona para Stringee"

WF9 v1 descarta en silencio todo post-call sin `lead_id` (IF `Has lead + safe route?`).
**Si el worker no reenvía `lead_id`, todas las llamadas Stringee se pierden ahí**, sin
error visible. Eso encaja con el síntoma reportado al inicio del proyecto. No lo afirmo:
es la primera cosa que confirma o descarta el script de §5.

---

## 4. Qué necesita V2.2 del worker

| Campo | Para qué | ¿Imprescindible? |
|---|---|---|
| `lead_id` | WF9 → `find_inflight_job(lead_id)` → `call_job_id` | **SÍ** |
| `call_job_id` | correlación directa, sin depender del lead | no (mejora) |
| `route_key` | resolver la ruta sin consultar el job | no — sale del job |
| `country_iso` | idem | no — sale del job |
| `provider` | idem | no — sale del job |
| `attempt` | idem | no — sale del job |

**Por qué `lead_id` alcanza:** `wf_call_jobs.inflight_lead` es UNIQUE, así que un lead tiene
como máximo una llamada en vuelo. Con `lead_id` del post-call, WF9 obtiene ese único
job y de él saca `call_job_id`, `route_key`, `country_iso`, `provider`, `adapter_key` y
`attempt`. Probado en `test_ops_analytics_v2_2.py` (`c_inflight`) y en la base misma
(`test_migration_v2_2.py`: la base rechaza la segunda llamada en vuelo con error 1062).

Riesgo residual: si el post-call llega **después** de que el reconciliador marcó el job
`NEEDS_RECONCILIATION`, el job sigue en vuelo (ese estado cuenta como en vuelo), así que
la correlación funciona igual y el resultado lo resuelve.

---

## 5. Script de auditoría READ-ONLY

Pegar completo por SSH. No escribe nada, no reinicia nada y **enmascara secretos**
(cualquier valor de variable cuyo nombre contenga KEY, SECRET, TOKEN, PASS o AUTH).

```bash
W=/opt/stringee-ai-worker
echo "════ 1. estructura ════"
ls -la "$W"; find "$W" -maxdepth 2 -type f \( -name '*.js' -o -name '*.ts' -o -name '*.py' -o -name 'package.json' -o -name '*.env*' \) -not -path '*/node_modules/*' | head -50

echo "════ 2. variables de entorno (valores enmascarados) ════"
for f in "$W"/.env "$W"/.env.*; do [ -f "$f" ] && { echo "--- $f"; sed -E 's/^([A-Za-z0-9_]*(KEY|SECRET|TOKEN|PASS|AUTH)[A-Za-z0-9_]*)=.*/\1=***MASKED***/I' "$f"; }; done

echo "════ 3. endpoint /call ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "['\"]/call['\"]" "$W" --exclude-dir=node_modules | head -20

echo "════ 4. qué campos del body usa ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "req\.body|request\.json|body\.(lead_id|phone|from_number|agent_id|country|call_attempts|is_followup|route_key|call_job_id)" "$W" --exclude-dir=node_modules | head -40

echo "════ 5. dynamic variables hacia ElevenLabs (LA PREGUNTA CLAVE) ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "dynamic_variables|dynamicVariables|conversation_initiation_client_data" -A 15 "$W" --exclude-dir=node_modules | head -80

echo "════ 6. job_id y conversation_id ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "job_?[iI]d|conversation_?[iI]d" "$W" --exclude-dir=node_modules | head -30

echo "════ 7. ¿el body se reenvía entero? ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "\.\.\.req\.body|\*\*body|Object\.assign\([^)]*body" "$W" --exclude-dir=node_modules | head

echo "════ 8. from_number / agent_id: ¿del request o del .env? ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "from_number|agent_id|AGENT_ID|FROM_NUMBER" "$W" --exclude-dir=node_modules | head -20

echo "════ 9. recordings y call-log ════"
grep -rn --include=*.js --include=*.ts --include=*.py -E "['\"]/recordings|['\"]/call-log" -A 5 "$W" --exclude-dir=node_modules | head -40

echo "════ 10. proceso en ejecución ════"
(pm2 ls 2>/dev/null || systemctl list-units --type=service 2>/dev/null | grep -i stringee || docker ps --format '{{.Names}} {{.Image}}' | grep -i stringee) | head
```

Y una verificación **en producción, de solo lectura**, de la hipótesis §3.1:

```bash
# ¿los post-call de Stringee llegan con lead_id? Últimas 500 llamadas Stringee CONTESTADAS
# (columnas reales de WF14: answered, duration_secs, lead_id; start_time es epoch crudo)
# frente a los followups que WF9 guardó con provider='stringee'.
mysql -u panel_rw -p asterisk -e "
SELECT COUNT(*)                                  AS contestadas,
       SUM(s.lead_id IS NULL)                    AS sin_lead_en_crm,
       SUM(s.lead_id IS NOT NULL AND f.lead_id IS NULL) AS con_lead_sin_followup_wf9
FROM (SELECT call_id, lead_id FROM stringee_calls
      WHERE answered = 1 ORDER BY start_time DESC LIMIT 500) s
LEFT JOIN (SELECT DISTINCT lead_id FROM wf_call_followups WHERE provider = 'stringee') f
       ON f.lead_id = s.lead_id;"
# Si con_lead_sin_followup_wf9 ≈ contestadas → WF9 descarta los post-call de Stringee (§3.1).
# En n8n: Executions de WF9 → buscar el log "Post-call skipped safely" con lead_id vacío.
```

> `stringee_calls.lead_id` lo completa WF14 v1 cruzando por `RIGHT(phone,10)` contra `crm_leads`.
> Sirve como diagnóstico del sistema v1; **no es el mecanismo de V2**.

---

## 6. Tabla de decisión

| Resultado del §5, sección 5 | Conclusión | Qué hacer |
|---|---|---|
| reenvía el **body completo** a dynamic_variables | **NO_CHANGE_REQUIRED** | WF2 V2 agrega `call_job_id`, `route_key`, `country_iso`, `provider`, `attempt` al body y llegan solos |
| reenvía una **lista fija que incluye `lead_id`** | **NO_CHANGE_REQUIRED** para operar | WF9 correlaciona por `lead_id` → job en vuelo. Agregar `call_job_id` al worker queda como mejora opcional |
| reenvía una lista fija **sin `lead_id`** | **MINIMAL_CHANGE_REQUIRED** | agregar `lead_id` (y `call_job_id`) a las dynamic_variables. Cambio de pocas líneas, requiere tu aprobación. Además explica el bug §3.1 |
| no reenvía variables | **MINIMAL_CHANGE_REQUIRED** | idem, y revisar cómo WF9 v1 funciona hoy |

En los cuatro casos, el cambio al worker **no se aplica en esta fase**.

---

## 7. Otras preguntas que el script responde

| Pregunta | Por qué importa |
|---|---|
| ¿`from_number` y `agent_id` salen del request o del `.env`? | si salen del `.env`, `caller_id` y `agent_id` de la ruta en el panel **no tienen efecto** para Stringee |
| ¿el worker conoce el `conversation_id`? | si lo devuelve en `/call`, WF2 puede guardarlo al despachar |
| ¿`job_id` o `jobId`? | fijar un nombre en el adapter |
| ¿tiene auth? | hoy `/call` no manda credencial desde n8n |
| ¿expone `job_id` en `/call-log` o `/recordings`? | permitiría reconciliar por id en vez de por teléfono |
