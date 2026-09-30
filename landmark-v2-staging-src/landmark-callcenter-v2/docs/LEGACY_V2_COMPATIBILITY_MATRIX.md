# Matriz de compatibilidad Legacy ↔ V2

Qué workflows del call center actual pueden seguir corriendo con V2
encendido, y cuáles no.

---

## 1. Por qué esto importa

No es un problema de datos duplicados. Es un problema con el cliente al
teléfono:

- dos despachadores → **la misma persona recibe dos llamadas**
- dos motores de follow-up → **se le agenda dos veces**
- dos flujos de cuenta → **se le abren dos cuentas**
- dos flujos de pago → **se le mandan dos links de cobro**

Por eso el panel se **niega** a cambiar de modo mientras el otro sistema
pueda estar llamando, en vez de avisar y dejar pasar.

---

## 2. El criterio

Un grupo legacy puede convivir con V2 si **sólo lee** o si es
**idempotente**. No puede convivir si toca al cliente o si cuenta su
llamada.

**Fail-closed**: un grupo sin clasificar se trata como conflictivo. Es
deliberado. Equivocarse hacia «no convive» cuesta una confirmación de
más; equivocarse hacia «sí convive» cuesta llamar dos veces a un cliente.

---

## 3. La matriz

| Categoría | ¿Convive? | Por qué |
|---|---|---|
| `DISPATCH` | **no** | Llama. Dos despachadores = el cliente recibe dos llamadas. |
| `FOLLOWUP` | **no** | Agenda reintentos. Duplicarlo agenda el mismo lead dos veces. |
| `ACCOUNT` | **no** | Abre cuentas. Duplicarlo abre dos cuentas al mismo cliente. |
| `PAYMENT` | **no** | Genera links de pago. Duplicarlo puede cobrar dos veces. |
| `POST_CALL` | **no** | Procesa resultados. Duplicarlo cuenta dos veces la misma llamada. |
| `RECORDING` | sí | Adjunta grabaciones. Un adjunto repetido molesta, no cobra. |
| `CRM_SYNC` | sí | Espeja el CRM a MySQL. Idempotente por diseño. |
| `ANALYTICS` | sí | Sólo lee y agrega. |
| `UNKNOWN` | **no** | Sin clasificar. Se asume conflictivo hasta que alguien lo clasifique. |

Las categorías que **no** conviven no se pueden marcar como convivientes
desde el panel: el módulo lo rechaza con el motivo escrito. No es una
casilla que se pueda desmarcar «porque sé lo que hago».

---

## 4. Los grupos que hay hoy

Clasificación inicial que siembra `005_legacy_backup_v2.sql`. Es
editable desde **Call Center → Legacy Backup**; una reclasificación no se
pisa al re-ejecutar la migración.

| Grupo | Categoría | ¿Convive? | Razonamiento |
|---|---|---|---|
| `INDIA - NEPAL` | `DISPATCH` | no | Llama a leads de India y Nepal. Choca con WF2 V2 sobre los mismos leads. |
| `MEXICO` | `DISPATCH` | no | Llama a leads de México. Mismo riesgo. |
| `INDIA - PROVEEDOR STRINGEE` | `DISPATCH` | no | Llama por Stringee. Choca con la ruta `IN_STRINGEE` de V2. |
| `PANEL` | `CRM_SYNC` | sí | Espeja leads a las tablas del panel. Sólo lee del CRM. |
| `CRM INDIA - NEPAL` | `CRM_SYNC` | sí | Espeja leads del CRM a MySQL. No llama ni crea follow-ups. |
| `CRM PANEL` | `CRM_SYNC` | sí | Espeja datos del CRM al panel. Sólo lectura. |

Cualquier grupo que exista en `n8n_switches` y no esté en esta lista entra
automáticamente como `UNKNOWN` y **bloquea el cambio de modo** hasta que
alguien lo clasifique.

---

## 5. Lo que el panel puede y no puede verificar

