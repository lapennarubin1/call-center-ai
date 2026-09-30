"""
wfbuild.py — Constructor de los templates n8n V2
=================================================

Por qué un constructor y no JSON escrito a mano
-----------------------------------------------
Siete workflows, ~250 nodos, con reglas de layout obligatorias
(N8N_TEMPLATE_STANDARD §2: secciones cada 460 px, ramas cada 220 px, sin nodos
huérfanos, sin salidas de IF sin conectar, una sticky note por sección). A mano
eso se rompe en la primera edición y el error no se ve hasta importar.

Acá el layout se CALCULA, las conexiones se declaran por nombre, y
`validate()` hace cumplir las prohibiciones del estándar antes de escribir el
archivo. Regenerar es `python3 tools/build_workflows.py`.

Reglas que se verifican automáticamente (build_workflows.py las corre):
  · nombres `[ÁMBITO] Acción`, sin emojis, sin sufijos numéricos
  · cero nodos huérfanos (todo nodo no-trigger tiene entrada)
  · cero nodos `disabled`
  · toda salida de IF/Switch conectada
  · cero secretos en el JSON
  · sticky note por sección
"""

import json
import re
import uuid

X0, DX, DY, Y0 = 0, 460, 220, 300
COL = 260      # ancho de una columna dentro de una sección

# Tipos de nodo que no necesitan entrada.
TRIGGERS = {
    'n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.webhook',
    'n8n-nodes-base.executeWorkflowTrigger', 'n8n-nodes-base.manualTrigger',
    'n8n-nodes-base.errorTrigger',
}
STICKY = 'n8n-nodes-base.stickyNote'

NAME_RE = re.compile(r'^\[[A-Z0-9_]+\] [A-Za-z0-9].*$')
EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF←-⇿☀-➿️]')
NUM_SUFFIX_RE = re.compile(r'\d$')

# Cualquier cosa con pinta de secreto en el JSON exportado es un fallo de build.
SECRET_RE = re.compile(
    r'(sk_[A-Za-z0-9]{16,}|xi-api-key["\']?\s*[:=]\s*["\'][^"\']{8,}|'
    r'"password"\s*:\s*"(?!\{\{)[^"]{3,}"|Admin@\d|cppwd=[A-Za-z0-9]{4,}|'
    r'wsec_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9]{20,})')


def nid():
    return str(uuid.uuid4())


