# Guía de facturación

Cómo se cobra cada proveedor de voz, dónde se configura, y la distinción
que más se malinterpreta: **tarifa contractual** frente a **coste efectivo**.

---

## 1. El problema que esto resuelve

El panel tenía el mismo proveedor comercial dado de alta **dos veces**:

| | Dónde vivía | Qué guardaba |
|---|---|---|
| SIP Balance | `sip_providers`, `sip_provider_pricing`, `sip_deposits` | depósitos, precio por país, saldo |
| Call Center | `voice_providers`, `call_routes` | adaptador, rutas, interruptores |

Había que crear Provider1 en los dos sitios, y las dos fichas podían
contradecirse. Desde r2 hay **una sola identidad**: `voice_providers`
apunta a su ficha de SIP Balance con `legacy_sip_provider_id`.

**SIP Balance no se tocó.** Sigue siendo el dueño de depósitos, precios
históricos y saldo. V2 sólo lee de ahí. Apagar un proveedor en V2 no
borra ni un depósito.

---

## 2. Los cinco modelos

`billing_model` es `VARCHAR` con un registro en la aplicación
(`panel/app/billing.py`), no un `ENUM` de SQL: añadir un modelo es tocar
un diccionario, no migrar la base.

| Modelo | Se cobra por | Necesita tarifa unitaria | Necesita cuota mensual |
|---|---|---|---|
| `PER_MINUTE` | minutos facturables × tarifa de ruta | sí, por ruta | no |
| `MONTHLY_FLAT` | una cuota fija, uso ilimitado | **no** | sí |
| `PER_CALL` | llamadas facturables × tarifa por llamada | sí, por ruta | no |
| `INCLUDED` | nada: va dentro de otro contrato | no | no |
| `CUSTOM` | términos que el panel no modela | no | no |

Un modelo que no está en el registro se puede **guardar** (aparece en
rojo como *unsupported*), pero el panel no calcula su coste ni lo da por
válido. Es el mismo criterio que con los adaptadores de voz.

---

## 3. Provider1 — `PER_MINUTE`

Rutas distintas del mismo país pueden tener tarifas distintas. Eso es
justo lo que `sip_provider_pricing` **no** podía expresar: su
`UNIQUE(provider_id, country)` obliga a un único precio por país.

Por eso en V2 la tarifa vive en la **ruta**:

```
IN_PROVEEDOR1      0.3000 USD/min     (trunk proveedor1)
IN_PROVEEDOR1_BIS  0.4500 USD/min     (otro trunk, mismo país)
NP_PROVEEDOR1      0.3600 USD/min     (trunk proveedor-nepal)
MX_PROVEEDOR1      0.3000 USD/min     (trunk proveedor-mx)
```

La primera vez que corre la migración, cada ruta **hereda** el precio que
ya tenía su país en SIP Balance, emparejando por `trunk_name`. A partir
de ahí son independientes: cambiar la tarifa de una ruta no modifica
`sip_provider_pricing`, y re-ejecutar la migración no pisa lo editado.

Coste del período:

```
coste = Σ (minutos facturables de la ruta × tarifa de esa ruta)
```

Si una ruta no tiene tarifa, **no se cuenta como 0**: el panel lo dice.
Sumar cero callado daría una factura estimada más baja que la real.

---

## 4. Stringee — `MONTHLY_FLAT`

Stringee **no se cobra por minuto**. Es una cuota mensual con uso
ilimitado. De ahí tres reglas que el panel aplica y los tests vigilan:

1. **No se le exige precio por minuto.** Pedírselo sería inventar un dato
   que su contrato no tiene, y dejaría la ruta en NOT READY para siempre.
2. **Price/min muestra `N/A`, nunca `$0/min`.** Cero diría que el minuto
   es gratis. La verdad es que la pregunta no aplica.
3. **El coste del período es la cuota**, no una suma de llamadas.

```
coste del período = cuota mensual
```

El panel añade además esta frase, porque es donde se suele leer mal:

> The fee is owed for the period regardless of usage. Individual calls
> did not generate this amount.

Las métricas operativas se siguen calculando igual: llamadas,
contestadas, tasa de respuesta, minutos hablados, duración media. Que no
se cobre por minuto no significa que no se mida.

---

## 5. Tarifa contractual vs coste efectivo

Esta es **la** distinción de esta guía.

| | Qué es | De dónde sale |
|---|---|---|
| **CONTRACT RATE** | lo que el contrato dice que cuesta una unidad | del contrato |
| **EFFECTIVE COST** | cuota fija ÷ uso | una división que hacemos nosotros |

