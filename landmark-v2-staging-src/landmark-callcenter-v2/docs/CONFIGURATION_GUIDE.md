# CONFIGURATION_GUIDE.md
**Todo lo que se configura, dónde se configura y qué efecto tiene.**
Fase BUILD · 21/09/2026

---

## 0. La regla

> **Ningún literal de negocio vive en un nodo de n8n.** País, prefijo, agente,
> chat de Telegram, capacidad, horario, endpoint, política, mínimo de
> grabación: todo sale del panel.

Hay un test que recorre los siete JSON y falla si aparece un `agent_id`, un
`phnum_`, un chat de Telegram o un mapa `{india: …}`.

---

## 1. Mapa de la configuración

```
countries              el país: prefijo, huso, idioma, interruptor
  └─ country_tool_configs   CREATE_ACCOUNT · CREATE_PAYMENT_LINK · CALLBACK
voice_providers        el proveedor COMERCIAL + su adapter_key
  └─ call_routes            país × proveedor — la unidad que llama
       ├─ route_capacity_windows   franjas horarias con capacidad
       └─ route_telegram_targets   destinos por propósito
followup_policies      reglas de reintento, reutilizables
wf_settings            umbrales operativos del suite
```

---

## 2. País — `Panel → Countries & Routes → Countries`

| campo | qué es | ejemplo |
|---|---|---|
| `iso` | ISO-2, **la identidad** | `IN` |
| `country_name` | nombre visible | `India` |
| `dial_prefix` | prefijo E.164 | `+91` |
| `national_number_len` | largo nacional, para normalizar | `10` |
| `timezone` | **huso IANA** — de acá salen las franjas y los días hábiles | `Asia/Kolkata` |
| `language` | idioma por defecto del agente | `hi` |
| `enabled` | **interruptor de PAÍS** | |
| `archived_at` | archivado (no se borra) | |

**El huso es del país, no de la ruta.** Dos rutas de India comparten
`Asia/Kolkata`: una llamada a las 10 de la mañana lo es para las dos.

Un país nace **apagado** (fail-closed): se configura todo y se enciende al final.

---

## 3. Proveedor — `Panel → Countries & Routes → Providers`

| campo | qué es |
|---|---|
| `code` | identidad del proveedor **comercial** (`proveedor1`, `stringee`) |
| `display_name` | nombre visible |
| `adapter_key` | **cómo se le habla**: `ELEVENLABS_SIP`, `STRINGEE_WORKER` |
| `endpoint` | URL base, si el adapter la necesita |
| `account_ref` | referencia de cuenta/contrato |
| `enabled` | **interruptor de PROVEEDOR** |

> **`code` ≠ `adapter_key`.** El primero es con quién se tiene contrato; el
> segundo, qué código sabe hablarle. Un `PROVEEDOR2` con
> `adapter_key = ELEVENLABS_SIP` **no requiere tocar n8n**.

El catálogo de adapters vive en `routes_config.ADAPTERS`, no en la base:
`adapter_key` es `VARCHAR` y no `ENUM`, pero un adapter que no esté en el
catálogo **se puede guardar apagado y no se puede activar** — no hay código en
WF2 que sepa llamarlo.

---

## 4. Ruta — `Panel → Countries & Routes → Routes`

La ruta es **la unidad que llama**: un país con un proveedor.

| campo | qué es | quién lo usa |
|---|---|---|
| `route_key` | identidad, `XX_PROVEEDOR` | todo el suite |
| `iso` / `provider_id` | qué país y con qué proveedor | |
| `enabled` | **interruptor de RUTA** | |
| `archived_at` | archivada: no llama, pero **sigue resolviendo** | WF9, WF10, motor |
| `priority` | orden de reparto de leads dentro del país | WF2 |
| `capacity_default` | llamadas por ciclo si no hay franja vigente | WF2 |
| `elevenlabs_agent_id` | el agente de ElevenLabs | adapters |
| `elevenlabs_phone_number_id` | el número de ElevenLabs | `ELEVENLABS_SIP` |
| `caller_id` | `from_number` | `STRINGEE_WORKER` |
| `followup_policy_id` | qué política aplica | motor |
| `recording_enabled` | si se recogen grabaciones | WF10 |
| `recording_min_secs` | duración mínima para subirla | WF10 |
| `recording_upload_crm` | si se sube al CRM | WF10 |
| `recording_telegram` | si se manda a Telegram | WF10 |
| `recording_source` | `STRINGEE_WORKER` o `ELEVENLABS_API` | WF10 |
| `recording_lookback_hours` | ventana de recogida (48 h) | WF10 |

### READY vs ON

Son **cosas distintas**:

