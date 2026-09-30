# CRM_ENGLISH_RULE.md
**Todo texto humano que llega a LeadStudio va en inglés.** Sin excepciones.
Fase BUILD · 21/09/2026

---

## 1. La regla

> Cualquier texto **legible por una persona** que el suite V2 escriba o envíe a
> LeadStudio / CRM está en **inglés**, sin importar el país, el idioma del
> cliente ni el idioma del agente.

Alcance:

- notas de llamada · de follow-up · de callback
- notas de creación de cuenta · de pago · de grabación · de reconciliación
- errores legibles que se **guardan** en el CRM
- descripciones y resúmenes que el suite genera
- los mensajes que el agente le dice al cliente cuando una tool falla

## 2. Lo que NO se traduce

| | ejemplo |
|---|---|
| nombre del cliente | `Rahul Sharma` |
| teléfono | `+919876543210` |
| email | `rahul@example.com` |
| IDs | `8f2c1a55-4e2b-…`, `conv_01k5…`, `fu_7c1d9e4a` |
| URLs | `https://crm.landmarkmarkets.in/` |
| route keys | `IN_PROVEEDOR1` |
| IDs de proveedor | `proveedor1`, `okpay`, `cashstudio` |
| códigos SIP | `603` |

Los **valores de enum/estado técnicos** siguen el contrato de la API de
LeadStudio *exactamente como esa API los define* (`type`, `outcome`,
`callStatus`, `status`, `stage`). No son texto humano y no se "traducen" ni se
inventan.

## 3. De dónde sale el texto

De **un solo catálogo**, en dos copias que un test mantiene idénticas:

| | |
|---|---|
| `panel/app/crm_notes.py` | la referencia, y lo que usa el panel |
| `tools/js/lmnotes.js` | lo que se inlinea en los Code nodes de n8n |

`tests/test_crm_english_v2.py` compara clave por clave y frase por frase: si
alguien edita una sola de las dos, el test falla. También verifica que las notas
generadas para 14 casos reales sean **byte a byte iguales** en Python y en JS.

Ningún nodo escribe un literal directamente en `notes`. El test
`todo POST de nota toma el texto de lmnotes.js` lo comprueba recorriendo los
siete JSON.

### Formato de una nota

```
<frase del catálogo> <frase de la acción> [<datos técnicos>]
```

```
Call was not answered. Next attempt scheduled.
[attempt 3 · route IN_PROVEEDOR1 · provider proveedor1 · sip 603 ·
 next 2026-09-22T04:30:00Z]
```

```
Trading account created successfully.
[market IND · username LM1234 · portal https://crm.landmarkmarkets.in/]
```

Los datos técnicos van entre corchetes al final: siguen siendo legibles para el
operador y **grepables** desde el CRM.

## 4. NO hay excepción para el resumen del agente

Una versión anterior de esta guía decía que el resumen de la conversación
era "contenido del cliente" y podía ir sin traducir. **Eso era un error**:
metía hindi, nepalí, árabe y español dentro de notas del CRM, que es
exactamente lo que esta regla existe para impedir. La excepción se ha
eliminado.

La regla no tiene excepciones. Incluye:

- resumen del agente · resumen de la conversación
- motivo del callback
- explicación del error del proveedor
- nota de error de cuenta · nota de error de pago
- detalle de reconciliación
- cualquier nota, descripción o comentario

### Qué se hace con el resumen

| Situación | Qué pasa |
|---|---|
| La fuente declara inglés (`summary_language='en'`) **y** el texto lo parece | se adjunta tras `Agent summary:` |
| La fuente no declara idioma | **no se adjunta** |
| La fuente declara inglés pero el texto no lo es | **no se adjunta** |
| El resumen está en otro idioma | **no se adjunta** |

Sin idioma declarado se descarta **aunque parezca inglés**. El detector
existe para cazar regresiones, no para autorizar contenido de idioma
desconocido.

Hoy **ElevenLabs no garantiza el idioma** del resumen: el agente habla en
el idioma del país. Así que en la práctica WF9 lo marca como no
garantizado y el resumen **no llega al CRM**.

**No se pierde nada.** El original viaja igual en el evento local
(`wf_events.metadata_json`), que es donde está la evidencia. El CRM recibe
su nota canónica:

```
Call answered. Contact established; no retry scheduled.
[attempt 1 · route IN_STRINGEE · provider stringee · duration 143s]
```

