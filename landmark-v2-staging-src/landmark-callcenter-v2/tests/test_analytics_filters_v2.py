#!/usr/bin/env python3
"""Analytics V2: los filtros se respetan de verdad (§30-33, §56-58).

Lo que se defiende:
  · el Breakdown by Hour muestra EXACTAMENTE el recorte seleccionado (§32)
  · el CSV exportado coincide con lo que se está viendo (§33)
  · los números salen de MySQL local, sin tocar LeadStudio (§57)
  · la atribución se declara como convención, no como hecho (§58)

Se siembran llamadas de tres países, dos proveedores y cuatro rutas, con
horas distintas, y se comprueba que cada filtro devuelve la suma correcta
—no simplemente que la consulta no dé error.
"""
import datetime as dt
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, mysql_available, migrated_db,        # noqa: E402
                    mysql_conn, DB)

import analytics_v2 as av2                                            # noqa: E402

# (país, proveedor, ruta, hora UTC, resultado, segundos)
SEED = [
    ('IN', 'proveedor1', 'IN_PROVEEDOR1',  9, 'ANSWERED',   120),
    ('IN', 'proveedor1', 'IN_PROVEEDOR1',  9, 'NO_ANSWER',  None),
    ('IN', 'proveedor1', 'IN_PROVEEDOR1', 14, 'ANSWERED',   300),
    ('IN', 'stringee',   'IN_STRINGEE',    9, 'ANSWERED',    60),
    ('IN', 'stringee',   'IN_STRINGEE',   14, 'NO_ANSWER',  None),
    ('IN', 'stringee',   'IN_STRINGEE',   14, 'BUSY',       None),
    ('NP', 'proveedor1', 'NP_PROVEEDOR1',  9, 'ANSWERED',    90),
    ('NP', 'proveedor1', 'NP_PROVEEDOR1', 20, 'NO_ANSWER',  None),
    ('MX', 'proveedor1', 'MX_PROVEEDOR1', 20, 'ANSWERED',   240),
]


def seed(db, day):
    for i, (iso, prov, route, hora, res, secs) in enumerate(SEED):
        ts = day.replace(hour=hora, minute=0, second=0, microsecond=0)
        db.execute("""INSERT INTO wf_call_jobs
              (call_job_id, lead_id, route_id, route_key, country_iso, provider,
               adapter_key, attempt, state, result, duration_seconds,
               created_at, dispatched_at, completed_at)
            VALUES (§,§,§,§,§,§,'ELEVENLABS_SIP',1,'COMPLETED',§,§,§,§,§)""",
            (f'cj-{i}', f'lead-{i}', 1 + (i % 4), route, iso, prov, res, secs,
             ts.strftime('%Y-%m-%d %H:%M:%S'), ts.strftime('%Y-%m-%d %H:%M:%S'),
             ts.strftime('%Y-%m-%d %H:%M:%S')))


def total(metrics):
    """call_metrics sin group_by devuelve ya el dict agregado."""
    return metrics or {}