- **READY** = la configuración está completa. Lo decide `validate_route()`.
- **ON** = decisión operativa de empezar a llamar.

Se puede guardar una ruta incompleta mientras está apagada. **Activarla** exige
READY: si falta algo, el panel dice exactamente qué.

Qué exige cada adapter:

| `adapter_key` | campos obligatorios de la ruta |
|---|---|
| `ELEVENLABS_SIP` | `elevenlabs_agent_id`, `elevenlabs_phone_number_id` |
| `STRINGEE_WORKER` | `elevenlabs_agent_id`, `caller_id` (+ `endpoint` en el proveedor) |

Además, siempre: `capacity_default > 0`, una política de follow-up **no
archivada**, franjas sin solapamiento, y —si las grabaciones van a Telegram— al
menos un chat de propósito `recording`.

### Revalidación en caliente

Una ruta encendida que queda inválida por una edición posterior (o por un
`UPDATE` manual en la base) devuelve `calling_now = false`. **Fail-closed:**
ninguna edición puede dejar llamando a una ruta mal configurada.

### Archivar, nunca borrar

Una ruta que **ya llamó** no se borra: `enabled = 0` + `archived_at`.

- deja de llamar
- su `route_key` **sigue resolviendo** por `/api/routes/by-key/` — un post-call o
  una grabación que llega horas después necesita su política y su config
- **su `route_key` no se puede reutilizar** para otra cosa

---

## 5. Franjas horarias — `Routes → la ruta → Capacity windows`

| campo | |
|---|---|
| `day_mask` | `mon,tue,wed,thu,fri` |
| `start_local` / `end_local` | **hora local del país** |
| `capacity` | llamadas por ciclo en esa franja |

```
09:00–14:00 → 5
14:00–15:00 → 8       ← el pico del mediodía
15:00–20:00 → 5
```

Reglas:

- **sin franjas** → llama 24/7 con `capacity_default` (comportamiento de v1)
- **con franjas** → la ventana operativa **es** el conjunto de franjas; fuera de
  todas, `blocked_by: OUTSIDE_SCHEDULE`
- `capacity = 0` apaga ese tramo sin borrar la franja
- `20:00 → 02:00` cruza medianoche y se evalúa como un tramo continuo hasta las 2
  del día **siguiente**
- dos franjas solapadas se **rechazan** al guardar
- todo en hora **local**: a través de un cambio de horario, la franja sigue el
  reloj de pared

> No hay una tabla aparte de "horario de operación": **las franjas son el
> horario**. Un solo modelo, que no puede contradecirse consigo mismo.

---

## 6. Telegram — `Routes → la ruta → Telegram targets`

| propósito | qué manda |
|---|---|
| `recording` | grabaciones (WF10) |
| `account` | cuentas creadas (WF3) |
| `payment` | pagos (WF7/8) |
| `alert` | alertas operativas |

Varios chats por propósito. Si `recording_telegram` está encendido y no hay
ningún chat `recording`, la ruta **no está READY**.

En v1 los chat_id estaban escritos en cuatro nodos, y dos apuntaban al mismo
grupo por un copy-paste.

---

## 7. Tools del país — `Panel → Countries & Routes → el país → Tool configs`

Las tools pertenecen al **PAÍS**, no al proveedor de voz. `IN_PROVEEDOR1` e
`IN_STRINGEE` usan **la misma** configuración de India.

| campo | |
|---|---|
| `tool_type` | `CREATE_ACCOUNT` · `CREATE_PAYMENT_LINK` · `CALLBACK` |
| `enabled` | si la tool está disponible |
| `mode` | `CONFIG_ROUTER` o `CUSTOM_ENDPOINT` |
| `provider_key` | `cashstudio`, `okpay`, `monetix`… |
| `market` | mercado del proveedor de cuentas (`IND`, `NPL`) |
| `currency` | moneda de los pagos |
| `endpoint` / `http_method` | endpoint propio, según el modo |
| `credential_ref` | **nombre lógico** de la credential de n8n |
| `adapter_key` / `router_key` | sólo pagos, según el modo |
| `callback_url` / `return_url` | sólo pagos |
| `config_json` | extras: `min_amount`, `max_amount`, `portal_url`, `sms_template`… |

### Los modos, por tipo de tool

**`CREATE_ACCOUNT` y `CALLBACK`** — sin cambios respecto de V2.2:

- **`CONFIG_ROUTER`** — por la API de LeadStudio (`/cashstudio-account`) con
  el `market` del país
- **`CUSTOM_ENDPOINT`** — endpoint propio del país, con su `credential_ref`

**`CREATE_PAYMENT_LINK`** — dos modos propios, desde r2:

- **`DIRECT_PROVIDER`** — el país llama a su pasarela con un adaptador
  soportado (`adapter_key`). Hoy: India → `OKPAY_V1`
