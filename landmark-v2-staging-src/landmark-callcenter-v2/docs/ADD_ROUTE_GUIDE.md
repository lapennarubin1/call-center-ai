# ADD_ROUTE_GUIDE.md
**La ruta es la unidad que llama. Crearla, validarla, encenderla, archivarla.**
Fase BUILD · 21/09/2026

---

## 0. Qué es una ruta

Un **país** + un **proveedor** + su configuración de llamado.

```
IN_PROVEEDOR1   India  ×  PROVEEDOR1   capacidad 6
IN_STRINGEE     India  ×  STRINGEE     capacidad 1
NP_PROVEEDOR1   Nepal  ×  PROVEEDOR1   capacidad 6
```

Un país puede tener **varias rutas activas a la vez**, incluso del mismo
proveedor. No hay ninguna restricción de unicidad país → proveedor.

---

## 1. Convención del `route_key`

```
<ISO2>_<PROVEEDOR>              IN_PROVEEDOR1 · NP_PROVEEDOR1 · IN_STRINGEE
<ISO2>_<PROVEEDOR>_<VARIANTE>   IN_PROVEEDOR1_NOCHE
```

Mayúsculas, dígitos y guion bajo; empieza por el ISO del país.

> **El `route_key` es para siempre.** Viaja en `dynamic_variables`, queda en cada
> fila de `wf_call_jobs`, en cada evento y en cada grabación. Una ruta archivada
> **no libera su clave**: reutilizarla haría que datos históricos apunten a una
> configuración que no es la suya.

---

## 2. Crear la ruta

`Panel → Countries & Routes → Routes → New`

| campo | |
|---|---|
| `route_key` | `MX_PROVEEDOR1` |
| `iso` | el país (debe existir) |
| `provider_id` | el proveedor (debe existir) |
| `priority` | orden de reparto dentro del país (menor = primero) |
| `capacity_default` | llamadas por ciclo si no hay franja vigente |
| `elevenlabs_agent_id` | el agente |
| `elevenlabs_phone_number_id` | para `ELEVENLABS_SIP` |
| `caller_id` | para `STRINGEE_WORKER` |
| `followup_policy_id` | la política |
| `recording_*` | ver §5 |
| `enabled` | **NO** al crearla |

Se puede guardar **incompleta**: mientras esté apagada, no pasa nada.

---

## 3. READY: qué se valida

Antes de dejar activar, `validate_route()` verifica:

| | |
|---|---|
| `route_key` | formato válido |
| país | existe, no archivado, con huso válido |
| proveedor | existe, con un `adapter_key` **del catálogo** |
| `capacity_default` | > 0 |
| franjas | horas válidas, sin solapamiento, inicio ≠ fin |
| política | asignada y **no archivada**, y su JSON válido |
| grabaciones | `recording_min_secs` entre 0 y 3600; si van a Telegram, al menos un chat `recording` |
| campos del adapter | `ELEVENLABS_SIP`: agente + phone_number_id · `STRINGEE_WORKER`: agente + caller_id |

Si falta algo, el panel dice **exactamente qué campo** y por qué. Activar una
ruta NOT READY **falla**: no hay forma de dejar llamando a una ruta incompleta.

### Revalidación en caliente

Una ruta encendida que queda inválida más tarde —alguien borró el último chat de
Telegram, archivó la política, apagó el proveedor, o hizo un `UPDATE` manual en
la base— devuelve `calling_now: false` en el ciclo siguiente. **Fail-closed.**

---

## 4. Capacidad y horario

### Sin franjas

Llama 24/7 con `capacity_default`. Es el comportamiento de v1.

### Con franjas

```
mon-fri  09:00–14:00   5
mon-fri  14:00–15:00   8
mon-fri  15:00–20:00   5
```

- **hora local del país**, siempre
- fuera de todas las franjas: no llama (`OUTSIDE_SCHEDULE`)
- `capacity = 0` apaga un tramo sin borrar la franja
- `20:00 → 02:00` cruza medianoche y vale hasta las 2 del día siguiente
- dos franjas solapadas se rechazan al guardar
- a través de un cambio de horario, la franja sigue el **reloj local**

### Elegir la capacidad

`capacity` = **cuántas llamadas simultáneas aguanta el canal**, no cuántos leads
hay.

| proveedor | límite real |
|---|---|
| SIP por Asterisk | los canales del trunk |
| Stringee | `MAX_CONCURRENT_CALLS` del worker (**5** por defecto) |

