#!/usr/bin/env python3
"""Paridad del motor de follow-up: JS (n8n) vs Python (referencia).

El motor real corre como JavaScript dentro de un Code node de n8n. La
implementación de REFERENCIA, la que cubren los 30+ tests de la fundación, está
en Python (`app/followup_engine.py`).

Si los dos divergen, el comportamiento probado no es el que corre en producción.
Esta suite ejecuta AMBOS con los mismos casos —incluidos cambios de horario
reales— y exige resultado idéntico.

Además verifica la política STANDARD_CALL_RETRY intento por intento (1..9 y el
CLOSE del 9) directamente sobre el JS extraído del workflow generado.
"""
import json
import os
import re
import sys

# Importar los módulos del suite no debe dejar .pyc dentro del paquete:
# lo que se empaqueta tiene que salir limpio.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _suite as S

import followup_engine as fe

TZS = ['Asia/Kolkata', 'Asia/Kathmandu', 'America/Mexico_City', 'America/Bogota',
       'America/Caracas', 'Europe/Madrid', 'UTC']
# Instantes elegidos a propósito: vísperas de cambio de horario en México y
# Madrid, fin de semana, fin de año y cruces de medianoche local.
NOWS = ['2026-09-18T04:30:00Z', '2026-09-18T23:45:00Z', '2026-09-19T18:00:00Z',
        '2026-03-07T12:00:00Z', '2026-04-04T21:30:00Z', '2026-10-24T22:10:00Z',
        '2026-10-31T23:30:00Z', '2026-12-31T18:00:00Z', '2026-11-01T05:00:00Z',
        '2027-02-26T19:00:00Z']
RESULTS = ['NO_ANSWER', 'ANSWERED', 'BUSY', 'VOICEMAIL', 'CALLBACK', 'WRONG_NUMBER',
           'DNC', 'FAILED', 'UNKNOWN']
SIPS = [None, '603', '408', '486', '404', '484', '500', '200']


def seeded_policy():
    """La política STANDARD_CALL_RETRY exactamente como la siembra la migración."""
    sql = open(os.path.join(S.ROOT, 'sql', 'parts',
                            '001_multi_country_config_v2_2.sql')).read()
    m = re.search(r"('\{\"policy_version\":1.*?\}')\s*\nFROM DUAL", sql, re.S)
    assert m, 'no se encontró la política sembrada en la migración'
    return json.loads(m.group(1)[1:-1])


def engine_js():
    js = S.load_js()
    return js['lmcore.js'] + js['lmengine.js']


def build_cases():
    cases = []
    for tz in TZS:
        for now in NOWS:
            for attempt in range(1, 13):
                for result in RESULTS:
                    sips = SIPS if result in ('FAILED', 'UNKNOWN') else [None]
                    for sip in sips:
                        cases.append({
                            'tz': tz, 'now': now, 'attempt': attempt, 'result': result,
                            'sip': sip,
                            'callback_at': ('2026-09-19T10:00:00Z'
                                            if result == 'CALLBACK' and attempt % 2 else None),
                        })
    return cases


def py_results(policy, cases):
    from datetime import datetime
    out = []
    for c in cases:
        now = datetime.fromisoformat(c['now'].replace('Z', '+00:00'))
        try:
            d = fe.resolve(policy, c['attempt'], c['result'], now_utc=now, tz_name=c['tz'],
                           callback_at=c['callback_at'], sip_code=c['sip'])
            out.append({k: d[k] for k in ('action', 'schedule_next_at', 'effective_result',
                                          'delay', 'crm_status_hint', 'sip_code')})
        except Exception as ex:
            out.append({'error': f'{type(ex).__name__}'})
    return out


def js_results(policy, cases):
    return S.run_node(engine_js(), r'''
const out = __payload.cases.map(c => {
  try {
    const d = lmResolve(__payload.policy, c.attempt, c.result, new Date(c.now),
                        c.tz, c.callback_at, c.sip);
    return { action: d.action, schedule_next_at: d.schedule_next_at,
             effective_result: d.effective_result, delay: d.delay,
             crm_status_hint: d.crm_status_hint, sip_code: d.sip_code };
  } catch (e) { return { error: e.constructor.name }; }
});
console.log(JSON.stringify(out));
''', {'policy': policy, 'cases': cases})


