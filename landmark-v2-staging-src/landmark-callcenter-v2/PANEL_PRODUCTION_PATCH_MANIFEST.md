# PANEL_PRODUCTION_PATCH_MANIFEST

**Qué hay que copiar al panel de producción, exactamente.**

Generado por `tools/make_patch_manifest.py` comparando este paquete
contra `panel/PANEL_BASE_CHECKSUMS.sha256`, que son las huellas del
`landmark-panel-safe.tar.gz` — el panel que corre hoy en producción.

> **No se escribe a mano.** Un inventario de despliegue escrito a mano
> envejece en la primera modificación, y nadie se entera hasta que el
> panel no arranca en producción.

---

## Resumen

| | Ficheros |
|---|---:|
| 🆕 **NUEVOS** — no existen en producción | **24** |
| ✏️ **MODIFICADOS** — existen y cambian | **4** |
| ✅ **SIN CAMBIOS** — byte a byte idénticos | **15** |
| | **43** |

> El panel de producción **no tiene** los módulos de la Fundación V2.
> Un plan que diga «sólo cambian unas líneas de `server.py`» es falso:
> hay **24 ficheros nuevos** que copiar. Copiar `server.py`
> sin ellos deja el panel sin arrancar, porque importa módulos que no
> existen.

---

## 1. NUEVOS — 24 ficheros

No existen en el panel de producción. **Hay que copiarlos todos.**

| Fichero | Qué aporta |
|---|---|
| `.env.example` | plantilla de entorno, sin valores reales. Referencia, no se copia sobre el .env |
| `PANEL_BASE_CONTRACT.json` | las rutas y funciones del panel real, congeladas. Lo usa la prueba de no-regresión |
| `README_PANEL_REAL.md` | el README del panel real, conservado tal cual |
| `app/analytics_v2.py` | analítica local con filtros por país/proveedor/ruta |
| `app/billing.py` | modelos de facturación y vínculo con SIP Balance |
| `app/call_jobs.py` | claim atómico de llamadas y máquina de estados |
| `app/crm_notes.py` | las frases en inglés que van al CRM |
| `app/followup_engine.py` | política de reintentos, provider-neutral |
| `app/legacy_mode.py` | modo de operación, clasificación legacy y guard |
| `app/ops_events.py` | eventos de negocio idempotentes |
| `app/payments.py` | adaptadores de pago: OkPay y router universal |
| `app/recording_ledger.py` | correlación y claim de grabaciones |
| `app/routes_config.py` | países, proveedores, rutas, validación y el cerrojo del modo |
| `app/templates/analytics.html` | analítica V2, con la barra de filtros |
| `app/templates/billing.html` | facturación y vínculo con SIP Balance |
| `app/templates/country.html` | ficha de un país, con validación READY |
| `app/templates/issues.html` | issues de reconciliación |
| `app/templates/legacy.html` | modo de operación y Legacy Backup |
| `app/templates/routes.html` | pantalla de países y rutas |
| `app/templates/settings.html` | parámetros operativos |
| `app/tool_requests.py` | claim idempotente de las tools de país |
| `app/v2_suite.py` | registra las vistas y la API de V2; instala el cerrojo |
| `app/wf_settings.py` | parámetros operativos del suite |
| `upgrade_callcenter_v2.sh` | actualiza el panel EN EL VPS sin tocar .env, .session_key, venv, logs ni datos. Valida que el panel importa antes de reemplazar nada, y con DRY_RUN=1 sólo enseña qué haría |

---

## 2. MODIFICADOS — 4 ficheros

Existen en producción y **cambian**. Hacer copia antes de sustituir.

### `README.md`

Es el README de la FUNDACIÓN, no una modificación del que hay en producción. El original viaja intacto como README_PANEL_REAL.md. No hace falta copiarlo.

### `app/server.py`

Tres cambios, todos acotados: (1) import de v2_suite y llamada a v2.register() al final — el enganche de V2; (2) las cuatro contraseñas que estaban escritas en el código pasan a ser obligatorias por entorno; (3) se añade LM_DB_PORT. Las 41 rutas originales siguen todas, y hay un test que lo comprueba.

### `app/templates/base.html`

Añade los enlaces de navegación de V2. Los 8 originales siguen.

### `deploy/install.sh`

La contraseña de la BD dejaba de venir en claro, y el .env pasa a FUSIONARSE en vez de sobrescribirse: antes borraba LM_MASTER_PASS, LM_SUPPORT_PASS, LM_N8N_* y LM_ROUTES_API_TOKEN en cada pasada.

---

## 3. SIN CAMBIOS — 15 ficheros

Byte a byte idénticos a producción. **No hace falta copiarlos**, y
copiarlos tampoco rompe nada.

- `app/analytics.py`
- `app/charts.py`
- `app/static/panel.css`
- `app/templates/base_bare.html`
- `app/templates/callcenter.html`
- `app/templates/dashboard.html`
- `app/templates/error.html`
- `app/templates/extensions.html`
- `app/templates/login.html`
- `app/templates/sip.html`
- `app/templates/support.html`
- `deploy.sh`
- `deploy/WF14_Sync_Panel.json`
- `deploy/WF14_Sync_Panel_v2.json`
- `deploy/requirements.txt`

---

## 4. Cómo aplicarlo

No a mano. `panel/upgrade_callcenter_v2.sh` hace exactamente esto,
con respaldo, validación previa al reinicio y modo de prueba:

```bash
sudo DRY_RUN=1 bash panel/upgrade_callcenter_v2.sh   # enseña qué haría
sudo bash panel/upgrade_callcenter_v2.sh             # lo hace
```

## 5. Lo que este parche NO toca

- el `.env` (se conserva y se respalda)
- el virtualenv
- logs y ficheros de ejecución
- los datos de Extensions y Support
- los grupos de workflows legacy y sus horarios
- la base de datos: el SQL va aparte y es una decisión separada