def main():
    s = Suite('analytics V2 · filtros y export')

    if not mysql_available():
        s.skip('analytics V2', 'sin MariaDB')
        return s.finish()

    NAME = 'lm_test_analytics'
    migrated_db(NAME)
    db = DB(mysql_conn(NAME))
    hoy = dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    seed(db, hoy)
    start = hoy - dt.timedelta(days=1)
    end = hoy + dt.timedelta(days=1)

    s.section('el filtro filtra de verdad')

    def no_filter_is_everything():
        m = total(av2.call_metrics(db, start, end))
        assert m['attempted'] == len(SEED), \
            f"sin filtro deberían ser {len(SEED)} llamadas, son {m['attempted']}"
    s.check(f'sin filtro se cuentan las {len(SEED)} llamadas', no_filter_is_everything)

    def country_filter():
        m = total(av2.call_metrics(db, start, end, filters={'country': 'IN'}))
        esperado = sum(1 for r in SEED if r[0] == 'IN')
        assert m['attempted'] == esperado, \
            f'filtrando India deberían ser {esperado}, son {m["attempted"]}'
        assert m['attempted'] < len(SEED), 'el filtro de país no recorta nada'
    s.check('filtrar por país devuelve sólo ese país', country_filter)

    def provider_filter():
        m = total(av2.call_metrics(db, start, end, filters={'provider': 'stringee'}))
        esperado = sum(1 for r in SEED if r[1] == 'stringee')
        assert m['attempted'] == esperado, \
            f'filtrando Stringee deberían ser {esperado}, son {m["attempted"]}'
    s.check('filtrar por proveedor devuelve sólo ese proveedor', provider_filter)

    def route_filter():
        m = total(av2.call_metrics(db, start, end, filters={'route': 'IN_STRINGEE'}))
        esperado = sum(1 for r in SEED if r[2] == 'IN_STRINGEE')
        assert m['attempted'] == esperado
    s.check('filtrar por ruta devuelve sólo esa ruta', route_filter)

    def combined_filter():
        m = total(av2.call_metrics(db, start, end,
                                   filters={'country': 'IN', 'provider': 'proveedor1'}))
        esperado = sum(1 for r in SEED if r[0] == 'IN' and r[1] == 'proveedor1')
        assert m['attempted'] == esperado, \
            f'país+proveedor deberían ser {esperado}, son {m["attempted"]}'
    s.check('país y proveedor se combinan con AND', combined_filter)

    def multi_value_filter():
        m = total(av2.call_metrics(db, start, end, filters={'country': ['IN', 'NP']}))
        esperado = sum(1 for r in SEED if r[0] in ('IN', 'NP'))
        assert m['attempted'] == esperado, \
            f'IN+NP deberían ser {esperado}, son {m["attempted"]}'
    s.check('un filtro con varios valores suma los dos', multi_value_filter)

    def talk_minutes_respect_filter():
        m = total(av2.call_metrics(db, start, end, filters={'provider': 'proveedor1'}))
        esperado = sum(r[5] or 0 for r in SEED
                       if r[1] == 'proveedor1' and r[4] in ('ANSWERED', 'CALLBACK'))
        assert m['talk_seconds'] == esperado, \
            f'segundos hablados con filtro: esperado {esperado}, dio {m["talk_seconds"]}'
    s.check('los minutos hablados también respetan el filtro',
            talk_minutes_respect_filter)

    # ══ Breakdown by Hour · §32 ══════════════════════════════════════
    s.section('Breakdown by Hour respeta los filtros (§32)')

    def hourly_unfiltered():
        t = av2.timeseries(db, start, end, 'hour', 'calls')
        horas = {r['bucket'][-8:-6]: r['value'] for r in t}
        for h in ('09', '14', '20'):
            esperado = sum(1 for r in SEED if f'{r[3]:02d}' == h)
            assert horas.get(h) == esperado, \
                f'hora {h}: esperado {esperado}, dio {horas.get(h)}'
    s.check('sin filtro, cada hora suma sus llamadas', hourly_unfiltered)

    def hourly_respects_country():
        t = av2.timeseries(db, start, end, 'hour', 'calls', filters={'country': 'IN'})
        horas = {r['bucket'][-8:-6]: r['value'] for r in t}
        for h in ('09', '14'):
            esperado = sum(1 for r in SEED if f'{r[3]:02d}' == h and r[0] == 'IN')
            assert horas.get(h) == esperado, \
                f'hora {h} con India: esperado {esperado}, dio {horas.get(h)}'
        assert '20' not in horas, \
            'con India seleccionada aparece la hora 20, que sólo tiene NP y MX'
    s.check('con un país seleccionado, el desglose horario sólo muestra ese país',
            hourly_respects_country)

    def hourly_respects_route():
        t = av2.timeseries(db, start, end, 'hour', 'calls',
                           filters={'route': 'IN_STRINGEE'})
        horas = {r['bucket'][-8:-6]: r['value'] for r in t}
        assert horas.get('09') == 1 and horas.get('14') == 2, \
            f'desglose por ruta incorrecto: {horas}'
    s.check('el desglose horario respeta el filtro de ruta', hourly_respects_route)

    def hourly_and_totals_agree():
        """Si el desglose y el total no suman lo mismo, uno de los dos miente."""
        for f in (None, {'country': 'IN'}, {'provider': 'stringee'},
                  {'route': 'NP_PROVEEDOR1'}, {'country': ['IN', 'MX']}):
            m = total(av2.call_metrics(db, start, end, filters=f))
            t = av2.timeseries(db, start, end, 'hour', 'calls', filters=f)
            suma = sum(r['value'] for r in t)
            assert suma == m['attempted'], \
                (f'filtro {f}: el desglose suma {suma} y el total dice '
                 f'{m["attempted"]}')
    s.check('el desglose por hora suma siempre el total del KPI',
            hourly_and_totals_agree)

    # ══ export · §33 ═════════════════════════════════════════════════
    s.section('el export coincide con lo que se ve (§33)')

    def export_matches_screen():
        """Se compara el desglose agrupado (lo que va al CSV) contra el
        total filtrado (lo que dice el KPI de pantalla)."""
        for f in ({'country': 'IN'}, {'provider': 'proveedor1'},
                  {'route': 'IN_PROVEEDOR1'}, None):
            filas = av2.call_metrics(db, start, end, group_by='route', filters=f)
            m = total(av2.call_metrics(db, start, end, filters=f))
            assert sum(r['attempted'] for r in filas) == m['attempted'], \
                f'filtro {f}: el CSV por ruta no suma lo mismo que la pantalla'
    s.check('el CSV por ruta suma exactamente el KPI de pantalla',
            export_matches_screen)

    def export_only_contains_selection():
        filas = av2.call_metrics(db, start, end, group_by='country',
                                 filters={'country': 'IN'})
        paises = {r['country'] for r in filas}
        assert paises == {'IN'}, \
            f'el CSV filtrado por India trae otros países: {paises}'
    s.check('el CSV filtrado no trae filas de fuera de la selección',
            export_only_contains_selection)

    def filters_are_described():
        d = av2.describe_filters({'country': ['IN', 'NP'], 'provider': 'proveedor1'})
        assert 'IN' in d and 'NP' in d and 'proveedor1' in d
        assert av2.describe_filters(None).startswith('All')
    s.check('el recorte se describe para que el CSV diga con qué filtros salió',
            filters_are_described)

    def normalize_ignores_empty_and_all():
        n = av2.normalize_filters({'country': 'all', 'provider': '', 'route': None})
        assert n == {}, f'"all" o vacío deberían significar todos, dio {n}'
        n2 = av2.normalize_filters({'country': 'IN,NP'})
        assert n2 == {'country': ['IN', 'NP']}
    s.check("'all' y vacío significan todos; la coma separa valores",
            normalize_ignores_empty_and_all)

    # ══ inyección ════════════════════════════════════════════════════
    s.section('los filtros van parametrizados')

    def filters_are_parameterised():
        frag, params = av2._filters({'country': "IN' OR '1'='1"})
        assert "OR" not in frag, 'el valor del filtro se interpoló en el SQL'
        assert params == ["IN' OR '1'='1"], 'el valor no viaja como parámetro'
        m = total(av2.call_metrics(db, start, end,
                                   filters={'country': "IN' OR '1'='1"}))
        assert m['attempted'] == 0, 'una inyección devolvió filas'
    s.check('un valor malicioso viaja como parámetro y no devuelve nada',
            filters_are_parameterised)

    def unknown_filter_is_ignored():
        frag, params = av2._filters({'inventado': 'x'})
        assert frag == '' and params == [], \
            'un campo de filtro desconocido llega al SQL'
    s.check('un campo de filtro desconocido no llega al SQL',
            unknown_filter_is_ignored)

    # ══ fuente y atribución · §56-58 ═════════════════════════════════
    s.section('fuente de los datos y atribución (§56-58)')

    def source_is_local_mysql():
        import socket
        real = socket.socket.connect
        llamadas = []

        def espia(self, addr):
            llamadas.append(addr)
            return real(self, addr)
        socket.socket.connect = espia
        try:
            av2.call_metrics(db, start, end, filters={'country': 'IN'})
            av2.timeseries(db, start, end, 'hour', 'calls', filters={'country': 'IN'})
        finally:
            socket.socket.connect = real
        externas = [a for a in llamadas
                    if not (isinstance(a, tuple) and str(a[0]).startswith('127.'))]
        assert not externas, f'analytics salió a la red: {externas}'
    s.check('analytics no hace ni una llamada fuera de MySQL local (§57)',
            source_is_local_mysql)

    def attribution_is_declared():
        assert av2.ATTRIBUTION_MODEL == 'LAST_CONNECTED_CALL_V1'
        assert av2.ATTRIBUTION_WINDOW_DAYS > 0
    s.check('el modelo de atribución está declarado y versionado (§58)',
            attribution_is_declared)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
