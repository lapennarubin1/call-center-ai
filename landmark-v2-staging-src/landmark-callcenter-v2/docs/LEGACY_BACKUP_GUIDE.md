# Guía de Legacy Backup / Plan B

El call center actual sigue instalado, entero y utilizable. No es
documentación de lo que hubo: es el plan B operativo.

---

## 1. Qué es el modo legacy

Dos sistemas capaces de llamar viven en la misma máquina:

- **V2** — los 7 workflows nuevos, gobernados por `call_routes`
- **Legacy** — los grupos de workflows que hay hoy en `n8n_switches`

El **modo de operación** dice cuál manda:

| Modo | Significa |
|---|---|
| `V2_PRIMARY` | V2 es el sistema principal |
| `LEGACY_BACKUP` | manda el call center anterior — **y es el modo con el que se instala** |

`V2_PRIMARY` **no** significa que V2 esté llamando. Significa que V2 es
quien tiene permitido llamar cuando se encienda: hace falta además encender
país, proveedor y ruta.

`LEGACY_BACKUP` sí significa lo contrario: **V2 no llama, pase lo que pase
con esos tres interruptores.**

### El modo inicial es `LEGACY_BACKUP`

Recién instalado, el sistema **no despacha por V2**. Hay que hacer el
cutover a mano (§5), y el panel lo verifica contra n8n antes de dejarlo.

Antes la migración dejaba `V2_PRIMARY`, apoyándose en que todas las rutas
nacen apagadas. Eso confundía dos cosas distintas —"V2 manda" y "V2 todavía
no tiene nada encendido"— y bastaba que alguien diera tres clics
perfectamente razonables en la pantalla de países para que el suite
empezara a llamar sin que nadie hubiera decidido el cambio, quizá con el
call center viejo despachando a la vez.

### Hay un tercer valor, y no es un modo: `UNKNOWN`

Si la columna del modo falta, está vacía o tiene un valor que no es ninguno
de los dos, el panel responde `UNKNOWN` y se comporta como si estuviera
bloqueado: cero rutas invocables y `error_code: OPERATING_MODE_UNKNOWN`.
WF2 lo lee y aborta.

**No saber en qué modo está el sistema no es permiso para llamar.** Un
panel anterior, que no conoce el modo, tampoco autoriza: WF2 exige
`operating_mode: V2_PRIMARY` **y** `dispatch_allowed: true`, y sin las dos
cosas no despacha.

---

## 2. Qué se conserva

Todo. La migración no borra, no trunca y no altera ninguna tabla del
legacy:

| | |
|---|---|
| `n8n_switches` | grupos, etiquetas, `workflow_ids` |
| `n8n_switch_schedules` | zona horaria, hora de encendido y apagado, días, estado |
| estados ON/OFF | viven en n8n, no se tocan |
| la pantalla Call Center | byte a byte idéntica |
| `Turn ON all`, `Turn OFF all`, `Edit`, `Schedule` | funcionan igual |

Lo único que se añade es una tabla **aparte**,
`legacy_group_classification`, que apunta a los grupos sin modificarlos.

Hay un test que recorre toda la migración buscando `DROP`, `TRUNCATE`,
`DELETE` o `ALTER` sobre esas tablas y falla si aparece alguno.

---

## 3. Clasificar los grupos

**Call Center → Legacy Backup**, sólo `master`.

Cada grupo se clasifica en una categoría, y la categoría decide si puede
convivir con V2 encendido. El detalle está en
`LEGACY_V2_COMPATIBILITY_MATRIX.md`.

Resumen: lo que **llama, agenda, abre cuentas, cobra o cuenta llamadas**
no convive. Lo que **sólo lee o es idempotente** sí.

Un grupo nuevo entra como `UNKNOWN` y **bloquea el cambio de modo** hasta
que alguien lo clasifique. Es a propósito: es preferible parar a alguien
que asumir que un workflow desconocido es inofensivo.

---

## 4. Pasar a LEGACY_BACKUP

**Antes:** V2 no puede estar en condiciones de llamar.

El panel lo comprueba con los tres interruptores más el archivado — los
mismos que usa WF2. Una ruta encendida bajo un país apagado **no** cuenta
como viva, así que no hace falta apagarla también.

1. Call Center → Countries: apagar las rutas, proveedores o países que la
   pantalla liste como vivos
2. Call Center → Legacy Backup: confirmar que *V2 routes able to call
   right now* dice **none**
3. Teclear `SWITCH TO LEGACY`
4. Escribir el motivo
5. Cambiar

**Después:** los workflows legacy siguen **apagados**. El cambio de modo
no los enciende. Hay que encenderlos en Call Center, con los controles de
siempre.

Si V2 sigue vivo, el panel se niega y **nombra las rutas** que lo
bloquean. No cambia nada a medias.

---

## 5. Volver a V2_PRIMARY — y el PRIMER cutover

Este mismo procedimiento sirve para las dos cosas: volver a V2 tras un
episodio de Plan B, y hacer el **cutover inicial** justo después de
instalar, que es el que pasa el sistema de `LEGACY_BACKUP` —el modo con
el que nace— a `V2_PRIMARY`. No hay un camino distinto ni más corto para
la primera vez: la comprobación contra n8n es exactamente la que hace
falta la primera vez.

**Antes:** los grupos legacy conflictivos tienen que estar en OFF.

1. Call Center → Legacy Backup: leer la lista de grupos a apagar
2. Call Center: apagarlos
3. Leer también la lista de los que **sí** pueden seguir corriendo, para
   no apagar de más
4. Teclear `SWITCH TO V2`
5. Escribir el motivo
6. Cambiar
7. Encender las rutas V2 que toquen, una a una