> Si la capacidad de la ruta supera la del worker, el exceso vuelve como
> `429 worker_capacity_reached` → `RELEASED` → reintento técnico. No se pierde
> ningún lead, pero se gasta ciclo. **La capacidad de la ruta debe ser ≤ la del
> proveedor.**

---

## 5. Grabaciones

| campo | |
|---|---|
| `recording_enabled` | si WF10 procesa esta ruta |
| `recording_min_secs` | mínimo para subirla (60 por defecto) |
| `recording_upload_crm` | subirla a LeadStudio |
| `recording_telegram` | mandarla a Telegram |
| `recording_source` | `STRINGEE_WORKER` (worker local) o `ELEVENLABS_API` |
| `recording_lookback_hours` | ventana de recogida (48 h) |

Las que quedan por debajo del mínimo **se cuentan** (`RECORDING_SKIPPED_SHORT`),
no desaparecen en silencio.

---

## 6. Encender

```
1. verificar que dice READY
2. la RUTA      → ON
3. el PROVEEDOR → ON
4. el PAÍS      → ON
```

Y confirmar:

```bash
curl -s -H "X-Service-Token: <token>" \
  http://172.18.0.1:8080/api/routes/active | grep -A8 MX_PROVEEDOR1
```

Debe traer `calling_now: true`, `capacity_now`, `blocked_by: []`.

---

## 7. Probar sin molestar a nadie

1. `capacity_default = 1`
2. disparar un ciclo a mano: `POST /webhook/dispatch-now-v2`
3. seguir la llamada:

```sql
SELECT call_job_id, lead_id, route_key, provider, adapter_key, attempt,
       state, result, duration_seconds, followup_id, error_code
  FROM wf_call_jobs WHERE route_key = 'MX_PROVEEDOR1'
 ORDER BY created_at DESC LIMIT 5;
```

4. verificar el follow-up en el CRM y que la nota esté **en inglés**
5. subir la capacidad de a poco: 1 → 2 → 5 → la real

---

## 8. Varias rutas en el mismo país

```
India:  IN_PROVEEDOR1 (priority 10, cap 5)
        IN_STRINGEE   (priority 20, cap 3)
        demanda del país = 8
```

WF2 pide **una sola cola** de 8 leads y los reparte por `priority`: los primeros
5 a `IN_PROVEEDOR1`, los 3 siguientes a `IN_STRINGEE`.

El claim atómico garantiza que ninguna ruta llame al mismo lead, y el
`UNIQUE(inflight_lead)` que un lead no tenga dos llamadas en vuelo.

### Para qué sirve tener varias

- **capacidad**: sumar canales de proveedores distintos
- **costo**: priorizar el barato y desbordar al caro
- **continuidad**: si uno se cae, se apaga su ruta y el otro sigue
- **comparación**: `answer_rate` por ruta, con los mismos leads y la misma política

---

## 9. Apagar o archivar

| | qué hace | cuándo |
|---|---|---|
| **Disable** | deja de llamar; se puede volver a encender | pausa operativa |
| **Archive** | deja de llamar y sale de las listas | la ruta no se usa más |

**Nunca se borra una ruta que ya llamó.** Archivada:

- no llama
- **su `route_key` sigue resolviendo** por `/api/routes/by-key/` — un post-call o
  una grabación que llega 6 horas después necesita su política y su config
- **su clave no se puede reutilizar**

`Unarchive` la devuelve a `DISABLED` (no a `ACTIVE`): hay que encenderla a mano.

---

## 10. Auditoría

Cada alta, edición, encendido, apagado y archivado queda en `route_audit`, con
campo, valor anterior, valor nuevo, usuario y fecha.

```sql
SELECT changed_at, actor, action, field, old_value, new_value
  FROM route_audit WHERE route_key = 'MX_PROVEEDOR1'
 ORDER BY changed_at DESC LIMIT 20;
```

---

## 11. Problemas frecuentes

| síntoma | causa | solución |
|---|---|---|
| **NOT READY** y no se ve por qué | falta un campo del adapter | el panel lista los faltantes |
| READY pero `calling_now: false` | el país o el proveedor están apagados | ver `blocked_by` |
| `OUTSIDE_SCHEDULE` a toda hora | las franjas no cubren el momento actual, o el huso está mal | revisar huso y franjas |
| `ZERO_CAPACITY` | la franja vigente tiene capacidad 0 | subirla o borrar la franja |
| muchos `RELEASED` | la capacidad supera la del proveedor | bajarla |
| no toma leads | la cola del país está vacía o todos están en vuelo | `SELECT status, COUNT(*) FROM crm_leads WHERE country=…` |