class Workflow:
    def __init__(self, name, version, contracts, rollback, trigger_note):
        self.name = name
        self.version = version
        self.nodes = []
        self.conns = {}
        self.by_name = {}
        self.sections = []
        self.meta_header = (
            f"{name} · v{version}\n"
            f"Contratos: {contracts}\n"
            f"Rollback: {rollback}"
        )
        self.trigger_note = trigger_note

    # ── posición ──────────────────────────────────────────────────────
    #  La X NO se calcula como section*DX: con varios `offset` dentro de una
    #  sección, un nodo puede terminar a la derecha del inicio de la sección
    #  siguiente y la conexión iría "hacia atrás" en el canvas. `layout()`
    #  reserva a cada sección el ancho que sus offsets realmente ocupan.
    def pos(self, section, branch=0, offset=0):
        return [X0 + (section * DX) + (offset * COL), Y0 + branch * DY]

    def layout(self):
        """Asigna la X definitiva: cada sección empieza después de que la
        anterior terminó. Garantiza el layout izquierda-a-derecha del §2.1."""
        widest = {}
        for n in self.nodes:
            g = n.get('_grid')
            if not g:
                continue
            widest[g[0]] = max(widest.get(g[0], 0), g[2])
        base, x = {}, X0
        for section in sorted(widest):
            base[section] = x
            x += max(DX, (widest[section] + 1) * COL)
        for n in self.nodes:
            g = n.get('_grid')
            if not g:
                continue
            section, branch, offset = g
            n['position'] = [base[section] + offset * COL, Y0 + branch * DY]
        # las sticky notes se apoyan en la base de su sección
        for n in self.nodes:
            g = n.get('_sticky')
            if g is None:
                continue
            section, branch = g
            n['position'] = [base.get(section, X0 + section * DX) - 20, Y0 + branch * DY - 40]
        return base

    # ── nodos ─────────────────────────────────────────────────────────
    def add(self, name, type_, params, section, branch=0, offset=0,
            type_version=1, credentials=None, extra=None, notes=None):
        if name in self.by_name:
            raise ValueError(f'nodo duplicado: {name}')
        node = {
            'parameters': params,
            'id': nid(),
            'name': name,
            'type': type_,
            'typeVersion': type_version,
            'position': self.pos(section, branch, offset),
            '_grid': (section, branch, offset),
        }
        if credentials:
            node['credentials'] = credentials
        if notes:
            node['notes'] = notes
            node['notesInFlow'] = False
        if extra:
            node.update(extra)
        self.nodes.append(node)
        self.by_name[name] = node
        return name

    # ── atajos por tipo ───────────────────────────────────────────────
    def code(self, name, js, section, branch=0, offset=0, mode=None, **kw):
        p = {'jsCode': js}
        if mode:
            p['mode'] = mode
        return self.add(name, 'n8n-nodes-base.code', p, section, branch, offset,
                        type_version=2, **kw)

    def http(self, name, method, url, section, branch=0, offset=0, headers=None,
             body=None, body_json=None, query=None, timeout=15000, never_error=True,
             full_response=False, credentials=None, retry=None, on_error=None,
             content_type=None, body_params=None, **kw):
        p = {'method': method, 'url': url, 'options': {'timeout': timeout}}
        if never_error or full_response:
            p['options']['response'] = {'response': {
                'responseFormat': 'json', 'neverError': bool(never_error),
                'fullResponse': bool(full_response)}}
        if headers:
            p['sendHeaders'] = True
            p['headerParameters'] = {'parameters': [{'name': k, 'value': v}
                                                    for k, v in headers.items()]}
        if query:
            p['sendQuery'] = True
            p['queryParameters'] = {'parameters': [{'name': k, 'value': v}
                                                   for k, v in query.items()]}
        if body_json is not None:
            p['sendBody'] = True
            p['specifyBody'] = 'json'
            p['jsonBody'] = body_json
        elif body is not None:
            p['sendBody'] = True
            p['specifyBody'] = 'string'
            p['body'] = body
        elif body_params is not None:
            p['sendBody'] = True
            if content_type:
                p['contentType'] = content_type
            p['bodyParameters'] = {'parameters': body_params}
        if credentials:
            p['authentication'] = 'genericCredentialType'
            # El tipo de autenticación debe coincidir con la credential
            # realmente asociada al HTTP Request.
            p['genericAuthType'] = next(iter(credentials.keys()))
        extra = dict(kw.pop('extra', {}) or {})
        if retry:
            extra['retryOnFail'] = True
            extra['maxTries'] = retry
            extra['waitBetweenTries'] = 2000
        if on_error:
            extra['onError'] = on_error
        return self.add(name, 'n8n-nodes-base.httpRequest', p, section, branch, offset,
                        type_version=4.2, credentials=credentials, extra=extra or None, **kw)

    def mysql(self, name, query, section, branch=0, offset=0, replacements=None,
              always_output=False, on_error=None, retry=None, **kw):
        """SQL SIEMPRE parametrizado: `?` en la query y las expresiones en
        options.queryReplacement. Nunca interpolación de strings en el SQL."""
        p = {'operation': 'executeQuery', 'query': query, 'options': {}}
        if replacements:
            p['options']['queryReplacement'] = replacements
        extra = dict(kw.pop('extra', {}) or {})
        if always_output:
            extra['alwaysOutputData'] = True
        if on_error:
            extra['onError'] = on_error
        if retry:
            extra['retryOnFail'] = True
            extra['maxTries'] = retry
        return self.add(name, 'n8n-nodes-base.mySql', p, section, branch, offset,
                        type_version=2.4,
                        credentials={'mySql': {'id': '__MYSQL_CREDENTIAL__',
                                               'name': 'Landmark MySQL'}},
                        extra=extra or None, **kw)

    def if_(self, name, left, operator, right, section, branch=0, offset=0,
            op_type='boolean', single=False, **kw):
        cond = {'id': nid(), 'leftValue': left, 'rightValue': right,
                'operator': {'type': op_type, 'operation': operator}}
        if single:
            cond['operator']['singleValue'] = True
        p = {'conditions': {'options': {'caseSensitive': True, 'leftValue': '',
                                        'typeValidation': 'loose', 'version': 2},
                            'conditions': [cond], 'combinator': 'and'},
             'options': {}}
        return self.add(name, 'n8n-nodes-base.if', p, section, branch, offset,
                        type_version=2.2, **kw)

    def switch(self, name, value_expr, outputs, section, branch=0, offset=0,
               fallback='extra', **kw):
        """outputs: lista de (valor, etiqueta_de_salida)."""
        rules = []
        for val, label in outputs:
            rules.append({
                'conditions': {'options': {'caseSensitive': True, 'leftValue': '',
                                           'typeValidation': 'loose', 'version': 2},
                               'conditions': [{'id': nid(), 'leftValue': value_expr,
                                               'rightValue': val,
                                               'operator': {'type': 'string',
                                                            'operation': 'equals'}}],
                               'combinator': 'and'},
                'renameOutput': True, 'outputKey': label})
        p = {'rules': {'values': rules}, 'options': {'fallbackOutput': fallback}}
        return self.add(name, 'n8n-nodes-base.switch', p, section, branch, offset,
                        type_version=3.2, **kw)

    def schedule(self, name, minutes, section, branch=0, offset=0, **kw):
        p = {'rule': {'interval': [{'field': 'minutes', 'minutesInterval': minutes}]}}
        return self.add(name, 'n8n-nodes-base.scheduleTrigger', p, section, branch, offset,
                        type_version=1.3, **kw)

    def webhook(self, name, path, section, branch=0, offset=0, raw_body=False,
                respond='responseNode', **kw):
        p = {'httpMethod': 'POST', 'path': path, 'responseMode': respond, 'options': {}}
        if raw_body:
            p['options']['rawBody'] = True
        return self.add(name, 'n8n-nodes-base.webhook', p, section, branch, offset,
                        type_version=2, extra={'webhookId': path}, **kw)

    def respond(self, name, section, branch=0, offset=0, body='={ "ok": true }',
                kind='json', **kw):
        p = {'respondWith': kind, 'responseBody': body, 'options': {}}
        return self.add(name, 'n8n-nodes-base.respondToWebhook', p, section, branch, offset,
                        type_version=1.1, **kw)

    def exec_wf(self, name, workflow_name, section, branch=0, offset=0, wait=True, **kw):
        p = {'workflowId': {'__rl': True, 'value': f'__WORKFLOW_ID_{workflow_name}__',
                            'mode': 'list', 'cachedResultName': workflow_name},
             'workflowInputs': {'mappingMode': 'defineBelow', 'value': {},
                                'matchingColumns': [], 'schema': [],
                                'attemptToConvertTypes': False, 'convertFieldsToString': True},
             'options': {'waitForSubWorkflow': wait}}
        return self.add(name, 'n8n-nodes-base.executeWorkflow', p, section, branch, offset,
                        type_version=1.2, **kw)

    def exec_trigger(self, name, fields, section, branch=0, offset=0, **kw):
        p = {'workflowInputs': {'values': [{'name': f} for f in fields]}}
        return self.add(name, 'n8n-nodes-base.executeWorkflowTrigger', p, section,
                        branch, offset, type_version=1.1, **kw)

    def telegram(self, name, chat_expr, text_expr, section, branch=0, offset=0,
                 audio=False, **kw):
        if audio:
            p = {'operation': 'sendAudio', 'chatId': chat_expr, 'binaryData': True,
                 'binaryPropertyName': 'data',
                 'additionalFields': {'caption': text_expr}}
        else:
            p = {'chatId': chat_expr, 'text': text_expr,
                 'additionalFields': {'appendAttribution': False, 'parse_mode': 'HTML'}}
        extra = dict(kw.pop('extra', {}) or {})
        extra.setdefault('webhookId', nid())
        return self.add(name, 'n8n-nodes-base.telegram', p, section, branch, offset,
                        type_version=1.2,
                        credentials={'telegramApi': {'id': '__TELEGRAM_CREDENTIAL__',
                                                     'name': 'Landmark Telegram'}},
                        extra=extra, **kw)

    def noop(self, name, section, branch=0, offset=0, **kw):
        return self.add(name, 'n8n-nodes-base.noOp', {}, section, branch, offset,
                        type_version=1, **kw)

    def sticky(self, section, title, body, color=4, height=320, width=420, branch=-1):
        content = f'## {title}\n\n{body}'
        node = {
            'parameters': {'content': content, 'height': height, 'width': width,
                           'color': color},
            'id': nid(), 'name': f'NOTE {section:02d} {title}'[:60],
            'type': STICKY, 'typeVersion': 1,
            'position': [X0 + section * DX - 20, Y0 + branch * DY - 40],
            '_sticky': (section, branch),
        }
        self.nodes.append(node)
        self.sections.append(section)
        return node['name']

    # ── conexiones ────────────────────────────────────────────────────
    def link(self, src, dst, out=0, in_=0):
        for n in (src, dst):
            if n not in self.by_name:
                raise ValueError(f'conexión a nodo inexistente: {n}')
        main = self.conns.setdefault(src, {}).setdefault('main', [])
        while len(main) <= out:
            main.append([])
        main[out].append({'node': dst, 'type': 'main', 'index': in_})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    # ── validación (checklist del estándar) ───────────────────────────
    def validate(self):
        issues = []
        real = [n for n in self.nodes if n['type'] != STICKY]
        targets = set()
        for src, c in self.conns.items():
            for out in c.get('main', []):
                for t in out:
                    targets.add(t['node'])

        for n in real:
            name = n['name']
            if n['type'] not in TRIGGERS and name not in targets:
                issues.append(f'nodo huérfano (sin entrada): {name}')
            if n.get('disabled'):
                issues.append(f'nodo desactivado: {name}')
            if EMOJI_RE.search(name):
                issues.append(f'emoji en el nombre: {name}')
            if not NAME_RE.match(name):
                issues.append(f'nombre fuera de "[ÁMBITO] Acción": {name}')
            if NUM_SUFFIX_RE.search(name):
                issues.append(f'sufijo numérico en el nombre: {name}')
            # toda salida de IF/Switch conectada
            if n['type'] == 'n8n-nodes-base.if':
                outs = self.conns.get(name, {}).get('main', [])
                if len(outs) < 2 or not all(outs[:2]):
                    issues.append(f'IF con salidas sin conectar: {name}')
            if n['type'] == 'n8n-nodes-base.switch':
                want = len(n['parameters']['rules']['values']) + \
                    (1 if n['parameters']['options'].get('fallbackOutput') == 'extra' else 0)
                outs = self.conns.get(name, {}).get('main', [])
                if len(outs) < want or not all(outs[:want]):
                    issues.append(f'Switch con salidas sin conectar: {name} '
                                  f'({len(outs)}/{want})')

        # layout izquierda a derecha: ningún nodo a la izquierda de su origen
        self.layout()
        pos = {n['name']: n['position'][0] for n in self.nodes}
        for src, c in self.conns.items():
            for out in c.get('main', []):
                for t in out:
                    if src in pos and t['node'] in pos and pos[t['node']] < pos[src]:
                        issues.append(f'conexión hacia la izquierda: {src} -> {t["node"]}')

        if not self.sections:
            issues.append('sin sticky notes')
        blob = json.dumps(self.to_dict(), ensure_ascii=False)
        for m in SECRET_RE.finditer(blob):
            issues.append(f'posible secreto en el JSON: {m.group(0)[:40]}')
        return issues

    # ── salida ────────────────────────────────────────────────────────
    def to_dict(self):
        self.layout()
        nodes = [{k: v for k, v in n.items() if not k.startswith('_')}
                 for n in self.nodes]
        return {
            'name': self.name,
            'nodes': nodes,
            'connections': self.conns,
            'active': False,
            'settings': {'executionOrder': 'v1', 'timezone': 'UTC',
                         'saveManualExecutions': True,
                         'callerPolicy': 'workflowsFromSameOwner'},
            'pinData': {},
            'versionId': nid(),
            'meta': {'templateVersion': self.version},
            'tags': [{'name': 'landmark'}, {'name': 'callcenter-v2'}],
        }

    def write(self, path):
        problems = self.validate()
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return problems