Con una cuota de 1 200 USD y 4 000 llamadas, el coste efectivo por
llamada es 0,30 USD. Eso **no** es la tarifa de Stringee. Stringee no
cobra 0,30 por llamada: cobra 1 200 al mes. Si el mes que viene se hacen
8 000 llamadas, el número baja a 0,15 sin que nadie haya renegociado
nada.

Por eso, cada vez que aparece, va etiquetado:

```
EFFECTIVE COST                          [ NOT A CONTRACT RATE ]
per call              0.3000 USD/call   — monthly fee ÷ calls in the period
per answered call     1.0000 USD/call   — monthly fee ÷ answered calls
per talk minute       0.4000 USD/min    — monthly fee ÷ talk minutes
```

Sin uso en el período, el coste efectivo es `—`, no cero: dividir entre
cero no da un número, y poner 0 diría que ese mes salió gratis.

El coste efectivo **sólo** se calcula para `MONTHLY_FLAT`. Donde ya hay
una tarifa real no hace falta estimar ninguna.

---

## 6. Dónde se configura

**Call Center → Billing**, sólo rol `master`.

### Alta de un proveedor: un solo flujo

`Add provider` resuelve también la identidad de facturación, para que
nadie tenga que dar de alta el mismo proveedor dos veces:

| Si el adaptador… | Entonces |
|---|---|
| factura contra un trunk SIP (`ELEVENLABS_SIP`) | se ofrece **vincular** una ficha existente de SIP Balance **o crear una** en el mismo paso |
| no (`STRINGEE_WORKER`) | no se pide ficha: nunca tendrá una, y pedírsela sería inventar un dato |

**Un proveedor nuevo nace apagado.** Darlo de alta no es encenderlo.

### Vincular después

Un proveedor que figure `NOT LINKED` trae su propio selector con las
fichas de SIP Balance disponibles. Una ficha sólo puede estar vinculada a
un proveedor V2; intentar vincularla a un segundo se rechaza.

Si alguien crea un proveedor **desde SIP Balance**, esa página sigue
funcionando igual. En Billing aparece marcado `BILLING-ONLY`, con la
indicación de vincularlo desde su proveedor de Call Center. **Nunca se
crea un duplicado automáticamente**: una ficha de SIP Balance sin
proveedor V2 es legítima — se puede llevar la cuenta corriente de un
proveedor sin que llame nadie.

Vincular y desvincular queda auditado, y **no toca** depósitos ni precios.

La pantalla es condicional: los campos que no significan nada para el
modelo elegido no se muestran. Un `MONTHLY_FLAT` no enseña columna de
precio por minuto; un `PER_MINUTE` no enseña cuota mensual.

| Campo | Dónde | Cuándo aplica |
|---|---|---|
| `billing_model` | proveedor | siempre |
| `billing_currency` | proveedor | siempre |
| `monthly_fee` | proveedor | `MONTHLY_FLAT` |
| `billing_start_date` | proveedor | siempre |
| `price_per_minute` | **ruta** | `PER_MINUTE` |
| `price_per_call` | **ruta** | `PER_CALL` |
| `trunk_name` | ruta | empareja con SIP Balance |

Al cambiar de modelo, el panel **borra** el dato que deja de aplicar: una
cuota mensual colgando en un proveedor que pasó a `PER_MINUTE` haría creer
que se sigue cobrando.

---

## 7. Apagar ≠ borrar

`provider.enabled` controla **llamar**, nada más.

Apagar Provider1:

- detiene el discado por todas sus rutas
- **no** borra el proveedor, ni sus rutas, ni sus depósitos, ni su
  historial de precios, ni su fecha de inicio de facturación

La factura del período en que estuvo encendido sigue siendo la que es.
Apagar un proveedor no puede reescribir el pasado.

---

## 8. Auditoría

Todo cambio de facturación deja fila en `billing_audit`: qué campo, valor
anterior, valor nuevo, motivo, quién y cuándo. Nunca se guarda una
credencial — sólo el campo y sus valores.

Se ve al pie de **Call Center → Billing**.

---

## 9. Lo que queda pendiente

- El filtro de proveedor del **dashboard legacy** cambia el embudo y las
  cuentas, pero **no** el gráfico Call Traffic ni el export diario: ésos
  leen `cdr_panel`, que es tráfico de Asterisk. Es la semántica que ya
  tenía el panel y no se cambió (§32 pide no cambiarla en silencio). En
  la analítica **V2** el filtro de proveedor sí afecta a todo, porque ahí
  cada llamada sabe por qué proveedor salió.
- El saldo con el proveedor (depósitos − consumo) lo sigue calculando SIP
  Balance con el CDR de Asterisk. No se duplicó en V2.