**El panel verifica el paso 2 por ti.** Consulta a n8n el estado real de
cada workflow (`n8n_switches_with_status`) y bloquea el cambio si alguno
conflictivo sigue encendido, nombrándolo.

Si n8n no responde, **también bloquea**. No saber si el despachador viejo
está encendido no es lo mismo que saber que está apagado, y tratarlo igual
es exactamente cómo se acaba llamando dos veces al mismo cliente.

---

## 6. Los horarios se conservan

`n8n_switch_schedules` no se toca en ningún momento: ni al ir a legacy,
ni al volver, ni al re-ejecutar la migración. Zona horaria, hora de
encendido, hora de apagado, días y estado siguen donde estaban.

Ir a `LEGACY_BACKUP` y volver a `V2_PRIMARY` deja el legacy exactamente
como estaba. Hay un test que lo comprueba haciendo el viaje de ida y
vuelta.

---

## 7. Auditoría

Cada cambio de modo escribe en `billing_audit` con `scope = 'MODE'`:
quién, cuándo, desde qué modo, hacia cuál, y el motivo.

Un intento fallido —rol incorrecto, frase mal escrita, V2 todavía vivo—
**no** cambia el modo y **no** escribe auditoría de cambio. La auditoría
registra lo que pasó, no lo que alguien intentó.

Se ve al pie de la pantalla, y por API en `GET /api/legacy/mode.json`.

---

## 8. Permisos

Sólo `master` puede cambiar el modo o clasificar grupos. Cualquier otro
rol recibe 403. Es el mismo modelo de acceso del resto del panel; no se
añadió una segunda forma de autenticarse.

---

## 9. Comprobar que no hay duplicados

Después de cualquier cambio de modo, antes de dar por cerrada la
operación:

```sql
-- ¿Qué rutas V2 pueden llamar ahora mismo?
SELECT r.route_key
  FROM call_routes r
  JOIN voice_providers vp ON vp.id = r.provider_id
  JOIN countries c ON c.iso = r.iso
 WHERE r.enabled = 1 AND r.archived_at IS NULL
   AND vp.enabled = 1 AND c.enabled = 1 AND c.archived_at IS NULL;
```

Y en Call Center, confirmar el estado de los grupos conflictivos.

Si en `LEGACY_BACKUP` esa consulta devuelve filas, **los dos sistemas
están habilitados para llamar**. Apagar las rutas inmediatamente.

---

## 10. Rollback

El plan B y el rollback son la misma cosa vista desde dos sitios:

1. `sql/rollback.sql` **Parte A** apaga V2 sin borrar nada
2. Desactivar los 7 workflows V2 en n8n
3. Si hay que volver a llamar: modo `LEGACY_BACKUP` y encender los grupos
4. Las partes destructivas del rollback están comentadas a propósito

Detalle en `ROLLBACK_PLAN.md`.

---

## 11. El modo es un enclavamiento, no un cartel

Desde r2-final el modo se **aplica**, en tres sitios:

| Dónde | Qué hace |
|---|---|
| `guard_legacy_activation` | envuelve la función que enciende grupos legacy. Cubre el encendido manual, el masivo y el **programado**, porque los tres pasan por ella |
| `/api/routes/active` | en `LEGACY_BACKUP` —o si el modo es `UNKNOWN`— devuelve **cero** rutas invocables y lo dice |
| WF2 | exige `operating_mode: V2_PRIMARY` **y** `dispatch_allowed: true`, y termina **antes** de pedir leads si falta cualquiera de las dos |

### Cuándo se instala el cerrojo (r2-final2)

Importa más de lo que parece. Antes se instalaba cuando el panel registraba
las vistas de V2, unas mil líneas después de que `server.py` arrancara el
hilo del planificador: existía una ventana real en la que un horario podía
encender un grupo conflictivo sin pasar por el cerrojo.

Ahora se instala **al importar `panel/app/v2_suite.py`**, que ocurre en la
línea 30 de `server.py`; el hilo arranca en la 108. El cerrojo existe antes
de que haya nada capaz de encender un workflow.

El otro cambio es el **tipo de la excepción**. El panel que ya existía
captura `ValueError` alrededor de cada encendido, así que un bloqueo que
fuese `RuntimeError` habría salido como **HTTP 500** y habría roto el
barrido del planificador en el primer horario bloqueado. `ModeError` hereda
de `ValueError` a propósito: el guard tiene que negarse **como el panel
espera que se nieguen las cosas**, o rompe lo que intenta proteger.

En `V2_PRIMARY`, un grupo `DISPATCH`, `FOLLOWUP`, `ACCOUNT`, `PAYMENT`,
`POST_CALL` o `UNKNOWN` **no se puede encender**. El intento se rechaza y
queda auditado; si venía de un horario, el propio horario guarda
`Scheduled activation blocked: operating mode is V2_PRIMARY.`

En `LEGACY_BACKUP`, encender país, proveedor y ruta **no** produce una ruta
invocable. Editar configuración sigue permitido, y una ruta puede figurar
READY. Lo que no puede es llamar.

**Apagar nunca se bloquea**, en ningún modo. Un OFF jamás crea una llamada
duplicada.

## 12. Lo que este circuito sigue sin garantizar

- **No impide** encender un workflow legacy directamente desde la interfaz
  de n8n, saltándose el panel. El enclavamiento vive en el panel; n8n no
  sabe qué es el modo de operación.
- **No cancela** trabajo en vuelo. Llamadas ya despachadas terminan su
  post-call; pedidos de pago ya creados siguen siendo cobros válidos del
  lado de la pasarela.

Antes de cambiar de modo conviene mirar
`SELECT ... FROM wf_call_jobs WHERE state IN ('CLAIMED','DISPATCHING','DISPATCHED')`
y dejar que WF14 reconcilie lo que quede a medias.
