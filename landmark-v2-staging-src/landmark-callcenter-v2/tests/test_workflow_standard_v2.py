#!/usr/bin/env python3
"""Checklist de aceptación del N8N_TEMPLATE_STANDARD_V2_2, verificado sobre los
JSON generados.

No comprueba que el workflow "se vea bien": comprueba las 26 reglas del §12,
que son las que evitan los bugs concretos de v1. Cada test nombra el bug que
previene.
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

STICKY = 'n8n-nodes-base.stickyNote'
TRIGGERS = {'n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.webhook',
            'n8n-nodes-base.executeWorkflowTrigger', 'n8n-nodes-base.manualTrigger'}

REQUIRED = ['TEMPLATE_FOLLOWUP_ENGINE_V2', 'TEMPLATE_WF2_CALL_DISPATCHER_V2',
            'TEMPLATE_WF9_POST_CALL_HANDLER_V2', 'TEMPLATE_WF3_ACCOUNT_CREATION_V2',
            'TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2', 'TEMPLATE_WF10_RECORDINGS_V2',
            'TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2']

# Secretos reales que aparecían en los JSON de v1.
SECRET_RE = re.compile(
    r'sk_[A-Za-z0-9]{20,}|wsec_[A-Za-z0-9]{20,}|Admin@123|'
    r'"password"\s*:\s*"(?!\{\{|\$)[^"]{3,}"|cppwd["\']?\s*[,:]\s*["\'][A-Za-z0-9]{6,}|'
    r'a421742f0b404661b8ddb323c51cc99b|zUg8Dhgb|vduAtccg')

# Inferencias prohibidas (§1.2).
INFERENCE_RE = [
    (re.compile(r'phone[^\n]{0,40}\.startsWith\(\s*[\'"]\+'), 'proveedor deducido del "+" del teléfono'),
    (re.compile(r"country\s*===\s*['\"](india|nepal|mexico|colombia|venezuela)"), 'rama por país'),
    (re.compile(r'isStringee|is_stringee'), 'rama por proveedor'),
    (re.compile(r'\{\s*india\s*:'), 'mapa literal por país'),
]

# Patrones que rompen la idempotencia o el claim.
FORBIDDEN = [
    (re.compile(r'getWorkflowStaticData'), '$getWorkflowStaticData para deduplicar (§6.2.1)'),
    (re.compile(r'affectedRows|ROW_COUNT\(\)'), 'decidir un claim por affectedRows (§6.1)'),
    (re.compile(r'\bBATCH_SIZE\b'), 'capacidad de negocio hardcodeada (§6.6)'),
]


def main():
    s = S.Suite('N8N TEMPLATE STANDARD V2.2 · checklist de aceptación')
    wfs = S.load_workflows()

    s.section('el paquete trae los siete templates')

    def all_present():
        names = {wf['name'] for wf in wfs.values()}
        missing = [r for r in REQUIRED if r not in names]
        assert not missing, f'faltan: {missing}'
        assert len(wfs) == 7, f'se esperaban 7 workflows, hay {len(wfs)}'
    s.check('los 7 workflows exigidos están presentes', all_present)

    def valid_json_importable():
        for fname, wf in wfs.items():
            for key in ('name', 'nodes', 'connections', 'settings'):
                assert key in wf, f'{fname}: falta la clave {key}'
            assert isinstance(wf['nodes'], list) and wf['nodes'], f'{fname}: sin nodos'
            for n in wf['nodes']:
                for key in ('parameters', 'name', 'type', 'typeVersion', 'position', 'id'):
                    assert key in n, f'{fname}::{n.get("name")}: falta {key}'
                assert isinstance(n['position'], list) and len(n['position']) == 2, \
                    f'{fname}::{n["name"]}: position inválida'
            for src, conn in wf['connections'].items():
                names = {x['name'] for x in wf['nodes']}
                assert src in names, f'{fname}: conexión desde nodo inexistente {src}'
                for out in conn.get('main', []):
                    for t in out:
                        assert t['node'] in names, \
                            f'{fname}: conexión hacia nodo inexistente {t["node"]}'
    s.check('cada JSON es importable en n8n (estructura y conexiones válidas)',
            valid_json_importable)

    s.section('cero secretos en el JSON exportado')

    def no_secrets():
        offenders = []
        for fname, wf in wfs.items():
            blob = json.dumps(wf, ensure_ascii=False)
            for m in SECRET_RE.finditer(blob):
                offenders.append(f'{fname}: {m.group(0)[:40]}')
        assert not offenders, 'posibles secretos:\n  ' + '\n  '.join(offenders)
    s.check('ninguna API key, contraseña ni clave de firma en los JSON', no_secrets)

    def credentials_by_reference():
        for fname, wf in wfs.items():
            for n in wf['nodes']:
                for ctype, c in (n.get('credentials') or {}).items():
                    assert 'name' in c, f'{fname}::{n["name"]}: credential sin nombre'
                    assert str(c.get('id', '')).startswith('__'), \
                        (f'{fname}::{n["name"]}: la credential lleva un id real '
                         f'({c.get("id")}); debe ser un marcador de instalación')
    s.check('las credenciales viajan como referencia, no con el id real',
            credentials_by_reference)

    s.section('cero nodos huérfanos · cero desactivados · toda salida conectada')

    def no_orphans():
        offenders = []
        for fname, wf in wfs.items():
            targets = {t['node'] for c in wf['connections'].values()
                       for out in c.get('main', []) for t in out}
            for n in wf['nodes']:
                if n['type'] in (STICKY,) or n['type'] in TRIGGERS:
                    continue
                if n['name'] not in targets:
                    offenders.append(f'{fname}::{n["name"]}')
        assert not offenders, ('nodos sin entrada (en n8n no dan error: no corren) — '
                               'el bug de "Lead ID Válido?" de WF3 v1:\n  ' +
                               '\n  '.join(offenders))
    s.check('ningún nodo queda sin entrada', no_orphans)

    def no_disabled():
        offenders = [f'{f}::{n["name"]}' for f, wf in wfs.items() for n in wf['nodes']
                     if n.get('disabled')]
        assert not offenders, ('nodos desactivados (en n8n DEJAN PASAR los datos: '
                               'apagar desde el JSON es un no-fix):\n  ' +
                               '\n  '.join(offenders))
    s.check('ningún nodo desactivado', no_disabled)

    def if_outputs_connected():
        offenders = []
        for fname, wf in wfs.items():
            for n in wf['nodes']:
                outs = wf['connections'].get(n['name'], {}).get('main', [])
                if n['type'] == 'n8n-nodes-base.if':
                    if len(outs) < 2 or not all(outs[:2]):
                        offenders.append(f'{fname}::{n["name"]} (IF)')
                elif n['type'] == 'n8n-nodes-base.switch':
                    want = len(n['parameters']['rules']['values'])
                    if n['parameters'].get('options', {}).get('fallbackOutput') == 'extra':
                        want += 1
                    if len(outs) < want or not all(outs[:want]):
                        offenders.append(f'{fname}::{n["name"]} (Switch {len(outs)}/{want})')
        assert not offenders, 'salidas sin conectar:\n  ' + '\n  '.join(offenders)
    s.check('toda salida de IF y de Switch va a algún lado', if_outputs_connected)

    s.section('nomenclatura y layout')

    def naming():
        name_re = re.compile(r'^\[[A-Z0-9_]+\] [A-Za-z][A-Za-z0-9 ]*$')
        emoji = re.compile('[\U0001F000-\U0001FAFF☀-➿️]')
        offenders = []
        for fname, wf in wfs.items():
            for n in wf['nodes']:
                if n['type'] == STICKY:
                    continue
                if emoji.search(n['name']):
                    offenders.append(f'{fname}::{n["name"]} (emoji)')
                elif not name_re.match(n['name']):
                    offenders.append(f'{fname}::{n["name"]} (formato)')
                elif re.search(r'\d$', n['name']):
                    offenders.append(f'{fname}::{n["name"]} (sufijo numérico)')
        assert not offenders, 'nombres fuera de "[ÁMBITO] Acción":\n  ' + '\n  '.join(offenders)
    s.check('nombres [ÁMBITO] Acción, sin emojis ni sufijos numéricos', naming)

    def sticky_coverage():
        for fname, wf in wfs.items():
            stickies = [n for n in wf['nodes'] if n['type'] == STICKY]
            assert len(stickies) >= 6, \
                f'{fname}: solo {len(stickies)} sticky notes (una por sección)'
            header = [n for n in stickies if 'Contratos:' in n['parameters']['content']]
            assert header, f'{fname}: la nota 00 no lleva la cabecera de versión y contratos'
            for n in stickies:
                assert len(n['parameters']['content']) > 80, \
                    f'{fname}: sticky vacía o mínima: {n["name"]}'
    s.check('una sticky por sección, con cabecera de versión y contratos',
            sticky_coverage)

    def left_to_right():
        """Ningún nodo a la izquierda de otro del que depende."""
        offenders = []
        for fname, wf in wfs.items():
            pos = {n['name']: n['position'] for n in wf['nodes']}
            for src, conn in wf['connections'].items():
                for out in conn.get('main', []):
                    for t in out:
                        if t['node'] not in pos or src not in pos:
                            continue
                        if pos[t['node']][0] < pos[src][0]:
                            offenders.append(f'{fname}: {src} -> {t["node"]}')
        assert not offenders, 'conexiones hacia la izquierda:\n  ' + '\n  '.join(offenders[:10])
    s.check('layout izquierda a derecha: sin conexiones hacia atrás', left_to_right)

    s.section('cero inferencias · cero literales de negocio')

    def no_inference():
        offenders = []
        for fname, wf in wfs.items():
            for n in S.code_nodes(wf):
                code = n['parameters'].get('jsCode', '')
                # los comentarios explican qué NO se hace: no cuentan
                body = '\n'.join(l for l in code.split('\n')
                                 if not l.strip().startswith('//'))
                for rx, why in INFERENCE_RE:
                    if rx.search(body):
                        offenders.append(f'{fname}::{n["name"]}: {why}')
        assert not offenders, 'inferencias prohibidas:\n  ' + '\n  '.join(offenders)
    s.check('nada de deducir país o proveedor del teléfono', no_inference)

    def no_forbidden_patterns():
        offenders = []
        for fname, wf in wfs.items():
            for n in wf['nodes']:
                # Las sticky notes DESCRIBEN lo que está prohibido ("nunca
                # $getWorkflowStaticData"): documentarlo no es usarlo.
                if n['type'] == STICKY:
                    continue
                blob = json.dumps(n.get('parameters', {}), ensure_ascii=False)
                body = '\n'.join(l for l in blob.split('\\n')
                                 if not l.strip().startswith('//'))
                for rx, why in FORBIDDEN:
                    if rx.search(body):
                        offenders.append(f'{fname}::{n["name"]}: {why}')
        assert not offenders, 'patrones prohibidos:\n  ' + '\n  '.join(offenders)
    s.check('sin staticData para deduplicar, sin affectedRows, sin BATCH_SIZE',
            no_forbidden_patterns)

    def no_hardcoded_business_values():
        """Agentes, chats de Telegram, prefijos y capacidades salen de la API."""
        bad = [
            (re.compile(r'agent_[0-9a-z]{20,}'), 'elevenlabs_agent_id hardcodeado'),
            (re.compile(r'phnum_[0-9a-zA-Z]{20,}'), 'phone_number_id hardcodeado'),
            (re.compile(r'-100\d{10,}'), 'chat_id de Telegram hardcodeado'),
        ]
        offenders = []
        for fname, wf in wfs.items():
            blob = json.dumps(wf, ensure_ascii=False)
            for rx, why in bad:
                for m in rx.finditer(blob):
                    offenders.append(f'{fname}: {why} ({m.group(0)[:24]})')
        assert not offenders, 'literales de negocio:\n  ' + '\n  '.join(offenders)
    s.check('sin agent_id, phone_number_id ni chat_id de Telegram en los nodos',
            no_hardcoded_business_values)

    s.section('SQL parametrizado')

    def sql_parameterised():
        offenders = []
        for fname, wf in wfs.items():
            for n in S.mysql_nodes(wf):
                q = n['parameters'].get('query', '')
                assert n['parameters'].get('operation') == 'executeQuery', \
                    f'{fname}::{n["name"]}: operación inesperada'
                # ninguna interpolación de expresiones dentro del SQL
                if '{{' in q:
                    offenders.append(f'{fname}::{n["name"]}: expresión dentro del SQL')
                nph = q.count('?')
                repl = n['parameters'].get('options', {}).get('queryReplacement', '')
                nrep = len(re.findall(r'\{\{', repl))
                if nph and nrep != nph:
                    offenders.append(
                        f'{fname}::{n["name"]}: {nph} placeholders y {nrep} valores')
                if nph == 0 and repl:
                    offenders.append(f'{fname}::{n["name"]}: valores sin placeholders')
        assert not offenders, 'SQL mal parametrizado:\n  ' + '\n  '.join(offenders)
    s.check('todo SQL usa ? con queryReplacement, sin interpolar expresiones',
            sql_parameterised)

    def no_sql_injection_surface():
        """Un SQL armado por concatenación en un Code node es lo que v1 hacía."""
        offenders = []
        for fname, wf in wfs.items():
            for n in S.code_nodes(wf):
                code = n['parameters'].get('jsCode', '')
                if re.search(r"(INSERT|UPDATE|DELETE|SELECT)\s+[A-Za-z_]+.{0,80}?['\"]\s*\+",
                             code, re.I | re.S):
                    offenders.append(f'{fname}::{n["name"]}')
        assert not offenders, ('SQL construido por concatenación en un Code node:\n  ' +
                               '\n  '.join(offenders))
    s.check('ningún Code node arma SQL concatenando strings', no_sql_injection_surface)

    s.section('neverError siempre seguido de un check explícito')

    def never_error_checked():
        offenders = []
        for fname, wf in wfs.items():
            byname = {n['name']: n for n in wf['nodes']}
            for n in wf['nodes']:
                if n['type'] != 'n8n-nodes-base.httpRequest':
                    continue
                opts = n['parameters'].get('options', {}).get('response', {}).get('response', {})
                if not opts.get('neverError'):
                    continue
                # Se recorre la cadena aguas abajo hasta encontrar un nodo que
                # EVALÚE la respuesta. Se permite encadenar otra lectura de
                # configuración en el medio (p. ej. routes -> settings -> check)
                # siempre que esa lectura NO consuma el cuerpo de la anterior.
                seen, frontier, checked = set(), [n['name']], False
                while frontier:
                    cur = frontier.pop(0)
                    for out in wf['connections'].get(cur, {}).get('main', []):
                        for t in out:
                            nxt = byname.get(t['node'])
                            if not nxt or nxt['name'] in seen:
                                continue
                            seen.add(nxt['name'])
                            if nxt['type'] in ('n8n-nodes-base.code', 'n8n-nodes-base.if',
                                               'n8n-nodes-base.switch'):
                                checked = True
                            elif nxt['type'] == 'n8n-nodes-base.httpRequest':
                                blob = json.dumps(nxt.get('parameters', {}))
                                if '$json.body' in blob or '$json.statusCode' in blob:
                                    # asume éxito de la respuesta anterior
                                    continue
                                frontier.append(nxt['name'])
                    if checked:
                        break
                if not (checked or not wf['connections'].get(n['name'], {}).get('main')):
                    offenders.append(f'{fname}::{n["name"]}')
        assert not offenders, ('neverError sin check (en v1 un 404 y un 200 se '
                               'procesaban igual):\n  ' + '\n  '.join(offenders))
    s.check('cada HTTP con neverError va seguido de un nodo que lo evalúa',
            never_error_checked)

    s.section('reglas propias de cada template')

    def adapter_shape():
        """Un adapter tiene exactamente 4 pasos y no contiene [CRM]/[DB]/[ENGINE]."""
        wf = wfs['TEMPLATE_WF2_CALL_DISPATCHER_V2.json']
        names = [n['name'] for n in wf['nodes'] if n['type'] != STICKY]
        for kind in ('ELEVENLABS_SIP', 'STRINGEE_WORKER'):
            steps = [n for n in names if n.startswith(f'[{kind}]')]
            assert len(steps) == 3, \
                f'{kind}: {len(steps)} nodos, se esperaban 3 (Build/Dispatch/Normalize)'
            assert any('Build Request' in x for x in steps), f'{kind}: falta Build Request'
            assert any('Normalize Dispatch' in x for x in steps), \
                f'{kind}: falta Normalize Dispatch'
        for n in wf['nodes']:
            if n['name'].startswith(('[ELEVENLABS_SIP]', '[STRINGEE_WORKER]')):
                assert n['type'] != 'n8n-nodes-base.mySql', \
                    f'{n["name"]}: un adapter no toca la base'
    s.check('WF2: cada adapter tiene 4 pasos y no toca la base ni el CRM', adapter_shape)

    def routes_by_adapter():
        wf = wfs['TEMPLATE_WF2_CALL_DISPATCHER_V2.json']
        sw = [n for n in wf['nodes'] if n['name'] == '[ROUTE] Resolve Adapter']
        assert sw, 'falta el Switch [ROUTE] Resolve Adapter'
        expr = json.dumps(sw[0]['parameters'])
        assert 'adapter_key' in expr, 'el Switch no ramifica por adapter_key'
        assert 'provider' not in expr.replace('adapter_key', ''), \
            'el Switch ramifica por proveedor'
    s.check('WF2: ramifica por adapter_key, nunca por proveedor ni país',
            routes_by_adapter)

    def dispatching_before_http():
        """El estado DISPATCHING se escribe ANTES del HTTP al proveedor."""
        wf = wfs['TEMPLATE_WF2_CALL_DISPATCHER_V2.json']
        pos = {n['name']: n['position'][0] for n in wf['nodes']}
        mark = pos['[DB] Mark Dispatching']
        for name in ('[ELEVENLABS_SIP] Dispatch', '[STRINGEE_WORKER] Dispatch Worker'):
            assert pos[name] > mark, f'{name} está antes de [DB] Mark Dispatching'
        # y está conectado aguas arriba del Switch de adapters
        reach, frontier = set(), ['[DB] Mark Dispatching']
        while frontier:
            cur = frontier.pop()
            for out in wf['connections'].get(cur, {}).get('main', []):
                for t in out:
                    if t['node'] not in reach:
                        reach.add(t['node']); frontier.append(t['node'])
        assert '[ROUTE] Resolve Adapter' in reach, \
            'Mark Dispatching no precede al routing de adapters'
    s.check('WF2: DISPATCHING se escribe antes del HTTP al proveedor',
            dispatching_before_http)

    def only_engine_creates_followups():
        offenders = []
        for fname, wf in wfs.items():
            for n in wf['nodes']:
                if n['type'] != 'n8n-nodes-base.httpRequest':
                    continue
                url = str(n['parameters'].get('url', ''))
                body = str(n['parameters'].get('jsonBody', ''))
                if '/followups' not in url:
                    continue
                # crear un FOLLOW-UP de llamada (type CALL) solo lo hace el motor
                if '"type": \'CALL\'' in body or "type: 'CALL'" in body or '"CALL"' in body:
                    if wf['name'] != 'TEMPLATE_FOLLOWUP_ENGINE_V2':
                        offenders.append(f'{fname}::{n["name"]}')
        assert not offenders, ('solo el motor crea follow-ups de llamada:\n  ' +
                               '\n  '.join(offenders))
    s.check('solo el motor crea follow-ups de tipo CALL', only_engine_creates_followups)

    def no_policy_outside_engine():
        """Nada de +2h/+3h/días hábiles fuera del motor."""
        offenders = []
        for fname, wf in wfs.items():
            if wf['name'] == 'TEMPLATE_FOLLOWUP_ENGINE_V2':
                continue
            for n in S.code_nodes(wf):
                code = n['parameters'].get('jsCode', '')
                body = '\n'.join(l for l in code.split('\n')
                                 if not l.strip().startswith('//'))
                if re.search(r'addBusinessDays|posInCycle|%\s*3\b|\+\s*2\s*\*\s*3600|'
                             r'scheduleNextAt\s*=', body):
                    offenders.append(f'{fname}::{n["name"]}')
        assert not offenders, ('política de follow-up fuera del motor:\n  ' +
                               '\n  '.join(offenders))
    s.check('ningún workflow calcula reintentos fuera del motor',
            no_policy_outside_engine)

    def wf9_resolves_archived_routes():
        wf = wfs['TEMPLATE_WF9_POST_CALL_HANDLER_V2.json']
        blob = json.dumps(wf)
        assert 'all=1' in blob or 'include_archived' in blob, \
            'WF9 no pide las rutas incluyendo las no activas'
        wf10 = wfs['TEMPLATE_WF10_RECORDINGS_V2.json']
        assert 'include_archived=1' in json.dumps(wf10), \
            'WF10 debe resolver rutas archivadas (grabación tardía)'
        eng = wfs['TEMPLATE_FOLLOWUP_ENGINE_V2.json']
        assert '/api/routes/by-key/' in json.dumps(eng), \
            'el motor debe resolver la ruta por clave'
    s.check('WF9/WF10/motor resuelven rutas archivadas', wf9_resolves_archived_routes)

    def wf14_is_not_analytics_source():
        wf = wfs['TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2.json']
        blob = json.dumps(wf)
        assert '/api/leads/queue' not in blob, \
            'WF14 no debe consumir la cola de llamadas (le robaría leads a WF2)'
        assert 'wf_reconciliation_issues' in blob, 'WF14 no abre issues'
        for n in S.mysql_nodes(wf):
            q = n['parameters']['query'].upper()
            assert 'DELETE FROM WF_EVENTS' not in q, 'WF14 no puede borrar eventos'
            assert 'DELETE FROM WF_CALL_JOBS' not in q, 'WF14 no puede borrar llamadas'
    s.check('WF14 reconcilia y no consume la cola ni borra datos locales',
            wf14_is_not_analytics_source)

    def workflows_inactive():
        for fname, wf in wfs.items():
            assert wf.get('active') is False, \
                f'{fname}: viene con active=true; debe importarse desactivado'
    s.check('todos los templates vienen desactivados (no arrancan al importar)',
            workflows_inactive)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
