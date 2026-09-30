# landmark-panel-multicountry-v2.2

Fundación multi-país V2.2 de Landmark Markets.

**NO ESTÁ INSTALADO. NO INSTALAR SOBRE `/opt/landmark-panel`.**
Se despliega en una copia (`/opt/landmark-panel-v22`) con su `.env`, puerto y base de
prueba. Ver `docs/PLAN_MIGRACION_V2_2.md` §F2.

## Contenido

    app/
      routes_config.py       países, proveedores/adapters, rutas, 3 interruptores, readiness, API
      call_jobs.py           claims, técnico vs negocio, una llamada en vuelo por lead
      ops_events.py          event store local idempotente + reconciliación
      analytics_v2.py        capa de consultas del dashboard (SQL local) + atribución
      followup_engine.py     referencia ejecutable del motor único
      server.py              +rutas nuevas (switches, analytics.json, API)
      templates/routes.html, country.html   nuevas
      analytics.py, charts.py, static, resto de templates: SIN CAMBIOS
    migrations/
      MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql   la única a aplicar
      legacy/…_V2_1.sql                             NO APLICAR · solo para el test de upgrade
    tests/                   5 suites + run_all.sh · 192 pruebas
    docs/                    contratos, estándar, analytics, auditoría Stringee, plan

## Probar

    ./tests/run_all.sh       # sqlite siempre; MariaDB si hay servidor (si no: SKIP)

Las suites de MariaDB CREAN Y BORRAN bases. **Nunca contra producción.**

## Variables

    LM_ROUTES_API_TOKEN   obligatoria · sin ella /api/* responde 503
