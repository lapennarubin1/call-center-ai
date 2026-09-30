# ADD_COUNTRY_GUIDE.md
**Agregar un país es configuración. No se toca n8n.**
Fase BUILD · 21/09/2026

---

## 0. Lo que NO hay que hacer

- ❌ duplicar nodos en WF2
- ❌ agregar una rama `if (country === 'pakistan')`
- ❌ crear un proveedor nuevo solo porque es otro país
- ❌ editar ningún JSON de workflow
- ❌ tocar Asterisk (salvo que el trunk necesite un endpoint nuevo — §7)

---

## 1. Los pasos (ejemplo: Pakistán)

### 1.1 Crear el país

`Panel → Countries & Routes → Countries → New`

```
iso                   PK
country_name          Pakistan
dial_prefix           +92
national_number_len   10
timezone              Asia/Karachi
language              ur
enabled               NO  ← nace apagado, a propósito
```

> El **huso** es lo más importante: de ahí salen las franjas horarias y el cálculo
> de días hábiles de la política de follow-up.

### 1.2 Crear la ruta

`Countries & Routes → Routes → New`

```
route_key                   PK_PROVEEDOR1
iso                         PK
provider_id                 PROVEEDOR1        ← el MISMO proveedor de siempre
priority                    70
capacity_default            3
elevenlabs_agent_id         agent_...         ← el agente urdu, creado en ElevenLabs
elevenlabs_phone_number_id  phnum_...
followup_policy             STANDARD_CALL_RETRY
recording_enabled           sí
recording_min_secs          60
enabled                     NO
```

La ruta dirá **NOT READY** hasta que estén todos los campos. Eso es correcto: se
guarda incompleta y se completa.

### 1.3 Franjas horarias (opcional)

Sin franjas, llama 24/7 con `capacity_default`. Con franjas:

```
mon-fri  09:00–13:00   3
mon-fri  13:00–14:00   5
mon-fri  14:00–20:00   3
```

En **hora local de Karachi**.

### 1.4 Telegram (si se quieren las grabaciones)

`la ruta → Telegram targets`: propósito `recording`, el chat del grupo.

Si `recording_telegram` está encendido y no hay chat, la ruta **no queda READY**.

### 1.5 Las tools del país

`Countries & Routes → Pakistan → Tool configs`

```
CREATE_ACCOUNT
  enabled         sí
  mode            CONFIG_ROUTER
  provider_key    cashstudio
  market          <el market REAL de CashStudio para Pakistán>   ← §5
  credential_ref  LEADSTUDIO_API
  config_json     {"portal_url": "https://..."}

CREATE_PAYMENT_LINK
  enabled         solo si hay un proveedor de pagos contratado
  ...

CALLBACK
  enabled         sí
```

### 1.6 Verificar que está READY

En la lista de rutas debe decir **READY**. Si dice NOT READY, el panel muestra
exactamente qué campo falta.

### 1.7 Encender, en este orden

```
1. la RUTA        → ON
2. el PROVEEDOR   → ya está ON (es el mismo de siempre)
3. el PAÍS        → ON   ← el último
```

Encender el país al final permite dejar todo configurado y revisado, y recién
entonces empezar a llamar.

### 1.8 Confirmar

```bash
curl -s -H "X-Service-Token: <token>" \
  http://172.18.0.1:8080/api/routes/active | python3 -m json.tool | grep -A3 PK_
```

Debe aparecer con `calling_now: true` y su `capacity_now`.

En el ciclo siguiente (< 1 min), WF2 empieza a pedir la cola de `PK`.

---

## 2. Lo que pasa solo, sin configurar nada más

| | |
|---|---|
| teléfonos | se normalizan a E.164 con `+92` |
| reintentos | `STANDARD_CALL_RETRY`, con días hábiles en `Asia/Karachi` |
| post-call | WF9 correlaciona por `call_job_id` o por la llamada en vuelo |
| follow-ups | el motor, con la política de la ruta |
| notas del CRM | **en inglés**, aunque el agente hable urdu |
| grabaciones | WF10, con el mínimo de la ruta |
| analytics | el país aparece solo en los desgloses |
| reconciliación | WF14 lo incluye |

---

## 3. Un país con DOS proveedores

Es el caso de India. Se crea una **segunda ruta** para el mismo país:

```
route_key      PK_STRINGEE
iso            PK
provider_id    STRINGEE
priority       80                 ← más alto = se reparte después
capacity_default  1
caller_id      <número>
elevenlabs_agent_id  agent_...
```

Las dos rutas conviven. La demanda del país pasa a ser la **suma**:

```
PK_PROVEEDOR1 = 3  +  PK_STRINGEE = 1   →  demanda 4
```

WF2 pide **una sola cola** de 4 leads y los reparte por `priority`. El claim
atómico garantiza que ninguna llame al mismo lead.

**Las tools son del país**: `PK_PROVEEDOR1` y `PK_STRINGEE` comparten exactamente
la misma configuración de cuenta y de pagos. No hay que duplicarla.

---

## 4. Checklist

- [ ] país creado, con el **huso** correcto
- [ ] ruta creada y en **READY**
- [ ] agente de ElevenLabs creado, en el idioma del país
- [ ] `market` de CashStudio **confirmado** (§5)
- [ ] proveedor de pagos definido, o `CREATE_PAYMENT_LINK` apagado
- [ ] chats de Telegram, si van las grabaciones
- [ ] franjas horarias, si el negocio las quiere
- [ ] probado con `capacity_default = 1`
- [ ] revisada la primera llamada de punta a punta en `wf_call_jobs`
- [ ] verificado que la nota del CRM está en inglés

---

## 5. Lo que hay que confirmar **antes**, no después

| | por qué |
|---|---|
| **el `market` de CashStudio** | si es inventado, el proveedor rechaza y ningún cliente abre cuenta. Sin market confirmado, dejar `CREATE_ACCOUNT` **apagado** |
| **el proveedor de pagos y su moneda** | si no hay contrato, dejar `CREATE_PAYMENT_LINK` apagado (como Nepal con Monetix) |
| **el número saliente / trunk** | que el proveedor SIP pueda terminar llamadas en ese destino |
| **el agente de ElevenLabs** | creado, con su idioma y sus tools apuntando a los webhooks V2 |
| **regulación local** | horarios permitidos, consentimiento, grabación |

> El suite **no inventa** configuración: un `market` vacío es `CONFIG_ERROR`, no
> un valor por defecto.

---

## 6. Quitar un país

**No se borra.** Se apaga:

```
Countries → el país → Disable
```

Paran todas sus rutas al instante. La configuración, el historial de llamadas y
los eventos quedan.

Si además hay que sacarlo de las listas: `Archive`. Sus rutas siguen
**resolviendo por clave**, así que un post-call tardío no se pierde.

---

## 7. Cuándo SÍ hay que tocar Asterisk

Solo si el nuevo destino necesita un **endpoint distinto** en el trunk — por
ejemplo, un `from_user` propio o un `send_pai` distinto, como ya existe para
México y Nepal:

```ini
[proveedor-pakistan]
type=endpoint
transport=transport-udp
context=from-provider-pakistan
aors=proveedor1-aor          ; el MISMO trunk
outbound_auth=proveedor1-auth ; las MISMAS credenciales
send_pai=yes
from_user=<el número asignado>
```

Y su patrón en el dialplan. **Sigue siendo UN proveedor comercial**: mismo AOR,
misma autenticación. Es exactamente el modelo que ya usa el Asterisk actual.

Eso es trabajo de telefonía, fuera del alcance del suite V2, y **no cambia nada**
en el panel ni en n8n.
