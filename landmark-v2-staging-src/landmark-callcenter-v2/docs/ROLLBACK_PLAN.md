# ROLLBACK_PLAN.md
**Cómo volver atrás, en orden de menos a más invasivo.**
Fase BUILD · 21/09/2026

---

## 0. Lo primero

> **El rollback normal es apagar una ruta desde el panel.** No hay que revertir
> un despliegue, ni restaurar un backup, ni tocar n8n.

La migración es **aditiva**: no modifica ninguna tabla de v1. Volver a v1 **no
requiere tocar el esquema**.

| nivel | qué se hace | cuánto tarda | qué se pierde |
|---|---|---|---|
| **1** | apagar la ruta en el panel | segundos | nada |
| **2** | apagar el país o el proveedor | segundos | nada |
| **3** | desactivar los workflows V2 en n8n | 1 min | nada |
| **4** | reactivar v1 | 1 min | nada |
| **5** | desinstalar el panel V2 | 5 min | las pantallas nuevas |
| **6** | borrar el esquema V2 | — | **el historial de V2** ⚠️ |

Del 1 al 4 son **reversibles y no destructivos**. El 6 no se hace nunca en
producción.

---

## 1. Nivel 1 — apagar una ruta (lo habitual)

**Panel → Countries & Routes → la ruta → Disable.**

Efecto: en el ciclo siguiente (< 1 min), `/api/routes/active` deja de devolverla
y WF2 no la despacha.

Qué pasa con lo que estaba en vuelo:

- las llamadas ya despachadas **siguen su curso**: el post-call llega, el motor
  crea el follow-up y el lead queda con su `scheduleNextAt` correcto
- nada queda a medias por apagar la ruta

Verificación:

```sql
SELECT r.route_key, r.enabled, c.enabled AS country, p.enabled AS provider
  FROM call_routes r
  JOIN countries c ON c.iso = r.iso
  JOIN voice_providers p ON p.id = r.provider_id
 WHERE r.enabled = 1 AND c.enabled = 1 AND p.enabled = 1 AND r.archived_at IS NULL;
```

---

## 2. Nivel 2 — apagar un país o un proveedor

- **un proveedor falla** (credenciales, caída) → **Providers → Disable**: paran
  todas sus rutas, en todos los países
- **un país hay que frenarlo entero** → **Countries → Disable**: paran todas sus
  rutas, de cualquier proveedor

Es el mismo mecanismo, con más alcance. Sin efectos secundarios.

---

## 3. Nivel 3 — desactivar los workflows V2

Si el problema no es de una ruta sino del suite:

```
n8n → desactivar, en este orden:
  1. TEMPLATE_WF2_CALL_DISPATCHER_V2      ← primero: deja de llamar
  2. TEMPLATE_WF3 / TEMPLATE_WF7_8
  3. TEMPLATE_WF10_RECORDINGS_V2
  4. TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2
  5. TEMPLATE_WF9_POST_CALL_HANDLER_V2    ← al final: que termine de procesar
  6. TEMPLATE_FOLLOWUP_ENGINE_V2
```

**WF9 se desactiva último a propósito:** las llamadas ya despachadas todavía van
a recibir su post-call. Apagarlo antes deja esos resultados sin registrar y los
leads en vuelo.

Antes de apagar WF9, esperar a que no queden llamadas activas:

```sql
SELECT COUNT(*) FROM wf_call_jobs WHERE state IN ('DISPATCHING','DISPATCHED');
```

---

## 4. Nivel 4 — reactivar v1

```sql
-- confirmar los valores reales primero
SELECT * FROM wf2_provider_config;

UPDATE wf2_provider_config SET enabled = 1 WHERE provider IN ('asterisk','stringee');
```

Y en n8n, reactivar los workflows v1 (que **nunca se borraron**).

**Antes de reactivar v1, confirmar que V2 no está llamando.** Los dos sistemas
sobre la misma ruta llaman al mismo lead dos veces: v1 no usa el claim de V2.

```sql
SELECT COUNT(*) AS rutas_v2_activas FROM call_routes r
  JOIN countries c ON c.iso = r.iso
  JOIN voice_providers p ON p.id = r.provider_id
 WHERE r.enabled=1 AND c.enabled=1 AND p.enabled=1 AND r.archived_at IS NULL;
-- debe dar 0
```

---

## 5. Plan B: el panel no está disponible

Si hay que frenar y el panel no responde, `sql/rollback.sql` **PARTE A** hace lo
mismo por SQL:

```bash
mysql -u root -p asterisk < sql/rollback.sql
```

Qué hace:

1. **PARTE 0** (solo lectura): informe de cuántas filas hay y qué llamadas están
   en vuelo
2. **PARTE A**: apaga **todas** las rutas y **todos** los países, y lo registra
   en `route_audit` con actor `rollback.sql`
3. verifica que no quede ninguna combinación llamando

Es **reversible**: volver a encender es un `UPDATE` o dos clics en el panel.

> Preferir el panel cuando esté disponible: deja auditoría con el usuario real.

---

## 6. Nivel 5 — desinstalar el panel V2

Solo si las pantallas nuevas causan un problema (no debería: son aditivas).

```bash
sudo tar xzf /backup/landmark-panel_<fecha>.tar.gz -C /
sudo systemctl restart landmark-panel
```

O, quirúrgicamente, revertir los dos hooks:

```python
# app/server.py — quitar estas dos líneas
import v2_suite as v2
v2.register(app, auth_master, auth_service_token, with_db)
```

Las tablas y los datos quedan intactos.

---

## 7. Nivel 6 — borrar el esquema V2 ⚠️

**No se hace en producción.** Está escrito y **comentado** en `sql/rollback.sql`
PARTE B, solo para desinstalar el suite de un staging.

Destruye: el historial de llamadas de V2 (`wf_call_jobs`), los eventos de
analytics (`wf_events`), los ledgers de idempotencia y **toda la configuración
multi-país del panel**.

Si alguna vez hiciera falta:

1. ejecutar la **PARTE 0** y guardar la salida
2. `mysqldump` de las 13 tablas
3. confirmar que no hay llamadas en vuelo
4. recién ahí descomentar

---

## 8. Escenarios concretos

### "Un lead recibió dos llamadas seguidas"

**Causa casi segura:** v1 y V2 activos sobre la misma ruta.

1. apagar la ruta en el panel (**nivel 1**)
2. confirmar en `wf2_provider_config` si v1 sigue habilitado para ese proveedor
3. decidir cuál de los dos se queda y apagar el otro
4. `SELECT lead_id, attempt, call_job_id, created_at FROM wf_call_jobs
   WHERE lead_id = '<lead>' ORDER BY created_at`

### "Se crearon follow-ups duplicados en el CRM"

1. **parar todo** (**nivel 3**)
2. `SELECT conversation_id, COUNT(*) FROM wf_conversation_ledger
   GROUP BY conversation_id HAVING COUNT(*) > 1` → debería dar **cero**
3. si da cero, los duplicados vienen de **v1**, no de V2
4. revisar `wf_reconciliation_issues` de tipo `FOLLOWUP_NEEDS_RECONCILIATION`

### "El proveedor rechaza todo (401)"

**No es un rollback.** Es el circuito técnico funcionando: los jobs van a
`RELEASED`, **no consumen intento de negocio** y reintentan con backoff.

1. arreglar la credencial en n8n
2. los intentos liberados se retoman solos con el mismo número
3. si quedaron con el backoff agotado:
   `SELECT COUNT(*) FROM wf_call_jobs WHERE state='RELEASED' AND tech_retry_count >= 8`

### "Muchos NEEDS_RECONCILIATION"

1. **nivel 1** sobre la ruta afectada
2. `/callcenter/issues`
3. la causa habitual es un proveedor con timeouts: la llamada **pudo** salir y
   el sistema —correctamente— se niega a re-despachar
4. resolver a mano y recién después reactivar

### "El panel muestra números raros"

Los KPIs salen de `wf_call_jobs` y `wf_events`, no del CRM. No hay nada que
"resincronizar".

```sql
SELECT state, result, COUNT(*), SUM(duration_seconds)
  FROM wf_call_jobs
 WHERE created_at > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)
 GROUP BY state, result;
```

Si esos números son correctos y la pantalla no, es la pantalla. Si son
incorrectos, es un workflow.

---

## 9. Lo que el rollback NO puede deshacer

| | por qué |
|---|---|
| llamadas ya hechas | el cliente ya atendió el teléfono |
| follow-ups ya creados en LeadStudio | el CRM es de v1 y de V2; el suite no borra follow-ups |
| cuentas ya creadas en CashStudio | son reales |
| links de pago ya enviados | el cliente los tiene |
| mensajes ya enviados a Telegram | — |

Por eso el despliegue es **una ruta por vez y con capacidad 1** al principio:
para que lo irreversible sea siempre poquito.