- **`UNIVERSAL_ROUTER`** — una API central recibe la petición normalizada y
  decide ella la pasarela (`router_key` + `endpoint` + `credential_ref`)

> El modo `CONFIG_ROUTER` **ya no vale para pagos**. Apuntaba a
> `POST /api/leads/{id}/payment-link`, un endpoint que nunca se verificó y que
> el WF7 real no usa. Detalle en `PAYMENT_INTEGRATION_GUIDE.md`.
> Nunca hay fallback de un modo al otro: si el configurado no se puede
> ejecutar, es `CONFIG_ERROR`.

### Reglas duras

- **`market` vacío ⇒ `CONFIG_ERROR`.** No se inventa un market de CashStudio.
- **`CUSTOM_ENDPOINT` sin `endpoint` ⇒ `CONFIG_ERROR`.**
- **`config_json` no puede contener secretos.** El panel rechaza claves tipo
  `api_key`, `secret`, `password`, `token`. Va la **referencia**, no el valor.
- Con el **país apagado**, la API lo informa y WF3/WF7 **no ejecutan**: responden
  al agente con un error de negocio legible en inglés, no con un 500.

---

## 8. Políticas de follow-up

Ver `FOLLOWUP_POLICY_GUIDE.md`. En resumen: reglas explícitas
`(result, attempt) → action`, asignadas **a la ruta**, compartidas por todos los
proveedores.

---

## 9. Parámetros operativos — `Panel → Call Center → Settings`

| clave | por defecto | qué hace |
|---|---|---|
| `reconcile_dispatching_minutes` | 5 | `DISPATCHING` más viejo → `NEEDS_RECONCILIATION` |
| `reconcile_unknown_minutes` | 5 | `UNKNOWN` más viejo → `NEEDS_RECONCILIATION` |
| `reconcile_dispatched_minutes` | 60 | `DISPATCHED` sin post-call → `NEEDS_RECONCILIATION` |
| `reconcile_claimed_minutes` | 10 | `CLAIMED` que nunca despachó → `RELEASED` |
| `reconcile_ledger_minutes` | 15 | claim de conversación huérfano |
| `tech_retry_max` | 8 | tope de reintentos técnicos por intento de negocio |
| `tech_retry_backoff_cap_minutes` | 60 | tope del backoff 1, 2, 4… |
| `postcall_polling_window_minutes` | 20 | ventana del polling de respaldo de WF9 |
| `legacy_compat_route_key` | *(vacío)* | ruta para post-calls de v1 sin `route_key`. **Vacío = no se procesan** |
| `crm_notes_language` | `en` | idioma obligatorio de las notas del CRM |
| `recording_default_min_secs` | 60 | mínimo si la ruta no lo define |
| `wf14_leadstudio_page_size` | 200 | paginación de la reconciliación |

`wf_settings` **rechaza claves con pinta de secreto**, igual que `config_json`.

---

## 10. Secretos: dónde va cada cosa

| secreto | dónde |
|---|---|
| API key de ElevenLabs | credential n8n `ElevenLabs API` |
| usuario/clave de LeadStudio | credential n8n `LeadStudio Login` (Custom Auth) |
| token del panel | credential n8n `Landmark Panel API` + `LM_ROUTES_API_TOKEN` |
| HMAC del post-call | variable de entorno `ELEVENLABS_WEBHOOK_SECRET` |
| clave del gateway de SMS | credential n8n `SMS Gateway` |
| clave de firma de un proveedor de pagos | credential n8n `Payment Provider API` |
| clave de MySQL | credential n8n + `.env` del panel |

**En la base solo va la referencia lógica:** `credential_ref = 'OKPAY_IN'`.

---

## 11. Cómo saber por qué una ruta no llama

`/api/routes/active?all=1` devuelve `blocked_by` para cada ruta:

| valor | qué significa |
|---|---|
| `COUNTRY_DISABLED` | el país está apagado |
| `COUNTRY_ARCHIVED` | el país está archivado |
| `PROVIDER_DISABLED` | el proveedor está apagado |
| `ROUTE_DISABLED` | la ruta está apagada |
| `ROUTE_ARCHIVED` | la ruta está archivada |
| `ROUTE_NOT_READY` | falta configuración — `config_issues` dice qué |
| `COUNTRY_NOT_READY` | el país está incompleto |
| `OUTSIDE_SCHEDULE` | fuera de todas las franjas |
| `ZERO_CAPACITY` | la franja vigente tiene capacidad 0 |
| `INVALID_TIMEZONE` | el huso del país no es válido |

Lo mismo se ve en la pantalla de rutas, con el detalle de qué campo falta.
