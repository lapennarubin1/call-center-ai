#!/usr/bin/env python3
"""La matriz de comportamiento que el suite tiene que cumplir.

Se ejecuta contra MariaDB real, usando los módulos de referencia del panel
(`routes_config`, `call_jobs`, `tool_requests`, `recording_ledger`) — que son
los mismos que la API expone a los workflows y los mismos cuyo SQL replican los
nodos (verificado en `test_workflow_sql_v2.py`).

Cubre: varias rutas por proveedor · varios proveedores por país · los tres
interruptores · rutas archivadas · franjas horarias · cruce de medianoche ·
cambio de horario · validación de ruta y de tool · adapters.
"""
import json
import os
import sys
from datetime import datetime, timezone as _tz

# Importar los módulos del suite no debe dejar .pyc dentro del paquete:
# lo que se empaqueta tiene que salir limpio.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _suite as S

import call_jobs as cj
import recording_ledger as rl
import routes_config as rc
import tool_requests as tr
import wf_settings as ws

DB_NAME = 'lm_behaviour_test'
ACTOR = 'test'


def utc(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


def main():
    s = S.Suite('MATRIZ DE COMPORTAMIENTO DEL SUITE V2')
    if not S.mysql_available():
        s.skip('suite completa', 'MariaDB no disponible')
        return s.finish()

    S.migrated_db(DB_NAME, baseline=True)
    db = S.db_for(DB_NAME)

    def route_by_key(key):
        return rc.route_get_full(db, key)

    def calling(key, now=None):
        r = route_by_key(key)
        assert r, f'ruta inexistente: {key}'
        return rc.route_to_api(db, r, now)

    # ══ un proveedor, varias rutas ════════════════════════════════════
    s.section('un proveedor comercial con rutas en varios países')

    def seed_more_routes():
        """PROVEEDOR1 atiende India, Nepal y ahora México: UN proveedor, tres
        rutas. No se crea un proveedor por país."""
        rc.country_upsert(db, ACTOR, {
            'iso': 'MX', 'country_name': 'Mexico', 'dial_prefix': '+52',
            'national_number_len': '10', 'timezone': 'America/Mexico_City',
            'language': 'es'})
        pid = [p for p in rc.providers_list(db) if p['code'] == 'proveedor1'][0]['id']
        policy = rc.policies_list(db)[0]['id']
        rc.route_create(db, ACTOR, {
            'route_key': 'MX_PROVEEDOR1', 'iso': 'MX', 'provider_id': pid,
            'priority': '40', 'capacity_default': '4',
            'elevenlabs_agent_id': 'agent_mx_test', 'caller_id': '+5255000000',
            'elevenlabs_phone_number_id': 'phnum_mx_test',
            'followup_policy_id': policy, 'recording_enabled': '0'})
        routes = [r for r in rc.routes_list(db, 'all')
                  if r.get('provider_code') == 'proveedor1']
        keys = sorted(r['route_key'] for r in routes)
        assert keys == ['IN_PROVEEDOR1', 'MX_PROVEEDOR1', 'NP_PROVEEDOR1'], keys
        providers = {r['provider_code'] for r in rc.routes_list(db, 'all')}
        assert providers == {'proveedor1', 'stringee'}, \
            f'se creó un proveedor por país: {providers}'
    s.check('PROVEEDOR1 tiene 3 rutas (IN, NP, MX) y sigue siendo UN proveedor',
            seed_more_routes)

    def two_providers_same_country():
        india = [r for r in rc.routes_list(db, 'all') if r['iso'] == 'IN']
        keys = sorted(r['route_key'] for r in india)
        assert keys == ['IN_PROVEEDOR1', 'IN_STRINGEE'], keys
        provs = sorted(r['provider_code'] for r in india)
        assert provs == ['proveedor1', 'stringee']
    s.check('India tiene 2 rutas activas de 2 proveedores distintos, a la vez',
            two_providers_same_country)

    def no_unique_country_provider():
        """No debe existir una restricción país -> proveedor."""
        pid = [p for p in rc.providers_list(db) if p['code'] == 'proveedor1'][0]['id']
        policy = rc.policies_list(db)[0]['id']
        rc.route_create(db, ACTOR, {
            'route_key': 'IN_PROVEEDOR1_ALT', 'iso': 'IN', 'provider_id': pid,
            'priority': '15', 'capacity_default': '2',
            'elevenlabs_agent_id': 'agent_alt', 'elevenlabs_phone_number_id': 'phnum_alt',
            'followup_policy_id': policy, 'recording_enabled': '0'})
        same = [r for r in rc.routes_list(db, 'all')
                if r['iso'] == 'IN' and r['provider_code'] == 'proveedor1']
        assert len(same) == 2, f'la base rechazó 2 rutas del mismo par país/proveedor'
    s.check('dos rutas del MISMO país y el MISMO proveedor conviven',
            no_unique_country_provider)

    def capacity_adds_up():
        """La demanda del país es la suma de las capacidades de sus rutas."""
        payload = rc.routes_active_payload(db, include_all=True)
        india = [r for r in payload['routes'] if r['iso'] == 'IN']
        total = sum(r['capacity_now'] for r in india if r['calling_now'])
        assert total > 0, 'India no tiene capacidad efectiva'
        per_route = {r['route_key']: r['capacity_now'] for r in india}
        assert per_route.get('IN_PROVEEDOR1') == 6, per_route
        assert per_route.get('IN_STRINGEE') == 1, per_route
        assert total == sum(v for v in per_route.values()), 'la suma no cuadra'
    s.check('la demanda del país es la suma de sus rutas (6 + 1 + 2 = 9)',
            capacity_adds_up)

    # ══ los tres interruptores ════════════════════════════════════════
    s.section('los tres interruptores son independientes')

    def route_switch():
        r = route_by_key('IN_PROVEEDOR1')
        rc.route_set_enabled(db, ACTOR, r['id'], False)
        assert not calling('IN_PROVEEDOR1')['calling_now'], 'la ruta apagada sigue llamando'
        assert 'ROUTE_DISABLED' in calling('IN_PROVEEDOR1')['blocked_by']
        assert calling('IN_STRINGEE')['calling_now'], \
            'apagar IN_PROVEEDOR1 apagó también IN_STRINGEE'
        rc.route_set_enabled(db, ACTOR, r['id'], True)
        assert calling('IN_PROVEEDOR1')['calling_now']
    s.check('RUTA OFF: solo esa ruta para; IN_STRINGEE sigue activa', route_switch)

    def provider_switch():
        pid = [p for p in rc.providers_list(db) if p['code'] == 'proveedor1'][0]['id']
        rc.provider_set_enabled(db, ACTOR, pid, False)
        for key in ('IN_PROVEEDOR1', 'IN_PROVEEDOR1_ALT'):
            c = calling(key)
            assert not c['calling_now'], f'{key} sigue llamando con el proveedor apagado'
            assert 'PROVIDER_DISABLED' in c['blocked_by']
        assert calling('IN_STRINGEE')['calling_now'], \
            'apagar PROVEEDOR1 apagó una ruta de otro proveedor'
        rc.provider_set_enabled(db, ACTOR, pid, True)
    s.check('PROVEEDOR OFF: paran todas sus rutas, en todos los países',
            provider_switch)

    def country_switch():
        rc.country_set_enabled(db, ACTOR, 'IN', False)
        for key in ('IN_PROVEEDOR1', 'IN_STRINGEE', 'IN_PROVEEDOR1_ALT'):
            c = calling(key)
            assert not c['calling_now'], f'{key} sigue llamando con India apagada'
            assert 'COUNTRY_DISABLED' in c['blocked_by']
        rc.country_set_enabled(db, ACTOR, 'IN', True)
        assert calling('IN_PROVEEDOR1')['calling_now']
    s.check('PAÍS OFF: paran TODAS las rutas de India, de cualquier proveedor',
            country_switch)

    def effective_condition():
        """calling_now = país ∧ proveedor ∧ ruta ∧ no archivada ∧ READY ∧ horario."""
        c = calling('IN_PROVEEDOR1')
        assert c['calling_now'] is True
        assert c['switches'] == {'country_enabled': True, 'provider_enabled': True,
                                 'route_enabled': True}, c['switches']
        assert c['ready'] and c['country_ready']
        assert c['blocked_by'] == []
    s.check('la condición efectiva combina los cinco factores', effective_condition)

    # ══ archivado ═════════════════════════════════════════════════════
    s.section('archivar en vez de borrar')

    def archive_route():
        r = route_by_key('IN_PROVEEDOR1_ALT')
        rc.route_archive(db, ACTOR, r['id'])
        c = calling('IN_PROVEEDOR1_ALT')
        assert not c['calling_now'], 'una ruta archivada sigue llamando'
        assert c['status'] == 'ARCHIVED'
        # pero SIGUE resolviendo por clave: un post-call tardío la necesita
        assert rc.route_by_key_payload(db, 'IN_PROVEEDOR1_ALT') is not None, \
            'la ruta archivada dejó de resolver: un post-call tardío se perdería'
    s.check('ruta archivada: no llama pero su route_key sigue resolviendo',
            archive_route)

    def archived_excluded_from_active():
        active = rc.routes_active_payload(db)
        keys = {r['route_key'] for r in active['routes']}
        assert 'IN_PROVEEDOR1_ALT' not in keys, 'una archivada aparece en las activas'
        arch = rc.routes_active_payload(db, include_archived=True)
        assert 'IN_PROVEEDOR1_ALT' in {r['route_key'] for r in arch['routes']}
    s.check('las archivadas salen de /active pero entran con include_archived',
            archived_excluded_from_active)

    def no_route_key_reuse():
        pid = [p for p in rc.providers_list(db) if p['code'] == 'stringee'][0]['id']
        try:
            rc.route_create(db, ACTOR, {
                'route_key': 'IN_PROVEEDOR1_ALT', 'iso': 'IN', 'provider_id': pid,
                'capacity_default': '1', 'priority': '90'})
            raise AssertionError('se reutilizó el route_key de una ruta archivada')
        except ValueError:          # ConfigError hereda de ValueError
            pass
    s.check('no se puede reutilizar el route_key de una ruta archivada',
            no_route_key_reuse)

    # ══ validación de ruta ════════════════════════════════════════════
    s.section('READY / NOT READY: no se activa una ruta incompleta')

    def incomplete_route_allowed_while_off():
        pid = [p for p in rc.providers_list(db) if p['code'] == 'proveedor1'][0]['id']
        rc.country_upsert(db, ACTOR, {
            'iso': 'CO', 'country_name': 'Colombia', 'dial_prefix': '+57',
            'national_number_len': '10', 'timezone': 'America/Bogota', 'language': 'es'})
        rc.route_create(db, ACTOR, {
            'route_key': 'CO_PROVEEDOR1', 'iso': 'CO', 'provider_id': pid,
            'capacity_default': '3', 'priority': '50'})     # sin agente: incompleta
        r = route_by_key('CO_PROVEEDOR1')
        assert r, 'no se pudo guardar una ruta incompleta estando apagada'
        rep = rc.validate_route(db, r)
        assert not rep['ready'], 'una ruta sin agente se reporta READY'
        fields = {i['field'] for i in rep['issues']}
        assert 'elevenlabs_agent_id' in fields, fields
        assert 'followup_policy' in fields, fields
    s.check('se puede guardar una ruta incompleta mientras está apagada',
            incomplete_route_allowed_while_off)

    def activation_blocked_until_ready():
        r = route_by_key('CO_PROVEEDOR1')
        try:
            rc.route_set_enabled(db, ACTOR, r['id'], True)
            raise AssertionError('se activó una ruta NOT READY')
        except rc.ConfigError as ex:
            assert ex.issues, 'el error no dice qué falta'
            msgs = ' '.join(i['message'] for i in ex.issues)
            assert 'Agent' in msgs or 'agent' in msgs, msgs
    s.check('activar una ruta NOT READY falla y dice exactamente qué falta',
            activation_blocked_until_ready)

    def ready_then_active():
        r = route_by_key('CO_PROVEEDOR1')
        policy = rc.policies_list(db)[0]['id']
        rc.route_update(db, ACTOR, r['id'], {
            'elevenlabs_agent_id': 'agent_co', 'elevenlabs_phone_number_id': 'phnum_co',
            'followup_policy_id': policy, 'capacity_default': '3',
            'priority': '50', 'recording_enabled': '0'})
        r = route_by_key('CO_PROVEEDOR1')
        assert rc.validate_route(db, r)['ready'], rc.validate_route(db, r)['issues']
        rc.route_set_enabled(db, ACTOR, r['id'], True)
        # el país nace apagado: la ruta está lista pero Colombia no llama
        c = calling('CO_PROVEEDOR1')
        assert not c['calling_now'], 'Colombia llama con el país apagado'
        assert 'COUNTRY_DISABLED' in c['blocked_by']
        rc.country_set_enabled(db, ACTOR, 'CO', True)
        assert calling('CO_PROVEEDOR1')['calling_now'], 'Colombia no llama con todo en ON'
    s.check('completar la config -> READY -> activar -> encender el país -> llama',
            ready_then_active)

    def runtime_revalidation():
        """Una ruta activa que queda inválida por una edición posterior deja de
        llamar sola (fail-closed), sin esperar a que alguien la desactive."""
        r = route_by_key('CO_PROVEEDOR1')
        db.execute("UPDATE call_routes SET elevenlabs_agent_id=NULL WHERE id=§", (r['id'],))
        c = calling('CO_PROVEEDOR1')
        assert not c['calling_now'], 'una ruta rota por un UPDATE manual sigue llamando'
        assert 'ROUTE_NOT_READY' in c['blocked_by']
        db.execute("UPDATE call_routes SET elevenlabs_agent_id='agent_co' WHERE id=§",
                   (r['id'],))
    s.check('un UPDATE manual que rompe la config apaga la ruta en caliente',
            runtime_revalidation)

    # ══ adapters ══════════════════════════════════════════════════════
    s.section('proveedor comercial != adapter técnico')

    def adapter_reuse():
        """PROVEEDOR2 con el MISMO adapter: cero código nuevo."""
        rc.provider_upsert(db, ACTOR, 'proveedor2', 'PROVEEDOR2', 'ELEVENLABS_SIP',
                           notes='segundo proveedor SIP')
        p2 = [p for p in rc.providers_list(db) if p['code'] == 'proveedor2'][0]
        assert p2['adapter_key'] == 'ELEVENLABS_SIP', \
            'PROVEEDOR2 no reutiliza el adapter de PROVEEDOR1'
        policy = rc.policies_list(db)[0]['id']
        rc.route_create(db, ACTOR, {
            'route_key': 'IN_PROVEEDOR2', 'iso': 'IN', 'provider_id': p2['id'],
            'capacity_default': '2', 'priority': '25',
            'elevenlabs_agent_id': 'agent_p2', 'elevenlabs_phone_number_id': 'phnum_p2',
            'followup_policy_id': policy, 'recording_enabled': '0'})
        r = route_by_key('IN_PROVEEDOR2')
        assert rc.validate_route(db, r)['ready'], rc.validate_route(db, r)['issues']
        rc.route_set_enabled(db, ACTOR, r['id'], True)
        rc.provider_set_enabled(db, ACTOR, p2['id'], True)
        assert calling('IN_PROVEEDOR2')['adapter_key'] == 'ELEVENLABS_SIP'
    s.check('un PROVEEDOR2 SIP reutiliza ELEVENLABS_SIP sin tocar n8n', adapter_reuse)

    def unsupported_adapter_cannot_activate():
        try:
            rc.provider_upsert(db, ACTOR, 'proveedor3', 'PROVEEDOR3', 'TWILIO_VOICE')
            p3 = [p for p in rc.providers_list(db) if p['code'] == 'proveedor3'][0]
            issues = rc.validate_provider(p3)
            assert issues, 'un adapter fuera del catálogo se reporta válido'
            codes = {i['field'] for i in issues}
            assert any('adapter_key' in c for c in codes), codes
        except rc.ConfigError:
            pass   # también es aceptable rechazarlo al guardar
    s.check('un adapter fuera del catálogo no puede quedar operativo',
            unsupported_adapter_cannot_activate)

    def adapter_requirements_differ():
        """Cada adapter exige campos distintos, y eso es config, no un if."""
        assert rc.ADAPTERS['ELEVENLABS_SIP']['route_requires'] == \
            ['elevenlabs_agent_id', 'elevenlabs_phone_number_id']
        assert rc.ADAPTERS['STRINGEE_WORKER']['route_requires'] == \
            ['elevenlabs_agent_id', 'caller_id']
        assert rc.ADAPTERS['STRINGEE_WORKER']['provider_requires_endpoint'] is True
    s.check('los requisitos de cada adapter viven en el catálogo, no en el código',
            adapter_requirements_differ)

    # ══ franjas horarias ══════════════════════════════════════════════
    s.section('capacidad por franja, en hora local del país')

    def capacity_windows():
        r = route_by_key('IN_PROVEEDOR1')
        for w in rc.windows_for_route(db, r['id']):
            rc.window_delete(db, ACTOR, w['id'])
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '09:00', '14:00', 5)
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '14:00', '15:00', 8)
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '15:00', '20:00', 5)
        # martes 10:30 en Asia/Kolkata = 05:00 UTC
        c = calling('IN_PROVEEDOR1', utc('2026-09-22T05:00:00Z'))
        assert c['capacity_now'] == 5, f'10:30 IST debería dar 5, dio {c["capacity_now"]}'
        # martes 14:30 IST = 09:00 UTC
        c = calling('IN_PROVEEDOR1', utc('2026-09-22T09:00:00Z'))
        assert c['capacity_now'] == 8, f'14:30 IST debería dar 8, dio {c["capacity_now"]}'
        # martes 21:00 IST = 15:30 UTC -> fuera de horario
        c = calling('IN_PROVEEDOR1', utc('2026-09-22T15:30:00Z'))
        assert not c['calling_now'], '21:00 IST debería estar fuera de horario'
        assert 'OUTSIDE_SCHEDULE' in c['blocked_by']
        assert c['capacity_now'] == 0
    s.check('9-14=5 · 14-15=8 · 15-20=5 · fuera de franja no llama', capacity_windows)

    def weekend_blocked():
        # domingo 10:30 IST -> ninguna franja lo cubre
        c = calling('IN_PROVEEDOR1', utc('2026-09-20T05:00:00Z'))
        assert not c['calling_now'], 'el domingo no debería llamar'
        assert 'OUTSIDE_SCHEDULE' in c['blocked_by']
    s.check('el fin de semana queda fuera de las franjas lun-vie', weekend_blocked)

    def cross_midnight_window():
        """Una franja 20:00 -> 02:00 cubre hasta las 2 del día SIGUIENTE."""
        r = route_by_key('MX_PROVEEDOR1')
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '20:00', '02:00', 7)
        rc.country_set_enabled(db, ACTOR, 'MX', True)
        rc.route_set_enabled(db, ACTOR, r['id'], True)
        # martes 21:00 en Ciudad de México (CST, UTC-6) = miércoles 03:00 UTC
        c = calling('MX_PROVEEDOR1', utc('2026-09-23T03:00:00Z'))
        assert c['capacity_now'] == 7, f'21:00 local debería dar 7, dio {c}'
        # miércoles 01:00 local = 07:00 UTC -> sigue DENTRO de la franja del martes
        c = calling('MX_PROVEEDOR1', utc('2026-09-23T07:00:00Z'))
        assert c['capacity_now'] == 7, f'01:00 local debería seguir dentro, dio {c}'
        # miércoles 03:00 local = 09:00 UTC -> fuera
        c = calling('MX_PROVEEDOR1', utc('2026-09-23T09:00:00Z'))
        assert not c['calling_now'], '03:00 local debería estar fuera'
    s.check('una franja que cruza medianoche se evalúa como un tramo continuo',
            cross_midnight_window)

    def no_current_country_uses_dst():
        """Dato real, no supuesto: NINGUNO de los países del sistema (IN, NP,
        MX, CO, VE) observa horario de verano hoy. México lo abolió en 2022.
        El manejo de DST es defensivo, para mercados futuros — y hay que
        probarlo con un huso que sí cambie."""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        for iso in ('IN', 'NP', 'MX', 'CO'):
            c = rc.country_get(db, iso)
            if not c:
                continue
            z = ZoneInfo(c['timezone'])
            offs = {_dt(2026, m, 15, 12, tzinfo=_tz.utc).astimezone(z).utcoffset()
                    for m in (1, 7)}
            assert len(offs) == 1, f'{iso} sí observa DST: revisar las franjas'
    s.check('ninguno de los países actuales observa DST (dato verificado)',
            no_current_country_uses_dst)

    def dst_shifts_utc_boundary():
        """La franja está en hora LOCAL. Al cambiar el horario, el instante UTC
        equivalente se corre una hora y la franja sigue valiendo 20:00-02:00.

        Se usa Europe/Madrid porque es un huso que SÍ cambia (último domingo de
        marzo de 2026: CET -> CEST). Es el escenario que rompería si las franjas
        se guardaran en UTC."""
        rc.country_upsert(db, ACTOR, {
            'iso': 'ES', 'country_name': 'Spain', 'dial_prefix': '+34',
            'national_number_len': '9', 'timezone': 'Europe/Madrid', 'language': 'es'})
        pid = [p for p in rc.providers_list(db) if p['code'] == 'proveedor1'][0]['id']
        policy = rc.policies_list(db)[0]['id']
        rc.route_create(db, ACTOR, {
            'route_key': 'ES_PROVEEDOR1', 'iso': 'ES', 'provider_id': pid,
            'capacity_default': '1', 'priority': '60',
            'elevenlabs_agent_id': 'agent_es', 'elevenlabs_phone_number_id': 'phnum_es',
            'followup_policy_id': policy, 'recording_enabled': '0'})
        r = route_by_key('ES_PROVEEDOR1')
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '20:00', '02:00', 7)
        rc.route_set_enabled(db, ACTOR, r['id'], True)
        rc.country_set_enabled(db, ACTOR, 'ES', True)

        # viernes 27/03 21:00 CET  = 20:00 UTC   (antes del cambio)
        before = calling('ES_PROVEEDOR1', utc('2026-03-27T20:00:00Z'))
        # martes  31/03 21:00 CEST = 19:00 UTC   (después del cambio)
        after = calling('ES_PROVEEDOR1', utc('2026-03-31T19:00:00Z'))
        assert before['capacity_source']['local_time'] == '21:00', before['capacity_source']
        assert after['capacity_source']['local_time'] == '21:00', after['capacity_source']
        assert before['capacity_now'] == 7, f'antes del cambio: {before["capacity_now"]}'
        assert after['capacity_now'] == 7, f'después del cambio: {after["capacity_now"]}'

        # el MISMO instante UTC cae en distinta hora local a un lado y al otro:
        # si las franjas estuvieran en UTC, una de las dos fallaría
        same_utc_before = calling('ES_PROVEEDOR1', utc('2026-03-27T19:00:00Z'))
        assert same_utc_before['capacity_source']['local_time'] == '20:00'
        assert after['capacity_source']['local_time'] == '21:00'
    s.check('la franja sigue el reloj local a través del cambio de horario (Madrid)',
            dst_shifts_utc_boundary)

    def overlapping_windows_rejected():
        r = route_by_key('IN_PROVEEDOR1')
        try:
            rc.window_add(db, ACTOR, r['id'], ['mon'], '10:00', '12:00', 3)
            raise AssertionError('se aceptó una franja solapada')
        except ValueError:
            pass
    s.check('dos franjas que se solapan se rechazan', overlapping_windows_rejected)

    def zero_capacity_blocks():
        r = route_by_key('IN_STRINGEE')
        rc.route_update(db, ACTOR, r['id'], {
            'capacity_default': '1', 'priority': '20', 'caller_id': '917971730907',
            'elevenlabs_agent_id': 'agent_stringee',
            'followup_policy_id': rc.policies_list(db)[0]['id'],
            'recording_enabled': '1', 'recording_min_secs': '60',
            'recording_upload_crm': '1', 'recording_telegram': '0'})
        rc.window_add(db, ACTOR, r['id'], ['mon', 'tue', 'wed', 'thu', 'fri'],
                      '00:00', '08:00', 0)
        # martes 05:00 IST = lunes 23:30 UTC
        c = calling('IN_STRINGEE', utc('2026-09-21T23:30:00Z'))
        assert not c['calling_now'], 'una franja con capacidad 0 sigue llamando'
        assert 'ZERO_CAPACITY' in c['blocked_by'] or 'OUTSIDE_SCHEDULE' in c['blocked_by']
    s.check('una franja con capacidad 0 no llama (turno de noche apagado)',
            zero_capacity_blocks)

    # ══ tools del país ════════════════════════════════════════════════
    s.section('las tools pertenecen al país, no al proveedor de voz')

    def tools_shared_between_routes():
        payload = rc.country_tools_payload(db, 'IN')
        assert payload, 'India no devuelve tools'
        assert 'CREATE_ACCOUNT' in payload['tools']
        assert payload['tools']['CREATE_ACCOUNT']['market'] == 'IND'
        # las dos rutas de India ven exactamente la misma config
        for key in ('IN_PROVEEDOR1', 'IN_STRINGEE'):
            r = calling(key)
            assert r['tools']['CREATE_ACCOUNT']['market'] == 'IND', \
                f'{key} ve otra config de cuenta'
    s.check('IN_PROVEEDOR1 e IN_STRINGEE usan la MISMA config de India',
            tools_shared_between_routes)

    def tool_validation():
        try:
            rc.tool_upsert(db, ACTOR, 'IN', 'CREATE_ACCOUNT', {
                'enabled': '1', 'mode': 'CONFIG_ROUTER', 'provider_key': 'cashstudio',
                'market': '', 'credential_ref': 'LEADSTUDIO_API'})
            t = [x for x in rc.tools_for_country(db, 'IN')
                 if x['tool_type'] == 'CREATE_ACCOUNT'][0]
            issues = rc.validate_tool(t)
            assert issues, 'una tool sin market se reporta válida'
        except rc.ConfigError:
            pass
    s.check('una tool CREATE_ACCOUNT sin market no es válida', tool_validation)

    def tool_rejects_secrets():
        try:
            rc.tool_upsert(db, ACTOR, 'IN', 'CREATE_PAYMENT_LINK', {
                'enabled': '1', 'mode': 'CUSTOM_ENDPOINT',
                'endpoint': 'https://pay.example.com/create',
                'http_method': 'POST', 'currency': 'INR',
                # SECSCAN-OK: valor falso; este test EXIGE que el panel lo rechace
                'config_json': '{"api_key": "sk_live_abc123"}'})
            raise AssertionError('se guardó una API key en config_json')
        except (rc.ConfigError, ValueError):
            pass
    s.check('config_json rechaza claves con pinta de secreto', tool_rejects_secrets)

    def country_off_blocks_tools():
        rc.country_set_enabled(db, ACTOR, 'IN', False)
        payload = rc.country_tools_payload(db, 'IN')
        assert payload['country_enabled'] is False, \
            'la API no avisa que el país está apagado: WF3/WF7 ejecutarían igual'
        rc.country_set_enabled(db, ACTOR, 'IN', True)
    s.check('con el país apagado, la API lo informa para que WF3/WF7 no ejecuten',
            country_off_blocks_tools)

    # ══ settings operativos ═══════════════════════════════════════════
    s.section('parámetros operativos, sin secretos')

    def settings_defaults():
        p = ws.settings_payload(db)
        assert p['settings']['crm_notes_language'] == 'en'
        assert p['settings']['tech_retry_max'] == 8
        assert isinstance(p['settings']['reconcile_dispatching_minutes'], int)
    s.check('/api/settings devuelve los umbrales sembrados, tipados', settings_defaults)

    def settings_reject_secrets():
        for key in ('leadstudio_password', 'elevenlabs_api_key', 'provider_secret'):
            try:
                ws.set_setting(db, ACTOR, key, 'whatever')
                raise AssertionError(f'se aceptó una clave con pinta de secreto: {key}')
            except ws.SettingError:
                pass
    s.check('wf_settings rechaza claves que parecen secretos', settings_reject_secrets)

    def settings_typed():
        try:
            ws.set_setting(db, ACTOR, 'tech_retry_max', 'muchos')
            raise AssertionError('se aceptó un entero inválido')
        except ws.SettingError:
            pass
        ws.set_setting(db, ACTOR, 'tech_retry_max', '12')
        assert ws.get_setting(db, 'tech_retry_max') == 12
    s.check('los valores se validan contra su tipo declarado', settings_typed)

    # ══ ledgers de idempotencia ═══════════════════════════════════════
    s.section('ledgers: una operación con efecto, una sola vez')

    def tool_claim_semantics():
        won1, _ = tr.claim(db, 'CREATE_ACCOUNT', 'lead-x', 'conv-x', country_iso='IN')
        won2, row = tr.claim(db, 'CREATE_ACCOUNT', 'lead-x', 'conv-x', country_iso='IN')
        assert won1 and not won2, 'dos ejecuciones ganaron el mismo claim'
        key = tr.request_key('CREATE_ACCOUNT', 'lead-x', 'conv-x')
        tr.mark_succeeded(db, key, result_ref='acc-1')
        assert tr.get(db, key)['state'] == 'SUCCEEDED'
        # cerrar dos veces no revierte
        assert tr.mark_failed(db, key, 'X') is False
        assert tr.get(db, key)['state'] == 'SUCCEEDED'
    s.check('claim de tool: gana uno, y el estado final no se pisa',
            tool_claim_semantics)

    def ambiguous_tool_needs_reconciliation():
        tr.claim(db, 'CREATE_PAYMENT_LINK', 'lead-y', 'conv-y', country_iso='IN')
        key = tr.request_key('CREATE_PAYMENT_LINK', 'lead-y', 'conv-y')
        tr.mark_needs_reconciliation(db, key, 'AMBIGUOUS', 'timeout')
        assert tr.get(db, key)['state'] == 'NEEDS_RECONCILIATION'
        # un nuevo intento del MISMO pedido no vuelve a ejecutar
        won, row = tr.claim(db, 'CREATE_PAYMENT_LINK', 'lead-y', 'conv-y')
        assert not won, 'un pedido ambiguo se reintentó solo'
    s.check('un pedido ambiguo queda para revisión y NO se reintenta solo',
            ambiguous_tool_needs_reconciliation)

    def recording_ledger_semantics():
        ok1 = rl.claim_recording(db, 'rec-1', 'STRINGEE_WORKER', call_job_id='j1',
                                 route_key='IN_STRINGEE', correlation='CALL_JOB')
        ok2 = rl.claim_recording(db, 'rec-1', 'STRINGEE_WORKER', call_job_id='j1')
        assert ok1 and not ok2, 'la misma grabación se reclamó dos veces'
        rl.mark_uploaded(db, 'rec-1', followup_id='fu-1')
        rl.mark_sent(db, 'rec-1')
        row = rl.get(db, 'rec-1')
        assert row['state'] == 'SENT' and row['crm_uploaded'] == 1
        assert row['correlation'] == 'CALL_JOB', \
            'no se registró CÓMO se correlacionó la grabación'
    s.check('claim de grabación + registro de cómo se correlacionó',
            recording_ledger_semantics)

    def short_recording_is_counted():
        rl.claim_recording(db, 'rec-2', 'STRINGEE_WORKER', call_job_id='j2',
                           duration_seconds=12, correlation='CALL_JOB')
        rl.mark_skipped_short(db, 'rec-2', 12, 60)
        row = rl.get(db, 'rec-2')
        assert row['state'] == 'SKIPPED_SHORT', row['state']
        assert row['crm_uploaded'] == 0, 'una grabación corta se subió al CRM'
    s.check('una grabación por debajo del mínimo se cuenta y NO se sube',
            short_recording_is_counted)

    def orphan_recording_not_uploaded():
        rl.claim_recording(db, 'rec-3', 'STRINGEE_WORKER', correlation='NONE')
        rl.mark_orphan(db, 'rec-3', 'sin llamada')
        row = rl.get(db, 'rec-3')
        assert row['state'] == 'ORPHAN'
        assert row['crm_uploaded'] == 0, \
            'una grabación sin correlacionar se subió: podría ir al lead equivocado'
    s.check('una grabación sin correlación NO se sube al CRM',
            orphan_recording_not_uploaded)

    # ══ atribución de cuentas ═════════════════════════════════════════
    s.section('la atribución es una convención de reporting, no un hecho')

    def attribution_model_declared():
        import analytics_v2 as av2
        assert av2.ATTRIBUTION_MODEL == 'LAST_CONNECTED_CALL_V1'
        assert av2.ATTRIBUTION_WINDOW_DAYS == 30
    s.check('el modelo de atribución está declarado y versionado',
            attribution_model_declared)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
