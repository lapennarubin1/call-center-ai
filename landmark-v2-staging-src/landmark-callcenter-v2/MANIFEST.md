# MANIFEST — Landmark Call Center V2 (r2-final2)

Paquete **de revisión final antes de staging**. Parche sobre la entrega
anterior; la arquitectura V2 sigue siendo la aprobada.

> **Nada de esto se ha desplegado.**
> No se ejecutó ninguna migración en producción, no se activó ningún
> workflow, no se tocó Asterisk, no se tocó el worker de Stringee, no se
> modificó ningún workflow en uso y no se cambió el modo de operación.
> Los 7 workflows viajan con `active: false`.

| | |
|---|---|
| Versión | V2.0 r2-final2 sobre FUNDACIÓN V2.2 |
| Fecha | 2026-09-21 |
| Motor validado | MariaDB 10.11.14 |
| Workflows | 7 · 299 nodos · 69 notas · 0 violaciones |
| Tests | **604 PASS · 0 FAIL · 0 SKIP** en 20 suites |
| · de los cuales, suite V2 | 412 PASS en 15 suites |
| · de los cuales, fundación V2.2 | 192 PASS en 5 suites |
| Secretos en claro | 0 (verificado en los 134 ficheros) |

---

## 1. Qué cambia en r2-final2

Parche pre-staging. La auditoría externa revisó el código de r2-final y
encontró que **el enclavamiento del modo estaba puesto tarde y fallaba
abierto**. Los seis puntos, y qué se hizo con cada uno:

| # | Lo que estaba mal | Qué se hizo |
|---|---|---|
| 1 | **El cerrojo se instalaba en `register()`**, mil líneas después de que `server.py` arrancara el hilo del planificador: había una ventana real en la que el barrido podía encender un grupo legacy conflictivo | Se instala **al importar `v2_suite`** (línea 30 de `server.py`), antes del hilo (línea 108). Sin editar `analytics.py`, que sigue byte a byte idéntico |
| 2 | **`ActivationBlocked` heredaba de `RuntimeError`**, y el panel captura `ValueError` alrededor de cada encendido: el bloqueo habría dado **HTTP 500** y habría roto el barrido en el primer horario bloqueado | `ModeError(ValueError)`. El guard correcto habría roto el panel mientras tenía razón |
| 3 | **Un modo ausente o corrupto se leía como `V2_PRIMARY`**, y WF2 asumía `V2_PRIMARY` ante un panel que no mandaba el campo | Falla cerrado en los dos lados: `UNKNOWN` → cero rutas y `OPERATING_MODE_UNKNOWN`. **No saber en qué modo está el sistema no es permiso para llamar** |
| 4 | **La migración dejaba `V2_PRIMARY`** apoyándose en que las rutas nacen apagadas: tres clics razonables bastaban para empezar a despachar sin cutover | El modo inicial es **`LEGACY_BACKUP`**. El cutover es un acto deliberado, verificado contra n8n |
| 5 | **Cuatro contraseñas reales vivían en `server.py`** como valor por defecto de `os.getenv`, y mi escáner reportaba 0 hallazgos | Ninguna contraseña tiene valor por defecto: si falta, **el panel no arranca** y dice cuál. El escáner aprendió las formas que se le escapaban, con control positivo |
| 6 | **`install.sh` reescribía el `.env` entero**: sobre una instalación en uso borraba `LM_MASTER_PASS`, `LM_SUPPORT_PASS`, `LM_ROUTES_API_TOKEN` y la configuración de n8n, y regeneraba `LM_SECRET` deslogueando a todos | El `.env` se **fusiona**, con respaldo previo. Una contraseña ya configurada no se resetea nunca |

Más: `panel/upgrade_callcenter_v2.sh` para actualizar el panel en el VPS sin
tocar datos, `PANEL_PRODUCTION_PATCH_MANIFEST.md` con los 24 ficheros nuevos
y los 4 modificados, y `tools/check_consistency.py`, que compara los recuentos
del MANIFEST contra los JSON y contra `TEST_RESULTS.json` para que ninguna
cifra de este documento se escriba a mano.

### Cómo se probó, y por qué esta vez es distinto

Los tres primeros defectos existían **con los tests en verde**, porque las
pruebas anteriores usaban un `run_due_schedules` reimplementado por mí. Ahora
`tests/test_real_interlock_v2.py` ejecuta el **`analytics.run_due_schedules()`
real** con el guard puesto y simula únicamente la llamada de red a n8n, y mete
la **respuesta real del panel** en el **nodo real de WF2**.

