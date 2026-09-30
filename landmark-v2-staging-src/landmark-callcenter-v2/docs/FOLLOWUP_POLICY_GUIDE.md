# FOLLOWUP_POLICY_GUIDE.md
**Cómo funciona, cómo se cambia y cómo se prueba una política de seguimiento.**
Fase BUILD · 21/09/2026

---

## 1. Una sola política, un solo motor

La decisión de *cuándo se vuelve a llamar a un lead* se toma en **un solo lugar**:
`TEMPLATE_FOLLOWUP_ENGINE_V2`.

- el proveedor **no** decide
- el país **no** decide
- WF2 y WF9 **no** deciden: normalizan el resultado y llaman al motor

En v1 la decisión estaba repartida: WF2 calculaba `scheduleNextAt` para Asterisk
con aritmética modular (`posInCycle`, `cycle`, `% 3`), y WF9 calculaba otra cosa
para Stringee. Eran dos políticas distintas que nadie podía leer juntas.

## 2. Forma de una política

Se guarda en `followup_policies.policy_json` y se asigna a cada ruta.

```json
{
  "policy_version": 1,
  "no_answer_sip_codes": ["603", "408", "486"],
  "result_aliases": { "BUSY": "NO_ANSWER", "VOICEMAIL": "NO_ANSWER" },
  "rules": [
    { "result": "NO_ANSWER", "attempt": 1, "action": "RETRY", "delay": "+2h" },
    { "result": "NO_ANSWER", "attempt": 9, "action": "CLOSE", "delay": null },
    { "result": "NO_ANSWER", "attempt": "*", "action": "CLOSE", "delay": null },
    { "result": "ANSWERED",  "attempt": "*", "action": "COMPLETE", "delay": null }
  ],
  "callback_default": "+24h",
  "unmatched_action": "NONE"
}
```

### Resolución de una regla, en este orden

1. `effective_result = result_aliases[result]` o el `result` tal cual
2. regla con `(effective_result, attempt exacto)`
3. regla con `(effective_result, "*")`
4. `unmatched_action` (por defecto `NONE`; solo admite `NONE` o `CLOSE`)

**Sin módulo. Sin `% 3`. Sin ciclos calculados.** Una regla por intento, escrita.

### Acciones

| acción | qué hace |
|---|---|
| `RETRY` | programa el próximo intento en `now + delay` |
| `CLOSE` | no hay más intentos |
| `COMPLETE` | contacto logrado, sin reintento |
| `CALLBACK` | programa en `callback_at` si es futuro; si no, `now + callback_default` |
| `NONE` | registra el follow-up sin programar nada |

### Unidades de `delay`

| | |
|---|---|
| `+Nm` | N minutos (duración absoluta) |
| `+Nh` | N horas (duración absoluta) |
| `+Nd` | N días de calendario, **misma hora de reloj local** del país (respeta cambio de horario) |
| `+Nbd` | N días **hábiles** (lun-vie) en el huso del país, misma hora local |

> **Feriados: no se contemplan.** `+2bd` salta sábados y domingos, no el Diwali.
> Está en `PENDING_VERIFICATION.md` (PV-6) por si hace falta.

## 3. La política que se instala: `STANDARD_CALL_RETRY`

Es la de WF2 v1, **regla por regla**, sin el módulo.

| intento | `NO_ANSWER` | siguiente llamada |
|---|---|---|
| 1 | `RETRY +2h` | 2 horas después |
| 2 | `RETRY +3h` | 3 horas después |
| 3 | `RETRY +2bd` | 2 días hábiles, misma hora local |
| 4 | `RETRY +2h` | |
| 5 | `RETRY +3h` | |
| 6 | `RETRY +3bd` | 3 días hábiles |
| 7 | `RETRY +2h` | |
| 8 | `RETRY +3h` | |
| 9 | **`CLOSE`** | no se llama más |
| `*` (>= 10) | `CLOSE` | red de seguridad |

Otros resultados:

| resultado | acción |
|---|---|
| `BUSY` | alias → `NO_ANSWER` |
| `VOICEMAIL` | alias → `NO_ANSWER` |
| `ANSWERED` | `COMPLETE` |
| `CALLBACK` | `CALLBACK` (por defecto +24 h) |
| `WRONG_NUMBER` | `CLOSE` |
| `DNC` | `CLOSE` |
| `FAILED` sin regla | `NONE` — ver §6 |
| `UNKNOWN` sin regla | `NONE` — ver §6 |

> **Una política de 9 reglas NO programa 9 llamadas.** Se hace UNA llamada; si
> resulta `NO_ANSWER` se programa la siguiente; cuando llega ese momento, se hace
> el siguiente intento.

### Ejemplo verificado