**No se traduce por nuestra cuenta.** Inventar una traducción dentro de
n8n, sin un servicio de traducción configurado y verificado, sería peor
que omitir el resumen: nadie sabría si lo que lee es lo que dijo el
cliente.

## 4b. Los errores van como código, no como texto

El texto crudo de un proveedor puede venir en cualquier idioma. Pasaba
directo al CRM:

```
MAL:  Payment link could not be created. [reason Pago rechazado por banco]
BIEN: Payment link could not be created. [code PROVIDER_ERROR]
```

`safe_detail()` sólo deja pasar tokens que **parecen técnicos**:
MAYÚSCULAS con guion bajo, algo con un dígito, o algo con separador
(`HTTP_503`, `PROVIDER_ERROR`, `4001`, `order:LM123`). Una palabra
corriente en minúsculas es prosa de alguien y se descarta — y si se
descarta un token, se descartan todos: media frase es peor que ninguna.

El error crudo se guarda en la evidencia local y en los issues de
reconciliación, donde sí sirve para diagnosticar.

### Lo que sigue exento

Nombres, teléfonos, emails, IDs, URLs, `route_key`, IDs de transacción,
códigos y enums técnicos que exige la API del CRM. Nada de eso se traduce
ni se altera.

## 5. Cómo se verifica

`tests/test_crm_english_v2.py` — 16 comprobaciones:

1. las 32 frases del catálogo pasan el detector de inglés
2. el detector atrapa lo que v1 escribía: `No contestó.`,
   `Cuenta creada correctamente.`, `🟡 No answer (SIP 603) — intento 3`
3. el detector atrapa también frases **sin acentos**: `El cliente quiere
   que lo llamen.` no lleva ni un acento ni ninguna expresión de la lista,
   pero tiene cinco palabras funcionales españolas
4. los catálogos Python y JS tienen las mismas claves y el mismo texto
5. las notas generadas coinciden en ambos lenguajes (14 casos)
6. `route_key`, IDs, códigos SIP y URLs sobreviven intactos
7. **resúmenes en español, hindi, nepalí y árabe NO llegan al CRM**
8. la nota sigue siendo inglés válido y conserva su texto canónico
9. un resumen **declarado** en inglés sí se usa
10. sin idioma declarado se descarta, aunque parezca inglés
11. un resumen mal declarado como inglés también se descarta
12. **errores crudos del proveedor NO llegan al CRM**, en ningún idioma
13. el error se convierte en un código publicable
14. los identificadores técnicos pasan; la prosa no
15. ningún nodo de los 7 workflows escribe un literal en español en un campo del CRM
16. todo `POST /followups` con `notes` toma el texto del catálogo, y los
    `message_to_user` están en inglés (v1 respondía en hinglish)

## 6. Qué cambia respecto de v1

| v1 escribía en el CRM | V2 escribe |
|---|---|
| `No contestó (duración 0s) \| fin: ` | `Call was not answered. [attempt 1 · route IN_PROVEEDOR1]` |
| `🟡 No answer (SIP 603) — intento 3 (ciclo 1, pos 1/3) — próxima en 2h` | `Call was not answered. Next attempt scheduled. [attempt 3 · sip 603 · next 2026-09-22T04:30:00Z]` |
| `📵 Buzón de voz \| duración: 5s` | `Call reached voicemail. [attempt 2 · duration 5s]` |
| `Cuenta CashStudio creada \| market: IND \| user: …` | `Trading account created successfully. [market IND · username …]` |
| `Payment link generate karne mein technical issue aaya.` | `We could not generate the payment link right now. Our team will follow up.` |
| `Contestó, conversación completa \| …` | `Call answered. Contact established; no retry scheduled. […] Agent summary: …` |

Además desaparecen los emojis de las notas: en v1 el `🟡` y el `📵` iban dentro
del campo `notes` del CRM.

## 7. Cómo agregar una frase

1. agregarla a `PHRASES` en `panel/app/crm_notes.py`
2. agregarla **idéntica** a `LM_PHRASES` en `tools/js/lmnotes.js`
3. `python3 tools/build_workflows.py` (re-inlinea el catálogo en los nodos)
4. `python3 tests/test_crm_english_v2.py`

**No se editan las frases existentes**: cambiarlas cambia el historial que el
operador ve en el CRM. Se agregan claves nuevas.