También se descubrió que la batería escondía una suite: `test_http_v2_2.py`
moría al importar el panel sin credenciales y no imprimía nada. `run_all.sh`
ahora cuenta como **fallo** cualquier suite que no llegue a imprimir su
resultado.

---

## 1b. Qué cambió en r2-final

Cuatro correcciones de la auditoría externa:

| # | Hallazgo | Qué se hizo |
|---|---|---|
| 1 | **El modo de operación era un cartel, no un enclavamiento** | Se aplica en tres sitios: un guard que envuelve la función que enciende grupos legacy (cubre manual, masivo y **programado**), `/api/routes/active` devolviendo cero rutas en `LEGACY_BACKUP`, y WF2 abortando antes de pedir leads. Volver a V2 **verifica contra n8n**; si no responde, bloquea |
| 2 | **El resumen del agente pasaba sin traducir al CRM** | Excepción eliminada. Un resumen sólo entra si la fuente **garantiza** que es inglés. Los errores crudos del proveedor pasan a ser **códigos**. El original queda como evidencia local |
| 3 | **Los KPI ignoraban el filtro** | `overview()`, business y accounts ahora filtran. Barra con 8 presets, From/To exactos y los tres filtros. Los enlaces de CSV serializan el recorte: imposible perder un filtro |
| 4 | **`link_legacy_provider()` no tenía UI** | Expuesto con su selector. Alta de proveedor en un solo flujo que crea o vincula su ficha de SIP Balance. Las fichas huérfanas se marcan `BILLING-ONLY`, nunca se duplican |

Más: PV-12 documentado con el puerto **8080** correcto, y la relación con el
8081 de staging explicada.

### Lo que este cerrojo NO cubre

El enclavamiento vive en el panel. Alguien que entre directamente a la
interfaz de n8n y active un workflow **se lo salta** — n8n no sabe qué es
el modo de operación. Está anotado como PV-26, con su mitigación.

---

## 1c. Qué cambió en r2 respecto de la entrega original

| Área | Qué se hizo |
|---|---|
| **Panel base** | Rebasado sobre `landmark-panel-safe`. 20 comprobaciones demuestran que los ficheros preservados son **byte a byte** idénticos y que las 41 rutas siguen registradas |
| **Facturación** | Una identidad de proveedor, 5 modelos, precio por ruta, coste efectivo etiquetado |
| **Legacy Backup** | Clasificación de grupos, modo de operación con enclavamiento y auditoría |
| **Pagos** | Se elimina el endpoint inventado. `DIRECT_PROVIDER` (OkPay real) y `UNIVERSAL_ROUTER` |
| **Analytics** | Los filtros se respetan en KPI, desglose por hora y CSV |
| **Seguridad** | Contraseña en claro quitada de `install.sh`; clave de OkPay nunca copiada; escáner reforzado |
| **Instalación** | Una instalación limpia ya no queda en condiciones de llamar |

---

## 2. Árbol

```
landmark-callcenter-v2/
├── MANIFEST.md                  ← este fichero
├── .gitignore
├── workflows/                   7 plantillas n8n, todas active:false
├── panel/                       panel REAL + los módulos de V2
├── sql/                         migración (6 partes), rollback, schema legacy
├── contracts/                   4 contratos normativos + contrato de build
├── tests/                       20 suites · 604 comprobaciones
├── tools/                       generador, escáner de secretos, empaquetador
└── docs/                        23 documentos
```

---

## 3. `workflows/` — las 7 plantillas

| Fichero | Nodos | Notas | Disparo |
|---|---:|---:|---|
| `TEMPLATE_FOLLOWUP_ENGINE_V2.json` | 60 | 11 | sub-workflow |
| `TEMPLATE_WF2_CALL_DISPATCHER_V2.json` | 58 | 14 | cron 1 min + `dispatch-now-v2` |
| `TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2.json` | 55 | 9 | `payment-link-v2`, `callback-request-v2`, `payment-callback-v2` |
| `TEMPLATE_WF3_ACCOUNT_CREATION_V2.json` | 38 | 9 | sub-workflow + `create-account-v2` |
| `TEMPLATE_WF10_RECORDINGS_V2.json` | 34 | 8 | cron 5 min |
| `TEMPLATE_WF9_POST_CALL_HANDLER_V2.json` | 27 | 8 | `elevenlabs-postcall-v2`, `stringee-callback-v2`, cron 2 min |
| `TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2.json` | 27 | 10 | cron 5 min |
| **TOTAL** | **299** | **69** | |

Orden de importación: FOLLOWUP_ENGINE → WF3 → WF2 → WF9 → WF7/8 → WF10 → WF14.
Todos los webhooks llevan sufijo `-v2`: ninguno pisa una ruta de v1.

---