Viernes 18/09/2026, 10:00 en `Asia/Kolkata` (04:30 UTC), intento 3, `NO_ANSWER`:

```
+2bd  →  martes 22/09 10:00 local  =  2026-09-22T04:30:00Z
```

Salta sábado y domingo, y conserva las 10:00 locales. Lo comprueba
`tests/test_engine_parity_v2.py`.

## 4. La misma política para todos los proveedores

```
IN_PROVEEDOR1  →  STANDARD_CALL_RETRY
IN_STRINGEE    →  STANDARD_CALL_RETRY
NP_PROVEEDOR1  →  STANDARD_CALL_RETRY
```

La política es **de la ruta**, no del proveedor. Dos rutas del mismo país pueden
tener políticas distintas si el negocio lo pide (p. ej. una ruta de reactivación
más agresiva), y eso es configuración en el panel.

## 5. Códigos SIP: es política, no del adapter

El adapter reporta el código **crudo**:

```json
{ "result": "FAILED", "sip_code": "603" }
```

El motor lo reclasifica:

> si `sip_code ∈ policy.no_answer_sip_codes` y `result ∈ {FAILED, UNKNOWN}`
> ⇒ el resultado efectivo es `NO_ANSWER`

Un código **no listado** (404, 484…) queda `FAILED`: sabemos que falló, pero no
que no contestaron. Con `unmatched_action: NONE` eso **no consume ni programa**
nada; queda registrado y visible.

Cambiar qué códigos significan "no contestó" es editar
`no_answer_sip_codes` **en el panel**. Cero código.

## 6. Decisión pendiente D-1: `FAILED` y `UNKNOWN`

v1 se contradecía: WF2 trataba `FAILED` sin reintento y WF9 lo mapeaba a
`NO_ANSWER` (y por tanto reintentaba). V2 **no elige por vos**: los deja en
`NONE` (registrar y no hacer nada) y lo marca como decisión de negocio.

Para que `FAILED` reintente como un no-contesta, agregar a la política:

```json
{ "result": "FAILED", "attempt": "*", "action": "RETRY", "delay": "+3h" }
```

Se hace desde el panel, sin tocar n8n. Ver `PENDING_VERIFICATION.md` (D-1).

## 7. Qué NUNCA llega al motor

| | por qué |
|---|---|
| `DISPATCHED` | no es un resultado final. El motor lo **rechaza** con `VALIDATION_ERROR` |
| `AUTH_ERROR`, `CONFIG_ERROR`, 401/403 | son errores **técnicos**: el job va a `RELEASED`, no consumen intento de negocio |
| worker lleno (429) | idem |
| `UNKNOWN` de **despacho** | la llamada pudo salir: `NEEDS_RECONCILIATION`, nunca re-despacho |

Un error técnico **jamás** se registra como `NO_ANSWER`. Esa confusión en v1
hacía que una caída del proveedor consumiera los 9 intentos de un lead en un día.

## 8. Crear o cambiar una política

1. **Panel → Countries & Routes → Policies**
2. Editar el JSON o crear una clave nueva (`MAYÚSCULAS_CON_GUION_BAJO`)
3. Guardar: el panel **valida** la forma (resultados, acciones, delays, reglas
   duplicadas, alias circulares) y rechaza lo que no cumpla
4. Asignarla a las rutas que correspondan
5. **Probarla antes de activar**: ver §9

Las políticas **no se borran**: se archivan. Una ruta con la política archivada
deja de estar `READY` y, por lo tanto, deja de llamar (fail-closed).

## 9. Cómo probar una política antes de activarla

```python
import sys; sys.path.insert(0, 'panel/app')
import followup_engine as fe, json
from datetime import datetime, timezone

policy = json.load(open('mi_politica.json'))
fe.validate_policy(policy)                    # lanza PolicyError si está mal

now = datetime(2026, 9, 18, 4, 30, tzinfo=timezone.utc)   # viernes 10:00 IST
for attempt in range(1, 11):
    d = fe.resolve(policy, attempt, 'NO_ANSWER', now_utc=now, tz_name='Asia/Kolkata')
    print(attempt, d['action'], d['delay'], d['schedule_next_at'])
```

`followup_engine.py` es el **mismo** motor que corre en n8n: la paridad entre la
versión Python y la JavaScript se verifica con 19.320 casos en
`tests/test_engine_parity_v2.py`, incluidos cambios de horario reales.

## 10. Idempotencia

El motor crea **un** follow-up por llamada:

- clave: `conversation_id` cuando existe; si no, `call_job_id`
- protección: `wf_conversation_ledger` (PK) + `wf_call_jobs.result IS NULL`
- perder el claim **no es un error**: devuelve `{skipped: true, reason: ALREADY_CLAIMED}`

Que el webhook y el polling vean la misma conversación es el camino **normal**.
