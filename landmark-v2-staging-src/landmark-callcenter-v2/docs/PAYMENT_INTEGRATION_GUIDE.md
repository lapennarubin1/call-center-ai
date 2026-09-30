# Guía de integración de pagos

Cómo se crea un link de pago, los dos modos soportados, y cómo se añade
un país sin tocar el JSON de ningún workflow.

---

## 1. Lo que se quitó, y por qué

La versión anterior asumía que LeadStudio expone:

```
POST /api/leads/{id}/payment-link
```

**Nadie lo verificó nunca.** El WF7 que corre hoy en producción no lo
usa: India cobra directamente contra OkPay (`api.wpay.one/v1/Collect`) y
la rama de Nepal está marcada `[DISABLED - CONFIGURE]` en el propio
workflow.

Ese endpoint se ha eliminado del paquete y **no se ha sustituido por otra
suposición**. Un test recorre todo el paquete buscando cualquier uso vivo
de esa URL y falla si reaparece.

Si algún día se verifica que LeadStudio sí sabe generar links de pago, se
añade como un adaptador más. Hasta entonces, no existe.

---

## 2. Los dos modos

### `DIRECT_PROVIDER`

El país llama directamente a su pasarela, con un **adaptador soportado**.
La lógica de firma vive en código revisable (`panel/app/billing.py` no,
`panel/app/payments.py`) y en su gemelo JS (`tools/js/lmpay.js`), nunca
como JavaScript guardado en la base de datos.

Hoy hay un adaptador: **`OKPAY_V1`**, extraído del WF7 vivo.

### `UNIVERSAL_ROUTER`

Una API central recibe la petición ya normalizada y decide **ella** qué
pasarela usar. n8n sólo consume el resultado normalizado.

Petición:

```json
{ "lead_id": "...", "country_iso": "IN", "amount": 5000, "currency": "INR" }
```

Respuesta:

```json
{ "success": true, "payment_url": "...", "payment_id": "...", "provider": "..." }
```

Ese router puede vivir en el backend de Landmark, en LeadStudio o en el
backend de otro cliente. **No está cableado a ninguno**: su `endpoint` y
su `credential_ref` son configuración del país.

### Nunca hay fallback entre modos

Si el modo configurado no se puede ejecutar, el resultado es
`CONFIG_ERROR`. Caerse al otro modo significaría cobrarle al cliente por
una vía que nadie autorizó. El panel ni siquiera deja guardar un país con
adaptador **y** router a la vez.

---

## 3. India: OkPay, tal como funciona hoy

Extraído del nodo `Build OkPay Signed Request` del WF7 real.

| | |
|---|---|
| Endpoint | `POST https://api.wpay.one/v1/Collect` |
| Content-Type | `application/x-www-form-urlencoded` |
| Firma | MD5 de los parámetros ordenados + `&key=<clave>` |
| Moneda | INR |
| `pay_type` | UPI |

Parámetros que entran en la firma: `mchId`, `currency`, `out_trade_no`,
`pay_type`, `money`, `notify_url`, `returnUrl`, `phone`, `attach`
(el `lead_id`). Los vacíos se descartan antes de firmar.

**Éxito sólo con las tres condiciones**: HTTP 2xx **y** `body.code === 0`
**y** hay URL. Con dos de tres no hay link que mandarle al cliente.

### El detalle que rompería el cobro

El MD5 de producción empaqueta `charCodeAt` byte a byte, es decir firma
sobre **Latin-1**, no sobre UTF-8. Firmar en UTF-8 da otra firma y OkPay
rechaza el cobro.

Verificado: `'Ñ-áé'` da `5e158e7d…` en producción y en Latin-1, y
`44e189a1…` en UTF-8.

El port lo replica exactamente, y hay un test de **paridad a tres
bandas** — Python del panel ↔ gemelo JS de los nodos ↔ la referencia
extraída del WF7 vivo — sobre ~300 casos, incluidos acentos y `=` en los
campos. Si cualquiera de las tres se desviara, el test falla antes de que
falle un cobro.

Un carácter por encima de `U+00FF` (devanagari, CJK) no cabe en un byte:
el empaquetado de producción se desborda y produce una firma corrupta que
la pasarela rechazaría igual. Ahí el adaptador **falla en claro**, con el
campo señalado, en vez de mandarle al cliente un link que no va a
funcionar.

### Las credenciales

`mchId` y la clave de firma **no están en la base de datos**. En el WF7 de
producción la clave viaja en claro dentro del JSON del workflow; copiarla
al paquete la metería además en los backups y en cada copia.

Llegan por entorno / credential de n8n:

```
LM_OKPAY_MCH_ID     merchant id
LM_OKPAY_SIGN_KEY   clave de firma
```