## 4. `panel/` — el panel REAL con V2 dentro

Base: `landmark-panel-safe`. **Sólo tres ficheros divergen**, y un test lo
demuestra comparando huellas:

| Fichero | Cambio | Por qué |
|---|---|---|
| `app/server.py` | 3 líneas: `import v2_suite` + `register()` | enganche aditivo |
| `app/templates/base.html` | enlaces de navegación | aditivo |
| `deploy/install.sh` | quita la contraseña por defecto en claro | §61 |

`analytics.py`, `charts.py`, `panel.css` y las plantillas
`dashboard`, `callcenter`, `sip`, `extensions`, `support`,
`base_bare` son **byte a byte** las del panel real.

### Módulos nuevos de V2

| Fichero | Qué hace |
|---|---|
| `app/v2_suite.py` | registra las vistas y la API de V2 |
| `app/billing.py` | 5 modelos, coste efectivo, vínculo con SIP Balance |
| `app/legacy_mode.py` | modo de operación, clasificación y enclavamientos |
| `app/payments.py` | `OKPAY_V1` + router universal, validación por modo |
| `app/analytics_v2.py` | analítica local con filtros por país/proveedor/ruta |
| `app/wf_settings.py` | parámetros operativos, rechaza claves con pinta de secreto |
| `app/crm_notes.py` | las frases en inglés que van al CRM |
| `app/tool_requests.py`, `app/recording_ledger.py` | claims idempotentes |
| `app/templates/{billing,legacy,analytics,issues,settings}.html` | pantallas nuevas |

`panel/PANEL_BASE_CHECKSUMS.sha256` y `panel/PANEL_BASE_CONTRACT.json`
congelan el panel real: son contra lo que corre la prueba de no-regresión.
`panel/.env.example` es la plantilla de entorno, sin un solo valor real.

---

## 5. `sql/`

| Fichero | Qué es |
|---|---|
| `migration.sql` | **lo ejecutable**: 368 sentencias, las 6 partes |
| `parts/001_…` | fundación V2.2, **literal, sin un byte cambiado** |
| `parts/002_…` | suite V2: ajustes, grabaciones, tools |
| `parts/003_…` | tablas de v1 que WF14/WF10 leen. No-op donde ya están |
| `parts/004_billing_v2.sql` | facturación y vínculo con SIP Balance |
| `parts/005_legacy_backup_v2.sql` | clasificación legacy, modo, seguridad de instalación |
| `parts/006_payments_v2.sql` | los dos modos de pago, pedidos, corrección del seed |
| `rollback.sql` | informe + apagado reversible; lo destructivo **comentado** |
| `legacy/schema_v1_reference.sql` | el schema de v1, **saneado** (traía una contraseña en claro) |

Verificado contra MariaDB 10.11.14: **idempotente · re-ejecutable · no
destructiva**. Precios, cuotas, depósitos, switches, horarios y configuración
de país editados a mano sobreviven a una re-ejecución.

**No enciende nada.** Al contrario: en una instalación limpia deja países,
proveedores y rutas apagados. En una instalación **en uso** no toca nada.

---

## 6. `contracts/`

Los cuatro contratos normativos de la fundación, sin cambios, más
`SUITE_V2_BUILD_CONTRACT.json`, **generado desde los JSON entregados**:
workflows, webhooks, credenciales, entorno, endpoints del panel y tablas que
V2 escribe — y las que **nunca** escribe.

---

## 7. `tests/` — 604 comprobaciones

```bash
cd tests && ./run_all.sh              # legible
python3 run_all_report.py             # recuento + TEST_RESULTS.json
```

**15 suites del suite V2** — 412 comprobaciones:

| Suite | Checks | Qué prueba |
|---|---:|---|
| `test_engine_parity_v2.py` | 21 | motor Python ↔ JS, 19.320 casos con DST real |
| `test_package_hygiene_v2.py` | 21 | JSON, compilación, secretos, docs, SQL |
| `test_crm_english_v2.py` | 16 | toda cadena al CRM en inglés |
| `test_panel_base_regression_v2.py` | 21 | **el panel real no se tocó** (§2, §55, §68-70) |
| `test_billing_v2.py` | 32 | los 5 modelos, coste efectivo, SIP Balance intacto |
| `test_legacy_backup_v2.py` | 29 | Plan B entero, enclavamientos, auditoría |
| `test_payments_v2.py` | 31 | **paridad de firma a 3 vías** con el WF7 real |
| `test_workflow_standard_v2.py` | 24 | los 7 JSON cumplen el estándar |
| `test_workflow_sql_v2.py` | 27 | el SQL de los workflows hace PREPARE real |
| `test_suite_behaviour_v2.py` | 38 | claims, interruptores, ventanas, políticas |
| `test_migration_suite_v2.py` | 14 | idempotencia sobre MariaDB real |
| `test_analytics_filters_v2.py` | 19 | los filtros se respetan en KPI, hora y CSV |
| `test_interlock_v2.py` | 34 | el modo de operación, y el nodo real de WF2 leyéndolo |
| `test_real_interlock_v2.py` | 32 | **el camino real**: `analytics.run_due_schedules()` de verdad |
| `test_panel_v2.py` | 53 | pantallas y API de V2, permisos, sin red |