def main():
    s = S.Suite('MOTOR DE FOLLOW-UP · paridad JS/Python')
    policy = seeded_policy()
    cases = build_cases()

    s.section(f'paridad exhaustiva ({len(cases)} casos · {len(TZS)} husos · DST real)')
    py = py_results(policy, cases)
    js = js_results(policy, cases)

    def parity():
        assert len(py) == len(js) == len(cases), 'distinta cantidad de resultados'
        diffs = [(c, a, b) for c, a, b in zip(cases, py, js) if a != b]
        detail = ''
        if diffs:
            c, a, b = diffs[0]
            detail = f' primer caso: {c} py={a} js={b}'
        assert not diffs, f'{len(diffs)}/{len(cases)} casos divergen.{detail}'
    s.check(f'JS y Python coinciden en los {len(cases)} casos', parity)

    def dst_covered():
        # Que los casos realmente atraviesen un cambio de horario: si no, la
        # paridad no prueba nada sobre DST.
        crossing = 0
        for c, a in zip(cases, py):
            if a.get('schedule_next_at') and c['tz'] in ('America/Mexico_City', 'Europe/Madrid'):
                from datetime import datetime
                from zoneinfo import ZoneInfo
                n = datetime.fromisoformat(c['now'].replace('Z', '+00:00'))
                t = datetime.fromisoformat(a['schedule_next_at'].replace('Z', '+00:00'))
                z = ZoneInfo(c['tz'])
                if n.astimezone(z).utcoffset() != t.astimezone(z).utcoffset():
                    crossing += 1
        assert crossing > 0, 'ningún caso cruza un cambio de horario'
    s.check('los casos incluyen cruces reales de horario de verano', dst_covered)

    s.section('política STANDARD_CALL_RETRY, intento por intento')
    expected = {1: ('RETRY', '+2h'), 2: ('RETRY', '+3h'), 3: ('RETRY', '+2bd'),
                4: ('RETRY', '+2h'), 5: ('RETRY', '+3h'), 6: ('RETRY', '+3bd'),
                7: ('RETRY', '+2h'), 8: ('RETRY', '+3h'), 9: ('CLOSE', None)}
    got = S.run_node(engine_js(), r'''
const out = {};
for (let a = 1; a <= 12; a++) {
  const d = lmResolve(__payload.policy, a, 'NO_ANSWER',
                      new Date('2026-09-18T04:30:00Z'), 'Asia/Kolkata', null, null);
  out[a] = [d.action, d.delay, d.schedule_next_at];
}
console.log(JSON.stringify(out));
''', {'policy': policy})

    for attempt, (act, delay) in expected.items():
        def one(a=attempt, e_act=act, e_delay=delay):
            g = got[str(a)]
            assert g[0] == e_act, f'intento {a}: acción {g[0]}, se esperaba {e_act}'
            assert g[1] == e_delay, f'intento {a}: delay {g[1]}, se esperaba {e_delay}'
            if e_act == 'RETRY':
                assert g[2], f'intento {a}: RETRY sin schedule_next_at'
            else:
                assert g[2] is None, f'intento {a}: CLOSE no debe programar nada'
        s.check(f'NO_ANSWER intento {attempt} -> {act} {delay or ""}'.strip(), one)

    def beyond_nine():
        for a in (10, 11, 12):
            assert got[str(a)][0] == 'CLOSE', f'intento {a} debería cerrar (regla comodín)'
    s.check('intentos >= 10 cierran por la regla comodín (red de seguridad)', beyond_nine)

    def business_days():
        # viernes 10:00 Kolkata + 2 días hábiles -> martes 10:00 local
        g = got['3'][2]
        assert g == '2026-09-22T04:30:00Z', f'+2bd dio {g}, se esperaba martes 10:00 local'
        g6 = got['6'][2]
        assert g6 == '2026-09-23T04:30:00Z', f'+3bd dio {g6}, se esperaba miércoles 10:00 local'
    s.check('+2bd y +3bd saltan el fin de semana y conservan la hora local', business_days)

    s.section('reclasificación SIP: es POLÍTICA, no del adapter')
    sipres = S.run_node(engine_js(), r'''
const out = {};
for (const code of ['603', '408', '486', '404', '484']) {
  const d = lmResolve(__payload.policy, 1, 'FAILED',
                      new Date('2026-09-18T04:30:00Z'), 'Asia/Kolkata', null, code);
  out[code] = [d.effective_result, d.action];
}
out['none'] = (() => { const d = lmResolve(__payload.policy, 1, 'FAILED',
  new Date('2026-09-18T04:30:00Z'), 'Asia/Kolkata', null, null);
  return [d.effective_result, d.action]; })();
console.log(JSON.stringify(out));
''', {'policy': policy})

    def sip_no_answer():
        for code in ('603', '408', '486'):
            assert sipres[code][0] == 'NO_ANSWER', \
                f'SIP {code} debería reclasificar a NO_ANSWER, dio {sipres[code][0]}'
            assert sipres[code][1] == 'RETRY', f'SIP {code} debería reintentar'
    s.check('603/408/486 se reclasifican a NO_ANSWER por no_answer_sip_codes', sip_no_answer)

    def sip_not_listed():
        for code in ('404', '484'):
            assert sipres[code][0] == 'FAILED', \
                f'SIP {code} no está en la lista: debe quedar FAILED, dio {sipres[code][0]}'
            assert sipres[code][1] == 'NONE', \
                f'FAILED sin regla debe caer en unmatched_action=NONE'
    s.check('un SIP fuera de la lista queda FAILED y no reintenta', sip_not_listed)

    s.section('el motor rechaza lo que no le corresponde')
    rej = S.run_node(engine_js(), r'''
const out = {};
function t(fn) { try { fn(); return 'NO_ERROR'; } catch (e) { return e.message; } }
out.dispatched = t(() => lmResolve(__payload.policy, 1, 'DISPATCHED', new Date(), 'UTC'));
out.attempt0 = t(() => lmResolve(__payload.policy, 0, 'NO_ANSWER', new Date(), 'UTC'));
out.bogus = t(() => lmResolve(__payload.policy, 1, 'AUTH_ERROR', new Date(), 'UTC'));
out.badpolicy = t(() => lmResolve({rules: []}, 1, 'NO_ANSWER', new Date(), 'UTC'));
out.baddelay = t(() => lmResolve({rules: [{result: 'NO_ANSWER', attempt: 1,
  action: 'RETRY', delay: '2h'}]}, 1, 'NO_ANSWER', new Date(), 'UTC'));
console.log(JSON.stringify(out));
''', {'policy': policy})

    s.check('DISPATCHED es rechazado: no es un resultado final',
            lambda: (_ for _ in ()).throw(AssertionError(rej['dispatched']))
            if rej['dispatched'] == 'NO_ERROR' else None)
    s.check('attempt < 1 es rechazado',
            lambda: (_ for _ in ()).throw(AssertionError('aceptó attempt=0'))
            if rej['attempt0'] == 'NO_ERROR' else None)
    s.check('un código técnico (AUTH_ERROR) no es un resultado de llamada',
            lambda: (_ for _ in ()).throw(AssertionError('aceptó AUTH_ERROR'))
            if rej['bogus'] == 'NO_ERROR' else None)
    s.check('una política sin reglas es rechazada',
            lambda: (_ for _ in ()).throw(AssertionError('aceptó rules vacías'))
            if rej['badpolicy'] == 'NO_ERROR' else None)
    s.check('un delay mal formado es rechazado',
            lambda: (_ for _ in ()).throw(AssertionError('aceptó delay "2h"'))
            if rej['baddelay'] == 'NO_ERROR' else None)

    s.section('el JS embebido en el workflow es el mismo que el de tools/js')
    def embedded_matches():
        wfs = S.load_workflows()
        wf = wfs['TEMPLATE_FOLLOWUP_ENGINE_V2.json']
        node = [n for n in S.code_nodes(wf)
                if n['name'] == '[ENGINE] Resolve Policy And Schedule']
        assert node, 'falta el nodo [ENGINE] Resolve Policy And Schedule'
        code = node[0]['parameters']['jsCode']
        src = S.load_js()['lmengine.js']
        # el cuerpo del motor debe estar inlineado íntegro
        marker = 'function lmResolve(policy, attempt, result'
        assert marker in code, 'el nodo no lleva lmResolve inlineado'
        for fn in ('lmValidatePolicy', 'lmApplyDelay', 'lmClassifySipCode', 'lmFindRule'):
            assert f'function {fn}' in code, f'falta {fn} en el nodo'
        assert src.count('function lmResolve') == 1
    s.check('el nodo del motor lleva lmengine.js completo, no una copia parcial',
            embedded_matches)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