Sin ellas, WF7 devuelve `CONFIG_ERROR` y no intenta cobrar.

**Por eso India se entrega DESHABILITADA.** La migración la pasa a
`DIRECT_PROVIDER` / `OKPAY_V1` y la apaga: encenderla sin credencial haría
que el agente fallase delante del cliente.

---

## 4. Nepal: no configurado

El WF7 real marca la rama de Nepal `[DISABLED - CONFIGURE]`. El paquete
refleja exactamente eso: `enabled = 0`, sin adaptador.

Dos reglas que se cumplen:

- **Nepal sin pago no bloquea nada más.** Se puede llamar y abrir cuentas
  en Nepal con el pago deshabilitado. Un país con el pago apagado no
  genera ni un problema de validación.
- **Nepal habilitado y sin configurar es NOT READY**, con los campos que
  faltan listados. No se enciende «a ver si funciona».

No hay adaptador soportado para Monetix. Se puede guardar
`adapter_key = 'MONETIX_V1'` mientras el país esté apagado, pero no puede
operar hasta que exista el adaptador.

---

## 5. Añadir un país

### Con un adaptador que ya existe → sólo configuración

Un país nuevo que cobre por OkPay:

1. Call Center → Countries → el país → Payment link
2. `mode = DIRECT_PROVIDER`, `adapter_key = OKPAY_V1`
3. Moneda, `callback_url`, `return_url`, `credential_ref`
4. Validar → READY
5. Encender

**No se edita el JSON de WF7.** El workflow resuelve el adaptador en
tiempo de ejecución desde `country_tool_configs`.

### Con un router universal → sólo configuración

1. `mode = UNIVERSAL_ROUTER`, `router_key = GENERIC_JSON_V1`
2. `endpoint` y `credential_ref` del router
3. Validar → READY

### Con una pasarela nueva → un adaptador

Sólo si la pasarela nueva tiene su propia forma de firmar:

1. Entrada en `PAYMENT_ADAPTERS` (`panel/app/payments.py`) con los
   campos que exige y qué pide de la credencial
2. Función de construcción y de parseo, en Python
3. Gemelo en `tools/js/lmpay.js`
4. Test de paridad entre los dos
5. Rama en el `[PAYMENT] Adapter Router` de WF7

Después, cualquier país que use esa pasarela es sólo configuración.

---

## 6. Idempotencia: no cobrar dos veces

`out_trade_no` se calcula de forma determinista a partir de
`(lead_id, attempt_ref)`. El mismo pedido da siempre el mismo
identificador, y `wf_payment_orders` lo tiene como `UNIQUE`.

Si el agente pide el link dos veces, el segundo intento choca con la
restricción y no se genera un segundo cobro. No se depende de que la
pasarela respete `X-Idempotency-Key`: el pedido se reserva **antes** de
llamar.

---

## 7. Errores

| Situación | Clase | Qué pasa |
|---|---|---|
| Falta credencial o configuración | `CONFIG_ERROR` | no se intenta cobrar |
| HTTP 401 / 403 | `AUTH_ERROR` | no consume intento de negocio |
| HTTP 5xx | `PROVIDER_ERROR` | reintento técnico |
| Sin respuesta / timeout | `UNKNOWN` | `NEEDS_RECONCILIATION`, sin reintento ciego |
| 2xx con `code != 0` | `PROVIDER_ERROR` | fallo real, aunque el HTTP diga 200 |
| Router dice `success` sin URL | `PROVIDER_ERROR` | no hay link: no es éxito |

Un timeout es **ambiguo**: el link pudo crearse. Se marca
`NEEDS_RECONCILIATION` y no se reintenta solo.

---

## 8. Inglés en el CRM

Todo lo que va a LeadStudio va en inglés, sin excepción:

```
Payment link sent successfully.
Payment link could not be created.
```

El WF7 de producción devolvía al agente:

> `Payment link generate karne mein technical issue aaya. Thodi der baad try karte hain.`

Eso es exactamente lo que la regla prohíbe. Hay un test que comprueba que
ninguno de esos términos vuelve.

No se traduce nunca: nombre del cliente, teléfono, email, IDs, URLs,
`route_key`, identificadores del proveedor, ni los enums técnicos que
exige la API del CRM.

---

## 9. Lo que queda por verificar

- El formato exacto del callback de OkPay hacia `payment-callback-v2`
  está tomado del WF8 vivo, pero no se ha probado contra la pasarela real
  desde V2 (ver `PENDING_VERIFICATION.md`).
- No hay contrato de router universal verificado con ningún backend
  concreto: `GENERIC_JSON_V1` define la forma, pero nadie ha respondido
  todavía con ella.