**5 suites de la fundación V2.2** — 192 comprobaciones, contra la copia del panel que va en el paquete:

| Suite | Checks | Qué prueba |
|---|---:|---|
| `test_foundation_v2_2.py` | 112 | fundación: config, interruptores, claims (sqlite + MariaDB) |
| `test_ops_analytics_v2_2.py` | 42 | fundación: eventos y analytics |
| `test_http_v2_2.py` | 15 | fundación: API, permisos, UI |
| `test_migration_v2_2.py` | 13 | fundación: migración sobre MariaDB |
| `test_concurrency_v2_2.py` | 10 | fundación: concurrencia y claims atómicos |

Ningún SKIP: se instaló MariaDB 10.11.14 para que todo corriera de
verdad. `run_all.sh` marca como FALLO cualquier suite que muera antes de
imprimir su resultado, para que un arranque roto no pase por silencio.

---

## 8. `tools/`

| Fichero | Qué es |
|---|---|
| `wfbuild.py` + `wf/*.py` + `build_workflows.py` | reconstruyen los 7 JSON |
| `js/lmcore.js`, `lmengine.js`, `lmnotes.js`, `lmpay.js` | librerías de los nodos Code; gemelas 1:1 de sus módulos Python |
| `secret_scan.py` | escáner de secretos, con control positivo |
| `make_package.py` | construye el zip **y lo verifica sobre el zip escrito** |

---

## 9. `docs/` — 23 documentos

**De la fundación (sin cambios):** `ARQUITECTURA_CORRECCIONES_V2_2.md` ·
`N8N_TEMPLATE_STANDARD_V2_2.md` · `ANALYTICS_ARCHITECTURE_V2_2.md` ·
`STRINGEE_WORKER_AUDIT_V2_2.md` · `TEST_PLAN_FOUNDATION_V2_2.md` ·
`PLAN_MIGRACION_V2_2.md`

**Escritos para V2:** `ARCHITECTURE_FINAL_V2.md` · `INSTALL_STAGING.md` ·
`PRODUCTION_DEPLOYMENT_PLAN.md` · `ROLLBACK_PLAN.md` · `CONFIGURATION_GUIDE.md` ·
`ADD_COUNTRY_GUIDE.md` · `ADD_PROVIDER_GUIDE.md` · `ADD_ROUTE_GUIDE.md` ·
`FOLLOWUP_POLICY_GUIDE.md` · `CRM_ENGLISH_RULE.md` · `ANALYTICS_GUIDE.md` ·
`STRINGEE_MINIMAL_PATCH_PROPOSAL.md` · `PENDING_VERIFICATION.md`

**Nuevos en r2:** `BILLING_GUIDE.md` · `PAYMENT_INTEGRATION_GUIDE.md` ·
`LEGACY_BACKUP_GUIDE.md` · `LEGACY_V2_COMPATIBILITY_MATRIX.md`

---

## 10. Lo que este paquete NO trae, a propósito

- **Ningún despliegue.** Nada se instaló en ningún sitio.
- **Ningún cambio en Stringee ni en Asterisk.** El parche mínimo está
  *descrito* y *no aplicado*.
- **Ningún secreto.** Ni la clave de firma de OkPay, ni la contraseña de
  `panel_rw`, ni ninguna otra.
- **Ningún borrado.** Rutas usadas se archivan; SIP Balance, los switches
  legacy y sus horarios quedan intactos.
- **Extensions y Support sin tocar.** Fuera de alcance, y hay tests que lo
  demuestran.
- **Gestión de agentes de ElevenLabs.** Se configura en ElevenLabs.

---

## 11. Por dónde empezar

1. `docs/PENDING_VERIFICATION.md` — **primero**: 2 puntos rojos antes de
   staging y 2 más antes de activar pagos.
2. `docs/ARCHITECTURE_FINAL_V2.md` — qué se construyó y por qué.
3. `docs/LEGACY_V2_COMPATIBILITY_MATRIX.md` — antes de tocar el modo.
4. `docs/INSTALL_STAGING.md` — la instalación, paso a paso.
