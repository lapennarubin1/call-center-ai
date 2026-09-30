"""
test_foundation_v2_2.py — Lógica del panel V2.1.

Corre dos veces la misma batería:
  · backend sqlite   (sin dependencias)
  · backend mysql    (MariaDB con MIGRATION_001 V2.1 aplicada: el seed es el REAL)

    python3 tests/test_foundation_v2_2.py            # ambos si hay MariaDB
    LM_TEST_BACKENDS=sqlite python3 tests/...        # solo sqlite
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from _harness import (Suite, MIGRATION, mysql_available, mysql_conn, fresh_mysql_db,
                      run_sql_file)

import routes_config as rc          # noqa: E402
import followup_engine as fe        # noqa: E402
import call_jobs as cj              # noqa: E402
import ops_events as oe             # noqa: E402
from analytics import DB            # noqa: E402

S = Suite('FUNDACIÓN V2.2 (config · switches · claims)')
STD = None   # política estándar, cargada del seed


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════

def _std_policy_json():
    """La política del seed, leída del propio .sql (una sola fuente de verdad)."""
    sql = open(MIGRATION).read()
    start = sql.index("'{\"policy_version\"")
    end = sql.index("}'", start) + 1
    return sql[start + 1:end]


def seed_sqlite(db):
    """Espejo exacto del seed de MIGRATION V2.2, con las funciones del panel."""
    rc.ensure_routes_tables(db)
    oe.ensure_tables_sqlite(db)
    a = 'seed'
    for iso, name, pfx, tz, nlen in (('IN', 'India', '+91', 'Asia/Kolkata', '10'),
                                     ('NP', 'Nepal', '+977', 'Asia/Kathmandu', '')):
        rc.country_upsert(db, a, {'iso': iso, 'country_name': name, 'dial_prefix': pfx,
                                  'timezone': tz, 'language': 'hi', 'national_number_len': nlen})
    sip = rc.provider_upsert(db, a, 'proveedor1', 'PROVEEDOR1', 'ELEVENLABS_SIP',
                             account_ref='650098', enabled=True)
    strg = rc.provider_upsert(db, a, 'stringee', 'STRINGEE', 'STRINGEE_WORKER',
                              endpoint='http://172.18.0.1:8091', enabled=True)
    pid = rc.policy_upsert(db, a, 'STANDARD_CALL_RETRY', 'Standard', _std_policy_json())
    for key, iso, prov, en, prio, caller, agent, phone, cap in [
        ('IN_PROVEEDOR1', 'IN', sip, 1, 10, None, 'agent_5701kramx550e3qs2tm11661b48p',
         'phnum_7801kyktmabxembteqce884tanb6', 6),
        ('IN_STRINGEE', 'IN', strg, 1, 20, '917971730907', 'agent_5701kramx550e3qs2tm11661b48p', None, 1),
        ('NP_PROVEEDOR1', 'NP', sip, 0, 30, None, 'agent_7601m209ntj3fyrbg0dcq6w507yq',
         'phnum_7801kyktmabxembteqce884tanb6', 6)]:
        db.execute("INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id, "
                   "elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id) "
                   "VALUES (§,§,§,§,§,§,§,§,§,§)", (key, iso, prov, en, prio, caller, agent, phone, cap, pid))
    for key, purpose, chat in [('IN_PROVEEDOR1', 'recording', '-1004454561082'),
                               ('IN_PROVEEDOR1', 'recording', '-1003984044945'),
                               ('IN_PROVEEDOR1', 'account', '-1004454561082'),
                               ('IN_PROVEEDOR1', 'account', '-1003984044945'),
                               ('IN_STRINGEE', 'recording', '-1004454561082'),
                               ('IN_STRINGEE', 'recording', '-1003984044945'),
                               ('NP_PROVEEDOR1', 'recording', '-1004454561082'),
                               ('NP_PROVEEDOR1', 'recording', '-1003984044945')]:
        rid_ = db.one("SELECT id FROM call_routes WHERE route_key=§", (key,))['id']
        db.execute("INSERT INTO route_telegram_targets (route_id, purpose, chat_id) VALUES (§,§,§)",
                   (rid_, purpose, chat))
    rc.tool_upsert(db, a, 'IN', 'CREATE_ACCOUNT', {'enabled': '1', 'provider_key': 'cashstudio',
                   'market': 'IND', 'credential_ref': 'LEADSTUDIO_API',
                   'config_json': '{"portal_url":"https://crm.landmarkmarkets.in/"}'})
    rc.tool_upsert(db, a, 'IN', 'CREATE_PAYMENT_LINK', {'enabled': '1', 'provider_key': 'okpay',
                   'currency': 'INR', 'credential_ref': 'OKPAY_IN',
                   'config_json': '{"min_amount":2000,"max_amount":500000}'})
    rc.tool_upsert(db, a, 'IN', 'CALLBACK', {'enabled': '0'})
    rc.tool_upsert(db, a, 'NP', 'CREATE_ACCOUNT', {'enabled': '1', 'provider_key': 'cashstudio',
                   'market': 'NPL', 'credential_ref': 'LEADSTUDIO_API'})
    rc.tool_upsert(db, a, 'NP', 'CREATE_PAYMENT_LINK', {'enabled': '0', 'provider_key': 'monetix',
                   'currency': 'NPR'})
    db.execute("UPDATE countries SET enabled=1 WHERE iso IN ('IN','NP')")


_counter = [0]


def cutover(db):
    """Deja el sistema en V2_PRIMARY, que es de donde parten estos tests.

    AÑADIDO POR EL SUITE V2 (r2-final2). No cambia ninguna aserción de la
    fundación: cambia la PRECONDICIÓN.

    La fundación mide los tres interruptores —país, proveedor, ruta— y
    da por sentado que, si los tres están encendidos, la ruta llama. Eso
    dejó de ser cierto: V2 añadió un cuarto interruptor por encima, el
    modo de operación, y `routes_active_payload()` ahora FALLA CERRADO
    cuando no sabe en qué modo está el sistema. Sin esta línea las
    tablas de la fundación no tienen `lm_operating_mode`, el modo se lee
    como UNKNOWN y TODA ruta sale como no invocable: los 18 casos de
    capacidad medirían cero y fallarían por una razón que no es la suya.

    Poner V2_PRIMARY aquí equivale a decir "este sistema ya hizo el
    cutover", que es el único estado en el que la pregunta que hace la
    fundación —¿esta ruta llama?— tiene sentido. Que el modo AUSENTE no
    autorice a llamar se prueba aparte, en tests/test_real_interlock_v2.py
    (sección D/E) y en tests/test_interlock_v2.py.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                      setting_key   VARCHAR(64) NOT NULL PRIMARY KEY,
                      setting_value TEXT)""")
    db.execute("DELETE FROM app_settings WHERE setting_key=§",
               ('lm_operating_mode',))
    db.execute("INSERT INTO app_settings (setting_key, setting_value) VALUES (§,§)",
               ('lm_operating_mode', 'V2_PRIMARY'))
    return db


def make_db(backend):
    rc._SCHEMA_OK.clear()
    if backend == 'sqlite':
        db = DB(sqlite3.connect(':memory:'), 'sqlite')
        seed_sqlite(db)
        return cutover(db)
    _counter[0] += 1
    name = f"fnd_{_counter[0]}"
    fresh_mysql_db(name)
    run_sql_file(name, MIGRATION)
    return cutover(DB(mysql_conn(name), 'mysql'))


def R(db, key):
    return next(r for r in rc.routes_list(db, 'all') if r['route_key'] == key)


def rid(db, key):
    return R(db, key)['id']


def expect_config_error(fn, *needles):
    try:
        fn()
    except rc.ConfigError as ex:
        text = str(ex) + ' ' + ' '.join(i['message'] + ' ' + i['field'] for i in ex.issues)
        for n in needles:
            assert n.lower() in text.lower(), f'falta "{n}" en: {text}'
        return ex
    raise AssertionError('debía lanzar ConfigError')


def expect_value_error(fn, needle=None):
    try:
        fn()
    except ValueError as ex:
        if needle:
            assert needle.lower() in str(ex).lower(), str(ex)
        return
    raise AssertionError('debía lanzar ValueError')


# ══════════════════════════════════════════════════════════════════════
#  Casos — reciben `db` ya sembrado
# ══════════════════════════════════════════════════════════════════════

def c_seed(db):
    keys = [r['route_key'] for r in rc.routes_list(db, 'all')]
    assert keys == ['IN_PROVEEDOR1', 'IN_STRINGEE', 'NP_PROVEEDOR1'], keys
    assert all(r['policy_key'] == 'STANDARD_CALL_RETRY' for r in rc.routes_list(db, 'all'))
    assert R(db, 'IN_PROVEEDOR1')['ready'] if 'ready' in R(db, 'IN_PROVEEDOR1') else True
    for k in ('IN_PROVEEDOR1', 'IN_STRINGEE'):
        rep = rc.validate_route(db, R(db, k))
        assert rep['ready'], (k, rep['issues'])


# ── 1 · multi-proveedor mismo país ────────────────────────────────────
def c_multi_provider(db):
    t = utc(2026, 9, 15, 6, 0)   # martes 11:30 Kolkata
    rc.window_add(db, 't', rid(db, 'IN_PROVEEDOR1'), ['mon', 'tue', 'wed', 'thu', 'fri'], '09:00', '14:00', 5)
    rc.route_update(db, 't', rid(db, 'IN_STRINGEE'), {'capacity_default': '3'})
    rc.window_add(db, 't', rid(db, 'IN_STRINGEE'), ['mon', 'tue', 'wed', 'thu', 'fri'], '09:00', '20:00', 3)
    p = rc.routes_active_payload(db, now_utc=t)
    india = {r['route_key']: r['capacity_now'] for r in p['routes'] if r['iso'] == 'IN'}
    assert india == {'IN_PROVEEDOR1': 5, 'IN_STRINGEE': 3}, india
    assert sum(india.values()) == 8, 'capacidad potencial India = 8, cada ruta conserva su límite'


def c_mismo_pais_mismo_proveedor(db):
    """Sin UNIQUE(iso, provider): dos rutas IN + proveedor1 pueden coexistir."""
    sip = R(db, 'IN_PROVEEDOR1')['provider_id']
    r2 = rc.route_create(db, 't', {'iso': 'IN', 'provider_id': sip, 'route_key': 'IN_PROVEEDOR1_B',
                                   'capacity_default': '2'})
    assert rc.route_get(db, r2)['route_key'] == 'IN_PROVEEDOR1_B'


# ── 2 · validación de activación ──────────────────────────────────────
def _new_route(db, iso, prov_code, key, **fields):
    prov = next(p for p in rc.providers_list(db) if p['code'] == prov_code)
    form = {'iso': iso, 'provider_id': prov['id'], 'route_key': key, 'capacity_default': '3'}
    form.update(fields)
    return rc.route_create(db, 't', form)


def _mx(db, iso='MX', name='México', prefix='+52', tz='America/Mexico_City', enable=True):
    rc.country_upsert(db, 't', {'iso': iso, 'country_name': name, 'dial_prefix': prefix,
                                'timezone': tz, 'language': 'es'})
    if enable:
        rc.country_set_enabled(db, 't', iso, True)
    return next(p['id'] for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')


def c_ruta_nace_disabled_e_incompleta(db):
    _mx(db)
    r = _new_route(db, 'MX', 'proveedor1', 'MX_PROVEEDOR1')
    assert rc.route_get(db, r)['enabled'] == 0
    rep = rc.validate_route(db, R(db, 'MX_PROVEEDOR1'))
    fields = {i['field'] for i in rep['issues']}
    assert {'elevenlabs_agent_id', 'elevenlabs_phone_number_id', 'followup_policy',
            'telegram.recording'} <= fields, fields


def c_sip_sin_agent(db):
    pol = _mx(db)
    r = _new_route(db, 'MX', 'proveedor1', 'MX_PROVEEDOR1', elevenlabs_phone_number_id='phnum_x',
                   followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'Agent ID')
    assert rc.route_get(db, r)['enabled'] == 0


def c_sip_sin_phone(db):
    pol = _mx(db)
    r = _new_route(db, 'MX', 'proveedor1', 'MX_PROVEEDOR1', elevenlabs_agent_id='agent_x',
                   followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'Phone Number ID')


def c_stringee_sin_agent(db):
    pol = _mx(db)
    r = _new_route(db, 'MX', 'stringee', 'MX_STRINGEE', caller_id='5215512345678',
                   followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'Agent ID')


def c_stringee_sin_caller(db):
    pol = _mx(db)
    r = _new_route(db, 'MX', 'stringee', 'MX_STRINGEE', elevenlabs_agent_id='agent_x',
                   followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'caller_id')


def c_completa_se_activa(db):
    pol = _mx(db)
    r = _new_route(db, 'MX', 'proveedor1', 'MX_PROVEEDOR1', elevenlabs_agent_id='agent_x',
                   elevenlabs_phone_number_id='phnum_x', followup_policy_id=str(pol))
    rc.telegram_add(db, 't', r, 'recording', '-1003616406932')
    rc.route_set_enabled(db, 't', r, True)
    assert rc.route_get(db, r)['enabled'] == 1


def c_timezone_invalida(db):
    expect_value_error(lambda: rc.country_upsert(db, 't', {
        'iso': 'VE', 'country_name': 'Venezuela', 'dial_prefix': '+58',
        'timezone': 'America/Noexiste', 'language': 'es'}), 'timezone')
    # y si llegara corrupta a la base, la ruta no se activa ni llama
    db.execute("UPDATE countries SET timezone=§ WHERE iso='IN'", ('Marte/Olympus',))
    rep = rc.validate_route(db, R(db, 'IN_PROVEEDOR1'))
    assert not rep['ready'] and any(i['field'] == 'country.timezone' for i in rep['issues'])
    item = rc.route_by_key_payload(db, 'IN_PROVEEDOR1')
    assert item['calling_now'] is False and item['capacity_now'] == 0


def c_capacidad_invalida(db):
    pol = _mx(db)
    expect_value_error(lambda: _new_route(db, 'MX', 'proveedor1', 'MX_A', capacity_default='-1'), 'capacity')
    expect_value_error(lambda: _new_route(db, 'MX', 'proveedor1', 'MX_B', capacity_default='9999'), '500')
    r = _new_route(db, 'MX', 'proveedor1', 'MX_C', capacity_default='0', elevenlabs_agent_id='a',
                   elevenlabs_phone_number_id='p', followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'capacity_default')


def c_tool_opcional_disabled_no_bloquea(db):
    """NP CREATE_PAYMENT_LINK: disabled e incompleta (Monetix sin contrato)."""
    tools = {t['tool_type']: t for t in rc.tools_for_country(db, 'NP')}
    assert int(tools['CREATE_PAYMENT_LINK']['enabled']) == 0
    rc.route_set_enabled(db, 't', rid(db, 'NP_PROVEEDOR1'), True)
    assert R(db, 'NP_PROVEEDOR1')['enabled'] == 1


def c_tool_enabled_incompleta_bloquea(db):
    """V2.2: las tools son del PAÍS. Una tool enabled e incompleta deja al país
    NOT READY: no se puede encender, y si ya estaba ON sus rutas no llaman."""
    expect_config_error(lambda: rc.tool_upsert(db, 't', 'NP', 'CREATE_PAYMENT_LINK',
                                               {'enabled': '1', 'provider_key': 'monetix'}), 'currency')
    db.execute("UPDATE country_tool_configs SET enabled=1, currency=NULL "
               "WHERE country_iso='NP' AND tool_type='CREATE_PAYMENT_LINK'")
    rc.country_set_enabled(db, 't', 'NP', False)
    expect_config_error(lambda: rc.country_set_enabled(db, 't', 'NP', True), 'moneda')
    db.execute("UPDATE countries SET enabled=1 WHERE iso='NP'")
    rc.route_set_enabled(db, 't', rid(db, 'NP_PROVEEDOR1'), True)
    item = rc.route_by_key_payload(db, 'NP_PROVEEDOR1', utc(2026, 9, 15, 6, 0))
    assert item['ready'] and not item['country_ready'], item['config_issues']
    assert item['calling_now'] is False and 'COUNTRY_NOT_READY' in item['blocked_by'], item['blocked_by']


def c_update_ruta_activa_no_puede_romperla(db):
    expect_config_error(lambda: rc.route_update(db, 't', rid(db, 'IN_PROVEEDOR1'),
                                                {'elevenlabs_phone_number_id': ''}), 'Phone')
    assert R(db, 'IN_PROVEEDOR1')['elevenlabs_phone_number_id'], 'no debe haberse escrito'
    # sobre una ruta DISABLED sí se puede guardar incompleta
    rc.route_update(db, 't', rid(db, 'NP_PROVEEDOR1'), {'elevenlabs_agent_id': ''})


def c_fail_closed_runtime(db):
    """Una ruta activa que queda inválida por un cambio ajeno NO llama."""
    t = utc(2026, 9, 15, 6, 0)
    assert rc.route_by_key_payload(db, 'IN_STRINGEE', t)['calling_now'] is True
    for tg in [x for x in R(db, 'IN_STRINGEE')['telegram_rows'] if x['purpose'] == 'recording']:
        rc.telegram_delete(db, 't', tg['id'])
    item = rc.route_by_key_payload(db, 'IN_STRINGEE', t)
    assert item['calling_now'] is False and item['capacity_source']['mode'] == 'config_error', item
    keys = [r['route_key'] for r in rc.routes_active_payload(db, now_utc=t)['routes']]
    assert 'IN_STRINGEE' not in keys and 'IN_PROVEEDOR1' in keys


def c_provider_disabled(db):
    rc.provider_set_enabled(db, 't', R(db, 'IN_PROVEEDOR1')['provider_id'], False)
    t = utc(2026, 9, 15, 6, 0)
    keys = [r['route_key'] for r in rc.routes_active_payload(db, now_utc=t)['routes']]
    assert keys == ['IN_STRINGEE'], keys


def c_route_disabled_no_afecta_otras(db):
    rc.route_set_enabled(db, 't', rid(db, 'IN_PROVEEDOR1'), False)
    t = utc(2026, 9, 15, 6, 0)
    keys = [r['route_key'] for r in rc.routes_active_payload(db, now_utc=t)['routes']]
    assert keys == ['IN_STRINGEE'], keys


# ── 3 · archivado ─────────────────────────────────────────────────────
def c_archive(db):
    r = rid(db, 'NP_PROVEEDOR1')
    rc.route_archive(db, 't', r)
    assert R(db, 'NP_PROVEEDOR1')['status'] == 'ARCHIVED'
    assert 'NP_PROVEEDOR1' not in [x['route_key'] for x in rc.routes_active_payload(db)['routes']]
    assert 'NP_PROVEEDOR1' not in [x['route_key'] for x in rc.routes_active_payload(db, include_all=True)['routes']]
    assert 'NP_PROVEEDOR1' in [x['route_key'] for x in
                               rc.routes_active_payload(db, include_archived=True)['routes']]
    hist = rc.route_by_key_payload(db, 'NP_PROVEEDOR1')
    assert hist and hist['status'] == 'ARCHIVED' and hist['followup_policy']['rules'], \
        'WF9 debe poder resolver policy/recording de una ruta archivada'
    assert hist['calling_now'] is False
    expect_value_error(lambda: rc.route_update(db, 't', r, {'capacity_default': '2'}), 'solo lectura')
    expect_value_error(lambda: rc.route_set_enabled(db, 't', r, True), 'archivada')
    sip = R(db, 'IN_PROVEEDOR1')['provider_id']
    expect_value_error(lambda: rc.route_create(db, 't', {'iso': 'NP', 'provider_id': sip,
                                                         'route_key': 'NP_PROVEEDOR1'}), 'no se reutiliza')
    assert [x['route_key'] for x in rc.routes_list(db, 'archived')] == ['NP_PROVEEDOR1']
    assert 'NP_PROVEEDOR1' not in [x['route_key'] for x in rc.routes_list(db, 'active')]
    rc.route_unarchive(db, 't', r)
    assert R(db, 'NP_PROVEEDOR1')['status'] == 'DISABLED', 'desarchivar nunca reactiva'


def c_sin_hard_delete(db):
    assert not hasattr(rc, 'route_delete'), 'no debe existir borrado físico de rutas'
    src = open(rc.__file__).read()
    assert 'DELETE FROM call_routes' not in src


# ── 4 · franjas: cross-midnight y DST ─────────────────────────────────
def c_overlap_A_reject(db):
    r = rid(db, 'IN_PROVEEDOR1')
    rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    expect_value_error(lambda: rc.window_add(db, 't', r, ['tue'], '01:00', '03:00', 4), 'solapa')


def c_overlap_B_accept(db):
    r = rid(db, 'IN_PROVEEDOR1')
    rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    rc.window_add(db, 't', r, ['tue'], '02:00', '03:00', 4)


def c_overlap_C_reject(db):
    r = rid(db, 'IN_PROVEEDOR1')
    rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    expect_value_error(lambda: rc.window_add(db, 't', r, ['mon'], '21:00', '23:00', 4), 'solapa')


def c_overlap_D_accept(db):
    r = rid(db, 'IN_PROVEEDOR1')
    rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    rc.window_add(db, 't', r, ['tue'], '20:00', '02:00', 4)


def c_overlap_wrap_domingo(db):
    r = rid(db, 'IN_PROVEEDOR1')
    rc.window_add(db, 't', r, ['sun'], '22:00', '02:00', 4)
    expect_value_error(lambda: rc.window_add(db, 't', r, ['mon'], '01:00', '03:00', 4), 'solapa')
    rc.window_add(db, 't', r, ['mon'], '02:00', '03:00', 4)


def c_overlap_update_excluye_propia(db):
    r = rid(db, 'IN_PROVEEDOR1')
    w = rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    rc.window_update(db, 't', w, ['mon'], '20:00', '03:00', 6)


def c_resolve_cross_midnight(db):
    _mx(db)
    pol = next(p['id'] for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')
    r = _new_route(db, 'MX', 'proveedor1', 'MX_PROVEEDOR1', elevenlabs_agent_id='a',
                   elevenlabs_phone_number_id='p', followup_policy_id=str(pol), recording_telegram='0')
    rc.route_set_enabled(db, 't', r, True)
    rc.window_add(db, 't', r, ['mon'], '20:00', '02:00', 4)
    mx = R(db, 'MX_PROVEEDOR1')
    # mar 01:00 local (CST UTC-6) = mar 07:00 UTC → tramo heredado del lunes
    c, cap, i = rc.resolve_now(mx, mx['windows'], utc(2026, 9, 15, 7, 0))
    assert (c, cap) == (True, 4), i
    # mar 02:30 local → fuera
    c, cap, i = rc.resolve_now(mx, mx['windows'], utc(2026, 9, 15, 8, 30))
    assert (c, cap) == (False, 0), i
    # mié 01:00 local: el martes no tiene franja → fuera
    c, cap, i = rc.resolve_now(mx, mx['windows'], utc(2026, 9, 16, 7, 0))
    assert c is False, i


def c_dst_madrid(db):
    rc.country_upsert(db, 't', {'iso': 'ES', 'country_name': 'España', 'dial_prefix': '+34',
                                'timezone': 'Europe/Madrid', 'language': 'es'})
    rc.country_set_enabled(db, 't', 'ES', True)          # V2.2: un país nuevo nace OFF
    pol = next(p['id'] for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')
    r = _new_route(db, 'ES', 'proveedor1', 'ES_PROVEEDOR1', elevenlabs_agent_id='a',
                   elevenlabs_phone_number_id='p', followup_policy_id=str(pol), recording_telegram='0')
    rc.route_set_enabled(db, 't', r, True)
    rc.window_add(db, 't', r, rc.WEEKDAY_CODES, '10:00', '18:00', 3)
    es = R(db, 'ES_PROVEEDOR1')
    a = rc.resolve_now(es, es['windows'], utc(2026, 1, 14, 10, 0))   # 11:00 CET
    b = rc.resolve_now(es, es['windows'], utc(2026, 7, 15, 9, 0))    # 11:00 CEST
    assert a[2]['local_time'] == b[2]['local_time'] == '11:00'
    assert a[:2] == b[:2] == (True, 3)


def c_capacidad_5_8_5(db):
    r = rid(db, 'IN_PROVEEDOR1')
    days = ['mon', 'tue', 'wed', 'thu', 'fri']
    rc.window_add(db, 't', r, days, '09:00', '14:00', 5)
    pico = rc.window_add(db, 't', r, days, '14:00', '15:00', 8)
    rc.window_add(db, 't', r, days, '15:00', '20:00', 5)
    cases = [(5, 0, 5), (9, 0, 8), (12, 0, 5), (16, 0, 0), (1, 0, 0)]   # UTC → 10:30, 14:30, 17:30, 21:30, 06:30
    for h, m, want in cases:
        item = rc.route_by_key_payload(db, 'IN_PROVEEDOR1', utc(2026, 9, 15, h, m))
        assert item['capacity_now'] == want, (h, item['capacity_now'], item['capacity_source'])
    rc.window_update(db, 't', pico, days, '14:00', '15:00', 3)
    assert rc.route_by_key_payload(db, 'IN_PROVEEDOR1', utc(2026, 9, 15, 9, 0))['capacity_now'] == 3


def c_sin_franjas_24_7(db):
    item = rc.route_by_key_payload(db, 'IN_PROVEEDOR1', utc(2026, 9, 19, 22, 0))  # sábado noche
    assert item['calling_now'] and item['capacity_now'] == 6
    assert item['capacity_source']['mode'] == 'default'


# ── 5 · follow-up policy / engine ─────────────────────────────────────
def c_policy_1_a_9(db):
    pol = next(p for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')['policy']
    now = utc(2026, 9, 18, 4, 30)       # viernes 10:00 Kolkata
    want = {1: ('RETRY', '2026-09-18T06:30:00Z'), 2: ('RETRY', '2026-09-18T07:30:00Z'),
            3: ('RETRY', '2026-09-22T04:30:00Z'),     # +2bd: vie → mar 10:00 local
            4: ('RETRY', '2026-09-18T06:30:00Z'), 5: ('RETRY', '2026-09-18T07:30:00Z'),
            6: ('RETRY', '2026-09-23T04:30:00Z'),     # +3bd: vie → mié 10:00 local
            7: ('RETRY', '2026-09-18T06:30:00Z'), 8: ('RETRY', '2026-09-18T07:30:00Z'),
            9: ('CLOSE', None), 10: ('CLOSE', None), 15: ('CLOSE', None)}
    for att, (action, sched) in want.items():
        d = fe.resolve(pol, att, 'NO_ANSWER', now, 'Asia/Kolkata')
        assert (d['action'], d['schedule_next_at']) == (action, sched), (att, d)
    assert fe.resolve(pol, 9, 'NO_ANSWER', now, 'Asia/Kolkata')['rule_matched'] == \
        {'result': 'NO_ANSWER', 'attempt': 9}


def c_policy_resultados(db):
    pol = next(p for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')['policy']
    now = utc(2026, 9, 18, 4, 30)
    r = lambda res, att=1, **kw: fe.resolve(pol, att, res, now, 'Asia/Kolkata', **kw)
    assert r('BUSY')['action'] == 'RETRY' and r('BUSY')['effective_result'] == 'NO_ANSWER'
    assert r('VOICEMAIL', 3)['schedule_next_at'] == '2026-09-22T04:30:00Z'
    assert r('ANSWERED')['action'] == 'COMPLETE' and r('ANSWERED')['schedule_next_at'] is None
    assert r('WRONG_NUMBER')['action'] == 'CLOSE' and r('DNC')['action'] == 'CLOSE'
    assert r('CALLBACK')['schedule_next_at'] == '2026-09-19T04:30:00Z'       # +24h default
    assert r('CALLBACK', callback_at='2026-09-20T09:00:00Z')['schedule_next_at'] == '2026-09-20T09:00:00Z'
    assert r('CALLBACK', callback_at='2020-01-01T00:00:00Z')['schedule_next_at'] == '2026-09-19T04:30:00Z', \
        'un callback_at en el pasado cae al default'
    assert r('FAILED')['action'] == 'NONE' and r('UNKNOWN')['action'] == 'NONE'
    # SIP: la política decide qué es "no contestó", no el adapter
    assert r('FAILED', 2, sip_code='603')['action'] == 'RETRY'
    assert r('FAILED', 2, sip_code='603')['effective_result'] == 'NO_ANSWER'
    assert r('FAILED', 2, sip_code='503')['action'] == 'NONE'
    try:
        r('DISPATCHED')
        raise AssertionError('DISPATCHED no es resultado final')
    except fe.PolicyError:
        pass


def c_policy_misma_para_stringee(db):
    """Stringee no tiene política especial: mismo resultado + intento = misma decisión."""
    now = utc(2026, 9, 18, 4, 30)
    a = R(db, 'IN_PROVEEDOR1')['policy']
    b = R(db, 'IN_STRINGEE')['policy']
    for att in range(1, 10):
        assert fe.resolve(a, att, 'NO_ANSWER', now, 'Asia/Kolkata') == \
            fe.resolve(b, att, 'NO_ANSWER', now, 'Asia/Kolkata')


def c_policy_validacion(db):
    bad = [
        '{"rules":[{"result":"NO_ANSWER","attempt":1,"action":"RETRY","delay":"2h"}]}',
        '{"rules":[{"result":"NO_ANSWER","attempt":1,"action":"RETRY"}]}',
        '{"rules":[{"result":"NO_ANSWER","attempt":1,"action":"CLOSE","delay":"+2h"}]}',
        '{"rules":[{"result":"NO_ANSWER","attempt":0,"action":"CLOSE"}]}',
        '{"rules":[{"result":"DISPATCHED","attempt":1,"action":"CLOSE"}]}',
        '{"rules":[{"result":"NO_ANSWER","attempt":1,"action":"CLOSE"},{"result":"NO_ANSWER","attempt":1,"action":"CLOSE"}]}',
        '{"rules":[]}', 'no json', '[1]',
        '{"rules":[{"result":"NO_ANSWER","attempt":1,"action":"CLOSE"}],"result_aliases":{"BUSY":"BUSY"}}',
    ]
    for b in bad:
        expect_value_error(lambda b=b: rc.policy_upsert(db, 't', 'BAD_POLICY', 'x', b), 'política inválida')


def c_policy_reutilizable_y_archivado(db):
    pid = R(db, 'IN_PROVEEDOR1')['followup_policy_id']
    assert R(db, 'IN_STRINGEE')['followup_policy_id'] == pid == R(db, 'NP_PROVEEDOR1')['followup_policy_id']
    expect_value_error(lambda: rc.policy_set_archived(db, 't', pid, True), 'asignada')


def c_business_days_tz(db):
    # viernes 23:30 en Kolkata = viernes 18:00 UTC; +1bd debe ir al LUNES local
    d = fe.apply_delay('+1bd', utc(2026, 9, 18, 18, 0), 'Asia/Kolkata')
    assert d.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Kolkata')).strftime('%a %H:%M') == 'Mon 23:30'


# ── 6 · tools ─────────────────────────────────────────────────────────
def c_tools_modos(db):
    _mx(db)
    expect_config_error(lambda: rc.tool_upsert(db, 't', 'MX', 'CREATE_ACCOUNT',
                                               {'enabled': '1', 'provider_key': 'cashstudio'}), 'market')
    expect_config_error(lambda: rc.tool_upsert(db, 't', 'MX', 'CREATE_ACCOUNT',
                                               {'enabled': '1', 'mode': 'CUSTOM_ENDPOINT',
                                                'endpoint': 'http://x.com', 'http_method': 'POST',
                                                'credential_ref': 'MX_ACCOUNT_API'}), 'https')
    expect_config_error(lambda: rc.tool_upsert(db, 't', 'MX', 'CREATE_ACCOUNT',
                                               {'enabled': '1', 'mode': 'CUSTOM_ENDPOINT',
                                                'endpoint': 'https://x.com/create',
                                                'http_method': 'POST'}), 'credential_ref')
    rc.tool_upsert(db, 't', 'MX', 'CREATE_ACCOUNT', {'enabled': '1', 'mode': 'CUSTOM_ENDPOINT',
                                                     'endpoint': 'https://x.com/create', 'http_method': 'POST',
                                                     'credential_ref': 'MX_ACCOUNT_API'})
    rc.tool_upsert(db, 't', 'MX', 'CREATE_PAYMENT_LINK', {'enabled': '0'})   # incompleta pero disabled: OK
    p = rc.country_tools_payload(db, 'MX')
    assert list(p['tools']) == ['CREATE_ACCOUNT'], 'solo tools enabled en el payload'
    assert p['tools']['CREATE_ACCOUNT']['credential_ref'] == 'MX_ACCOUNT_API'


def c_tools_sin_secretos(db):
    _mx(db)
    for bad in ('{"api_key":"sk_123"}', '{"auth":{"password":"x"}}', '{"client_secret":"y"}',
                '{"list":[{"token":"z"}]}'):
        expect_value_error(lambda b=bad: rc.tool_upsert(db, 't', 'MX', 'CALLBACK',
                                                        {'enabled': '0', 'config_json': b}), 'secreto')
    expect_value_error(lambda: rc.tool_upsert(db, 't', 'MX', 'CREATE_ACCOUNT',
                                              {'enabled': '0', 'credential_ref': 'sk_live_abc123'}),
                       'nombre lógico')


# ── 7 · auditoría ─────────────────────────────────────────────────────
def c_auditoria(db):
    r = rid(db, 'IN_STRINGEE')
    rc.route_update(db, 'master', r, {'capacity_default': '6'})
    rc.route_set_enabled(db, 'master', r, False)
    rc.route_archive(db, 'master', r)
    rows = rc.audit_recent(db, 50)
    acts = [(a['action'], a['field']) for a in rows if a['route_key'] == 'IN_STRINGEE']
    assert ('update', 'capacity_default') in acts and ('disable', 'enabled') in acts \
        and ('archive', 'archived_at') in acts, acts
    ch = next(a for a in rows if a['field'] == 'capacity_default')
    assert (ch['old_value'], ch['new_value'], ch['actor']) == ('1', '6', 'master')
    n = len(rc.audit_recent(db, 500))
    rc.route_update(db, 'master', rid(db, 'IN_PROVEEDOR1'), {'capacity_default': '6'})
    assert len(rc.audit_recent(db, 500)) == n, 'un no-cambio no se audita'


# ── 8 · contrato del payload ──────────────────────────────────────────
def c_contrato(db):
    p = rc.routes_active_payload(db, include_all=True)
    req = ['route_id', 'route_key', 'status', 'ready', 'country_ready', 'switches', 'blocked_by',
           'iso', 'country', 'provider', 'adapter_key', 'provider_endpoint', 'dial_prefix',
           'language', 'timezone', 'calling_now', 'capacity_now', 'elevenlabs_agent_id',
           'elevenlabs_phone_number_id', 'caller_id', 'followup_policy', 'followup_policy_key',
           'recording_enabled', 'recording_min_secs', 'recording', 'telegram', 'tools']
    for r in p['routes']:
        for k in req:
            assert k in r, f"falta {k} en {r['route_key']}"
        assert set(r['switches']) == {'country_enabled', 'provider_enabled', 'route_enabled'}
    blob = json.dumps(p, default=str).lower()
    for leak in ('sk_79048', 'admin@123', 'wsec_', 'vduatcc'):
        assert leak not in blob, leak


def _claim(db, lead, attempt, key='IN_PROVEEDOR1'):
    job = cj.make_call_job_id(key)
    return job, cj.claim_job(db, job, lead, 1, key, 'IN', 'proveedor1', attempt, 'exec-1')


def c_claim_mismo_lead_intento(db):
    j1, a = _claim(db, 'L1', 1, 'IN_PROVEEDOR1')
    j2, b = _claim(db, 'L1', 1, 'IN_STRINGEE')
    assert (a, b) == (True, False)
    assert cj.job_get(db, j1)['route_key'] == 'IN_PROVEEDOR1'


def c_claim_distintos(db):
    assert _claim(db, 'L1', 1)[1] and _claim(db, 'L2', 1)[1], 'leads distintos: ambos'
    j1, won = _claim(db, 'L3', 1)
    assert won
    assert _claim(db, 'L3', 2)[1] is False, 'intento 2 con el 1 EN VUELO → SKIP (sería doble llamada)'
    cj.mark_dispatching(db, j1)
    cj.record_result(db, j1, 'NO_ANSWER', 0)
    assert _claim(db, 'L3', 2)[1], 'intento distinto, en secuencia → válido'


def c_job_estados(db):
    job, won = _claim(db, 'L9', 1)
    assert won and cj.mark_dispatching(db, job)
    assert not cj.mark_dispatching(db, job), 'no se puede re-despachar'
    assert cj.mark_dispatched(db, job, conversation_id='conv_1', http_status=200)
    assert cj.record_result(db, job, 'ANSWERED', 120) == 'RECORDED'
    assert cj.job_get(db, job)['state'] == 'COMPLETED'


def c_job_crash_tras_dispatch(db):
    job, _ = _claim(db, 'L7', 1)
    cj.mark_dispatching(db, job)
    db.execute("UPDATE wf_call_jobs SET updated_at='2000-01-01 00:00:00' WHERE call_job_id=§", (job,))
    cj.reconcile_stale_jobs(db)
    assert cj.job_get(db, job)['state'] == 'NEEDS_RECONCILIATION'
    assert _claim(db, 'L7', 1)[1] is False, 'NUNCA se vuelve a llamar automáticamente'
    assert _claim(db, 'L7', 2)[1] is False, 'ni con el intento siguiente: el lead queda bloqueado'


def c_job_fallo_pre_dispatch_reclamable(db):
    """Error TÉCNICO (401): no consume el intento de negocio; reintento con backoff."""
    job, _ = _claim(db, 'L8', 1)
    cj.mark_dispatching(db, job)
    assert cj.mark_released(db, job, 'AUTH_ERROR', 'HTTP 401', http_status=401)
    assert cj.job_get(db, job)['state'] == 'RELEASED'
    assert cj.next_attempt(db, 'L8', 0) == 1, 'el intento liberado se retoma, no se salta'
    assert _claim(db, 'L8', 1, 'IN_STRINGEE')[1] is False, 'en backoff: todavía no'
    db.execute("UPDATE wf_call_jobs SET next_tech_retry_at='2000-01-01 00:00:00' WHERE lead_id='L8'")
    j2, won = _claim(db, 'L8', 1, 'IN_STRINGEE')
    assert won, 'vencido el backoff, el MISMO intento se re-reclama'
    row = cj.job_get(db, j2)
    assert (row['attempt'], row['tech_retry_count'], row['route_key']) == (1, 1, 'IN_STRINGEE'), row


def c_job_fallo_proveedor_consume_intento(db):
    """Error PERMANENTE (dato inválido): terminal, no se re-reclama."""
    job, _ = _claim(db, 'L6', 1)
    cj.mark_dispatching(db, job)
    cj.mark_failed(db, job, 'VALIDATION_ERROR', 'numero invalido')
    assert cj.job_get(db, job)['state'] == 'FAILED'
    assert _claim(db, 'L6', 1)[1] is False


def c_ledger(db):
    assert cj.claim_conversation(db, 'conv_A', 'webhook', route_key='IN_PROVEEDOR1')
    assert not cj.claim_conversation(db, 'conv_A', 'polling', route_key='IN_PROVEEDOR1')
    assert cj.complete_conversation(db, 'conv_A', 'fu_1', 'ANSWERED')
    assert cj.conversation_get(db, 'conv_A')['state'] == 'PROCESSED'


def c_ledger_sin_retry_ciego(db):
    cj.claim_conversation(db, 'conv_B', 'webhook')
    db.execute("UPDATE wf_conversation_ledger SET claimed_at='2000-01-01 00:00:00' "
               "WHERE conversation_id='conv_B'")
    assert cj.reconcile_stale_conversations(db) == 1
    assert cj.conversation_get(db, 'conv_B')['state'] == 'NEEDS_RECONCILIATION'
    assert not cj.claim_conversation(db, 'conv_B', 'polling'), 'no hay reintento ciego'
    cj.claim_conversation(db, 'conv_C', 'webhook')
    cj.fail_conversation(db, 'conv_C', 'CRM_TIMEOUT', followup_may_exist=True)
    assert cj.conversation_get(db, 'conv_C')['state'] == 'NEEDS_RECONCILIATION'


# ══════════════════════════════════════════════════════════════════════
#  V2.2 · proveedor único con varias rutas, tres interruptores, adapters
# ══════════════════════════════════════════════════════════════════════

def _five_countries(db):
    """PROVEEDOR1 con rutas en IN (seed), NP (seed), MX, CO, VE."""
    pol = next(p['id'] for p in rc.policies_list(db) if p['policy_key'] == 'STANDARD_CALL_RETRY')
    for iso, name, pfx, tz in (('MX', 'México', '+52', 'America/Mexico_City'),
                               ('CO', 'Colombia', '+57', 'America/Bogota'),
                               ('VE', 'Venezuela', '+58', 'America/Caracas')):
        _mx(db, iso, name, pfx, tz)
        r = _new_route(db, iso, 'proveedor1', f'{iso}_PROVEEDOR1', elevenlabs_agent_id=f'agent_{iso}',
                       elevenlabs_phone_number_id=f'phnum_{iso}', followup_policy_id=str(pol),
                       recording_telegram='0')
        rc.route_set_enabled(db, 't', r, True)
    rc.route_set_enabled(db, 't', rid(db, 'NP_PROVEEDOR1'), True)


T_ALL_OPEN = utc(2026, 9, 16, 14, 0)


def _calling(db, t=T_ALL_OPEN):
    return sorted(r['route_key'] for r in rc.routes_active_payload(db, now_utc=t)['routes'])


def c_proveedor1_multiruta(db):
    _five_countries(db)
    rows = [r for r in rc.routes_list(db, 'all') if r['provider_code'] == 'proveedor1']
    assert sorted(r['route_key'] for r in rows) == ['CO_PROVEEDOR1', 'IN_PROVEEDOR1', 'MX_PROVEEDOR1',
                                                   'NP_PROVEEDOR1', 'VE_PROVEEDOR1']
    assert len({r['provider_id'] for r in rows}) == 1, 'UN solo proveedor, cinco rutas'
    assert len([p for p in rc.providers_list(db) if p['adapter_key'] == 'ELEVENLABS_SIP']) == 1
    assert _calling(db) == ['CO_PROVEEDOR1', 'IN_PROVEEDOR1', 'IN_STRINGEE', 'MX_PROVEEDOR1',
                            'NP_PROVEEDOR1', 'VE_PROVEEDOR1'], _calling(db)


def c_country_off(db):
    _five_countries(db)
    rc.country_set_enabled(db, 't', 'IN', False)
    calling = _calling(db)
    assert 'IN_PROVEEDOR1' not in calling and 'IN_STRINGEE' not in calling, calling
    assert {'NP_PROVEEDOR1', 'MX_PROVEEDOR1', 'CO_PROVEEDOR1', 'VE_PROVEEDOR1'} <= set(calling)
    item = rc.route_by_key_payload(db, 'IN_STRINGEE', T_ALL_OPEN)
    assert item['blocked_by'] == ['COUNTRY_DISABLED'], item['blocked_by']
    assert item['ready'], 'apagar el país no toca la configuración de la ruta'


def c_route_off(db):
    rc.route_set_enabled(db, 't', rid(db, 'IN_PROVEEDOR1'), False)
    assert _calling(db) == ['IN_STRINGEE']
    assert rc.route_by_key_payload(db, 'IN_PROVEEDOR1', T_ALL_OPEN)['blocked_by'] == ['ROUTE_DISABLED']


def c_provider_off(db):
    _five_countries(db)
    rc.provider_set_enabled(db, 't', R(db, 'IN_PROVEEDOR1')['provider_id'], False)
    assert _calling(db) == ['IN_STRINGEE'], _calling(db)
    for k in ('IN_PROVEEDOR1', 'NP_PROVEEDOR1', 'MX_PROVEEDOR1', 'CO_PROVEEDOR1', 'VE_PROVEEDOR1'):
        assert rc.route_by_key_payload(db, k, T_ALL_OPEN)['blocked_by'] == ['PROVIDER_DISABLED'], k


def c_switches_combinados(db):
    rc.country_set_enabled(db, 't', 'IN', False)
    rc.provider_set_enabled(db, 't', R(db, 'IN_PROVEEDOR1')['provider_id'], False)
    rc.route_set_enabled(db, 't', rid(db, 'IN_PROVEEDOR1'), False)
    b = rc.route_by_key_payload(db, 'IN_PROVEEDOR1', T_ALL_OPEN)['blocked_by']
    assert b == ['COUNTRY_DISABLED', 'PROVIDER_DISABLED', 'ROUTE_DISABLED'], b
    rc.country_set_enabled(db, 't', 'IN', True)
    rc.provider_set_enabled(db, 't', R(db, 'IN_PROVEEDOR1')['provider_id'], True)
    rc.route_set_enabled(db, 't', rid(db, 'IN_PROVEEDOR1'), True)
    assert rc.route_by_key_payload(db, 'IN_PROVEEDOR1', T_ALL_OPEN)['calling_now']


def c_adapter_desconocido(db):
    pid = rc.provider_upsert(db, 't', 'twilio', 'TWILIO', 'TWILIO_VOICE', enabled=False)
    expect_config_error(lambda: rc.provider_set_enabled(db, 't', pid, True), 'no soportado')
    pol = _mx(db)
    r = _new_route(db, 'MX', 'twilio', 'MX_TWILIO', elevenlabs_agent_id='a',
                   followup_policy_id=str(pol), recording_telegram='0')
    rep = rc.validate_route(db, R(db, 'MX_TWILIO'))
    assert any(i['code'] == 'UNKNOWN_ADAPTER' for i in rep['issues']), rep['issues']
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'no soportado')
    expect_value_error(lambda: rc.provider_upsert(db, 't', 'x2', 'X', 'sip minúscula'), 'adapter_key')


def c_adapter_reutilizado(db):
    rc.provider_upsert(db, 't', 'proveedor2', 'PROVEEDOR2', 'ELEVENLABS_SIP', enabled=True)
    pol = _mx(db)
    r = _new_route(db, 'MX', 'proveedor2', 'MX_PROVEEDOR2', elevenlabs_agent_id='a',
                   followup_policy_id=str(pol), recording_telegram='0')
    expect_config_error(lambda: rc.route_set_enabled(db, 't', r, True), 'Phone Number ID')
    rc.route_update(db, 't', r, {'elevenlabs_phone_number_id': 'phnum_mx2'})
    rc.route_set_enabled(db, 't', r, True)
    assert 'MX_PROVEEDOR2' in _calling(db)


def c_country_staging(db):
    _mx(db, enable=False)
    rep = rc.validate_country(db, 'MX')
    assert rep['ready'] and rep['routes_total'] == 0
    rc.country_set_enabled(db, 't', 'MX', True)
    assert rc.country_get(db, 'MX')['enabled'] == 1
    assert not [r for r in _calling(db) if r.startswith('MX_')]


def c_country_nace_off(db):
    rc.country_upsert(db, 't', {'iso': 'PK', 'country_name': 'Pakistan', 'dial_prefix': '+92',
                                'timezone': 'Asia/Karachi', 'language': 'ur'})
    assert rc.country_get(db, 'PK')['enabled'] == 0, 'fail-closed: un país nuevo nace apagado'


CASES = [
    ('seed', [('el seed reproduce la config v1 y las rutas activas están READY', c_seed)]),
    ('1 · multi-proveedor mismo país', [
        ('IN_PROVEEDOR1 (5) + IN_STRINGEE (3) activas a la vez = 8, límites propios', c_multi_provider),
        ('dos rutas IN + proveedor1 pueden coexistir (sin UNIQUE iso/proveedor)', c_mismo_pais_mismo_proveedor)]),
    ('2 · validación de activación', [
        ('ruta nueva nace DISABLED y lista lo que falta', c_ruta_nace_disabled_e_incompleta),
        ('SIP sin agent_id → no activa', c_sip_sin_agent),
        ('SIP sin phone_number_id → no activa', c_sip_sin_phone),
        ('Stringee sin agent_id → no activa', c_stringee_sin_agent),
        ('Stringee sin caller_id → no activa', c_stringee_sin_caller),
        ('ruta completa → se activa', c_completa_se_activa),
        ('timezone inválida → rechazo (y fail-closed si llega corrupta)', c_timezone_invalida),
        ('capacidad inválida → rechazo', c_capacidad_invalida),
        ('tool opcional disabled e incompleta → la ruta se activa igual', c_tool_opcional_disabled_no_bloquea),
        ('payment enabled e incompleta → país NOT READY: no enciende y sus rutas no llaman', c_tool_enabled_incompleta_bloquea),
        ('editar una ruta ACTIVA no puede dejarla incompleta', c_update_ruta_activa_no_puede_romperla),
        ('fail-closed en runtime: activa que queda inválida no llama', c_fail_closed_runtime),
        ('proveedor deshabilitado → sus rutas no llaman', c_provider_disabled),
        ('deshabilitar una ruta no afecta a la otra del mismo país', c_route_disabled_no_afecta_otras)]),
    ('3 · archivado (sin hard delete)', [
        ('archivada: fuera de active/all, resoluble por clave, solo lectura, clave no reutilizable', c_archive),
        ('no existe borrado físico de rutas', c_sin_hard_delete)]),
    ('4 · franjas, cross-midnight, DST', [
        ('A · Mon 20-02 + Tue 01-03 → REJECT', c_overlap_A_reject),
        ('B · Mon 20-02 + Tue 02-03 → ACCEPT', c_overlap_B_accept),
        ('C · Mon 20-02 + Mon 21-23 → REJECT', c_overlap_C_reject),
        ('D · Mon 20-02 + Tue 20-02 → ACCEPT', c_overlap_D_accept),
        ('Sun 22-02 da la vuelta a Mon: Mon 01-03 REJECT, Mon 02-03 ACCEPT', c_overlap_wrap_domingo),
        ('editar una franja no choca consigo misma', c_overlap_update_excluye_propia),
        ('resolución cross-midnight en México', c_resolve_cross_midnight),
        ('DST Madrid: misma hora local en invierno y verano', c_dst_madrid),
        ('India 5/8/5 y cambio en vivo 8→3', c_capacidad_5_8_5),
        ('sin franjas = capacity_default 24/7 (comportamiento actual)', c_sin_franjas_24_7)]),
    ('5 · follow-up policy', [
        ('intentos 1-9: 2h/3h/2bd/2h/3h/3bd/2h/3h/CLOSE; 10+ CLOSE', c_policy_1_a_9),
        ('resultados: aliases, COMPLETE, CLOSE, CALLBACK, SIP 603 vía política, DISPATCHED rechazado', c_policy_resultados),
        ('Stringee usa la misma política: decisiones idénticas 1-9', c_policy_misma_para_stringee),
        ('políticas malformadas rechazadas', c_policy_validacion),
        ('política compartida por 3 rutas; no se archiva si está en uso', c_policy_reutilizable_y_archivado),
        ('días hábiles calculados en el huso del país', c_business_days_tz)]),
    ('6 · tools', [
        ('CONFIG_ROUTER / CUSTOM_ENDPOINT: requisitos y payload solo con enabled', c_tools_modos),
        ('secretos en config_json o credential_ref → rechazados', c_tools_sin_secretos)]),
    ('7 · auditoría y contrato', [
        ('auditoría con actor y valores; no-cambios no se registran', c_auditoria),
        ('payload con todos los campos y sin secretos', c_contrato)]),
    ('V2.2 · proveedor1 multi-ruta y tres interruptores', [
        ('PROVEEDOR1: UN proveedor, rutas IN/NP/MX/CO/VE, todas llamando', c_proveedor1_multiruta),
        ('país OFF: India no llama por ningún proveedor; NP/MX/CO/VE siguen', c_country_off),
        ('ruta OFF: IN_PROVEEDOR1 apagada, IN_STRINGEE sigue', c_route_off),
        ('proveedor OFF: PROVEEDOR1 apagado en los 5 países; Stringee sigue', c_provider_off),
        ('los tres bloqueos se informan juntos y se levantan juntos', c_switches_combinados),
        ('adapter desconocido: se guarda apagado, no se activa', c_adapter_desconocido),
        ('PROVEEDOR2 reutiliza ELEVENLABS_SIP con sus mismos requisitos', c_adapter_reutilizado),
        ('país en staging: ON con 0 rutas READY, no llama', c_country_staging),
        ('país nuevo nace OFF', c_country_nace_off)]),
    ('8 · claims (secuencial)', [
        ('mismo lead + mismo intento → gana uno', c_claim_mismo_lead_intento),
        ('leads distintos: ambos · mismo lead intento 2: SKIP en vuelo, OK en secuencia', c_claim_distintos),
        ('máquina de estados: no se re-despacha', c_job_estados),
        ('crash tras DISPATCHING → NEEDS_RECONCILIATION, sin re-llamada', c_job_crash_tras_dispatch),
        ('error técnico 401: RELEASED, no consume intento, backoff, re-claim del MISMO intento', c_job_fallo_pre_dispatch_reclamable),
        ('error permanente: FAILED terminal, no se re-reclama', c_job_fallo_proveedor_consume_intento),
        ('ledger: webhook gana, polling pierde', c_ledger),
        ('ledger: CLAIMED viejo → NEEDS_RECONCILIATION, sin retry ciego', c_ledger_sin_retry_ciego)]),
]


def main():
    backends = os.getenv('LM_TEST_BACKENDS', 'sqlite,mysql').split(',')
    if 'mysql' in backends and not mysql_available():
        S.skip('backend mysql', 'MariaDB no disponible')
        backends = [b for b in backends if b != 'mysql']
    for be in backends:
        for section, cases in CASES:
            S.section(f'[{be}] {section}')
            for name, fn in cases:
                S.check(f'[{be}] {name}', lambda fn=fn, be=be: fn(make_db(be)))
    return S.finish()


if __name__ == '__main__':
    raise SystemExit(main())