| | |
|---|---|
| **Sí puede** | saber qué rutas V2 están en condiciones de llamar (los tres interruptores + archivado) |
| **Sí puede** | saber qué grupos legacy existen y cómo están clasificados |
| **Sí puede** | leer el estado ON/OFF **real** de cada workflow legacy, preguntándole a n8n |
| **No puede** | impedir que alguien encienda un workflow desde la interfaz de n8n, saltándose el panel |

La pantalla muestra una columna **Running now** con el estado real de cada
grupo. Si n8n no responde, lo dice y **bloquea** el cambio a V2: no poder
verificar no es lo mismo que estar apagado.

---

## 6. Los enclavamientos

### V2_PRIMARY → LEGACY_BACKUP

Bloquea si **alguna ruta V2 puede llamar**. El mensaje nombra las rutas
concretas. Hay que apagarlas (ruta, proveedor o país) antes de cambiar.

### LEGACY_BACKUP → V2_PRIMARY

Lista los grupos conflictivos que hay que apagar antes, y también los que
**sí** pueden seguir corriendo — para no apagar de más.

### En ambos sentidos

- sólo rol `master`
- frase exacta tecleada: `SWITCH TO LEGACY` o `SWITCH TO V2`
- cualquier grupo `UNKNOWN` bloquea
- queda auditado: quién, cuándo, desde, hacia, por qué
- un intento fallido **no** cambia el modo ni escribe auditoría de cambio

---

## 7. Lo que el cambio de modo hace y no hace

**No enciende ni apaga** un solo workflow. Lo que sí hace es **impedir**
que se encienda lo que no toca:

| Modo | Se puede encender | No se puede encender |
|---|---|---|
| `V2_PRIMARY` | `CRM_SYNC`, `ANALYTICS`, `RECORDING` | `DISPATCH`, `FOLLOWUP`, `ACCOUNT`, `PAYMENT`, `POST_CALL`, `UNKNOWN` |
| `LEGACY_BACKUP` | cualquier grupo legacy | — (y V2 no puede llamar) |

El bloqueo se aplica al encendido manual, al masivo y al **programado**.
Un horario que intente encender un grupo conflictivo guarda en su propia
fila: `Scheduled activation blocked: operating mode is V2_PRIMARY.`

Encender y apagar workflows sigue donde siempre estuvo: en **Call
Center**, con los controles que ya existían (`Turn ON all`, `Turn OFF
all`, `Edit`, `Schedule`). Esa pantalla no se ha tocado.

Es deliberado: §51 dice explícitamente que no vale apagar todo a ciegas.
El panel dice **qué** hay que apagar; apagarlo es una acción humana.

---

## 8. Cómo comprobar que no hay duplicados

Antes de dar por buena una convivencia:

```sql
-- ¿Alguna ruta V2 puede llamar?
SELECT r.route_key
  FROM call_routes r
  JOIN voice_providers vp ON vp.id = r.provider_id
  JOIN countries c ON c.iso = r.iso
 WHERE r.enabled = 1 AND r.archived_at IS NULL
   AND vp.enabled = 1 AND c.enabled = 1 AND c.archived_at IS NULL;

-- ¿Qué grupos legacy conflictivos hay?
SELECT switch_label, category
  FROM legacy_group_classification
 WHERE coexists_with_v2 = 0;
```

Y el chequeo que de verdad cierra el asunto: en **Call Center**,
confirmar que esos grupos están en OFF.

Si los dos sistemas hubieran llamado a la vez, se ve así:

```sql
-- El mismo lead con llamada V2 y CDR legacy en la misma ventana
SELECT j.lead_id, j.route_key, j.created_at
  FROM wf_call_jobs j
 WHERE j.created_at >= NOW() - INTERVAL 1 DAY
   AND EXISTS (SELECT 1 FROM cdr_panel c
                WHERE c.calldate BETWEEN j.created_at - INTERVAL 5 MINUTE
                                     AND j.created_at + INTERVAL 5 MINUTE
                  AND c.dst LIKE CONCAT('%', RIGHT(j.lead_id, 6), '%'));
```

(Es una heurística sobre el CDR, no una prueba: el CDR no guarda
`lead_id`. Sirve para detectar, no para descartar.)
