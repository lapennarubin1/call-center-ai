#!/usr/bin/env python3
"""CRM_ENGLISH_RULE: todo texto humano que llega a LeadStudio va en inglés.

La regla es obligatoria en todo el suite. Este test la hace verificable en vez
de confiar en la disciplina de quien edite un nodo:

  1. el catálogo de frases (Python) está íntegramente en inglés
  2. el catálogo JS es IDÉNTICO al de Python — nadie puede editar uno solo
  3. las notas generadas para cada caso real están en inglés
  4. ningún nodo de los siete workflows manda un literal en español al CRM
  5. los IDs, nombres, teléfonos y URLs NO se traducen
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

import crm_notes as cn

# Campos del cuerpo de LeadStudio que llevan texto legible por una persona.
HUMAN_FIELDS = ['notes', 'note', 'description', 'summary', 'message', 'comment']

# Endpoints de LeadStudio a los que se escribe texto humano.
CRM_WRITE_RE = re.compile(r'/api/leads/[^\'"\s]*/(followups|notes)|/api/leads/\{\{')

SPANISH_IN_CRM = re.compile(
    r'\b(contest[oó]|cuenta|llamada|intento|pr[oó]xima|correctamente|usuario|'
    r'contrase[nñ]a|grabaci[oó]n|pago|enlace|fall[oó]|buz[oó]n|ocupado|'
    r'desconocido|pendiente|rechazado|creada|enviado|cerrado|reintento)\b', re.I)


def main():
    s = S.Suite('CRM ENGLISH RULE')
    js = S.load_js()
    wfs = S.load_workflows()

    s.section('catálogo de frases')

    def all_english():
        bad = cn.all_phrases_english()
        assert not bad, f'frases que no pasan el detector de inglés: {bad}'
    s.check(f'las {len(cn.PHRASES)} frases del catálogo están en inglés', all_english)

    def detector_works():
        # El detector tiene que atrapar exactamente lo que v1 escribía en el CRM.
        for bad in ['No contestó.', 'Cuenta creada correctamente.',
                    'Buzón de voz', '🟡 No answer (SIP 603) — intento 3',
                    'Llamada fallida: sin razón', 'Contestó, conversación completa']:
            assert not cn.is_english(bad), f'el detector dejó pasar: {bad!r}'
        for good in ['Call was not answered.', 'Trading account created successfully.',
                     'Customer requested a callback.', 'Payment link sent successfully.']:
            assert cn.is_english(good), f'el detector rechazó texto válido: {good!r}'
    s.check('el detector atrapa las notas en español que escribía v1', detector_works)

    s.section('paridad del catálogo Python <-> JS')

    def catalogs_match():
        src = js['lmnotes.js']
        m = re.search(r'const LM_PHRASES = \{(.*?)\n\};', src, re.S)
        assert m, 'no se encontró LM_PHRASES en lmnotes.js'
        found = dict(re.findall(r"(\w+):\s*'((?:[^'\\]|\\.)*)'", m.group(1)))
        found = {k: v.replace("\\'", "'") for k, v in found.items()}
        missing = set(cn.PHRASES) - set(found)
        extra = set(found) - set(cn.PHRASES)
        assert not missing, f'frases que faltan en JS: {sorted(missing)}'
        assert not extra, f'frases de más en JS: {sorted(extra)}'
        diff = {k: (cn.PHRASES[k], found[k]) for k in cn.PHRASES if cn.PHRASES[k] != found[k]}
        assert not diff, f'frases que difieren entre Python y JS: {diff}'
    s.check('los dos catálogos tienen las mismas claves y el mismo texto', catalogs_match)

    def generators_match():
        """Las funciones que arman las notas dan el mismo string en ambos lados."""
        cases = [
            ('call', {'result': 'NO_ANSWER', 'attempt': 3, 'route_key': 'IN_PROVEEDOR1',
                      'provider': 'proveedor1', 'sip_code': '603', 'duration_seconds': 0,
                      'action': 'RETRY', 'schedule_next_at': '2026-09-22T04:30:00Z'}),
            ('call', {'result': 'ANSWERED', 'attempt': 1, 'route_key': 'IN_STRINGEE',
                      'provider': 'stringee', 'duration_seconds': 143, 'action': 'COMPLETE'}),
            ('call', {'result': 'CALLBACK', 'attempt': 2, 'route_key': 'NP_PROVEEDOR1',
                      'provider': 'proveedor1', 'action': 'CALLBACK',
                      'schedule_next_at': '2026-09-19T10:00:00Z'}),
            ('call', {'result': 'DNC', 'attempt': 4, 'route_key': 'MX_PROVEEDOR1',
                      'action': 'CLOSE'}),
            ('account', {'status': 'CREATED', 'market': 'IND', 'username': 'LM1234',
                         'portal_url': 'https://crm.landmarkmarkets.in/'}),
            ('account', {'status': 'ALREADY_EXISTS', 'market': 'NPL'}),
            ('account', {'status': 'FAILED', 'market': 'IND', 'error': 'EMAIL_TAKEN'}),
            ('payment', {'status': 'LINK_CREATED', 'amount': '5000', 'currency': 'INR',
                         'provider': 'okpay', 'order_ref': 'LM_abc_1'}),
            ('payment', {'status': 'CONFIRMED', 'amount': '5000', 'currency': 'INR',
                         'provider': 'okpay', 'order_ref': 'LM_abc_1',
                         'transaction_id': 'T99'}),
            ('payment', {'status': 'LINK_FAILED', 'amount': '100', 'currency': 'NPR',
                         'provider': 'monetix', 'error': 'gateway down'}),
            ('recording', {'status': 'ATTACHED', 'duration_seconds': 180,
                           'route_key': 'IN_STRINGEE'}),
            ('recording', {'status': 'SKIPPED', 'duration_seconds': 12, 'min_secs': 60,
                           'route_key': 'IN_PROVEEDOR1'}),
        ]
        py = []
        for kind, kw in cases:
            fn = {'call': cn.call_note, 'account': cn.account_note,
                  'payment': cn.payment_note, 'recording': cn.recording_note}[kind]
            py.append(fn(**kw))
        py.append(cn.error_note('COUNTRY_DISABLED', 'IN'))
        py.append(cn.error_note('VALIDATION_ERROR', 'missing phone'))

        got = S.run_node(js['lmcore.js'] + js['lmnotes.js'], r'''
const out = [];
for (const [kind, o] of __payload.cases) {
  if (kind === 'call') out.push(lmCallNote(o));
  else if (kind === 'account') out.push(lmAccountNote(o));
  else if (kind === 'payment') out.push(lmPaymentNote(o));
  else out.push(lmRecordingNote(o));
}
out.push(lmErrorNote('COUNTRY_DISABLED', 'IN'));
out.push(lmErrorNote('VALIDATION_ERROR', 'missing phone'));
console.log(JSON.stringify(out));
''', {'cases': cases})

        diffs = [(a, b) for a, b in zip(py, got) if a != b]
        assert not diffs, f'{len(diffs)} notas difieren. Primera: py={diffs[0][0]!r} js={diffs[0][1]!r}'
        for n in py:
            assert cn.is_english(n), f'nota generada no está en inglés: {n!r}'
    s.check('las notas generadas coinciden y están en inglés (14 casos)', generators_match)

    s.section('no se traduce lo que no se debe traducir')

    def keeps_identifiers():
        note = cn.call_note('NO_ANSWER', attempt=3, route_key='IN_PROVEEDOR1',
                            provider='proveedor1', sip_code='603',
                            schedule_next_at='2026-09-22T04:30:00Z')
        for token in ('IN_PROVEEDOR1', 'proveedor1', '603', '2026-09-22T04:30:00Z'):
            assert token in note, f'se perdió {token} en la nota'
        acc = cn.account_note('CREATED', market='IND', username='LM1234',
                              portal_url='https://crm.landmarkmarkets.in/')
        for token in ('IND', 'LM1234', 'https://crm.landmarkmarkets.in/'):
            assert token in acc, f'se perdió {token} en la nota de cuenta'
    s.check('route_key, IDs de proveedor, códigos SIP y URLs viajan intactos',
            keeps_identifiers)

    # ══ el resumen NO pasa en crudo ══════════════════════════════════
    s.section('ningún resumen sin garantía de inglés entra al CRM')

    # Una versión anterior adjuntaba el resumen de ElevenLabs tal cual,
    # con el argumento de que era "contenido del cliente". Eso metía
    # hindi, nepalí y español dentro de notas del CRM. No hay excepción.
    RESUMENES = {
        'español': 'El cliente quiere que lo llamen mañana.',
        'hindi':   'ग्राहक ने कल कॉल करने के लिए कहा।',
        'nepalí':  'ग्राहकले भोलि फोन गर्न भन्नुभयो।',
        'árabe':   'العميل يريد معاودة الاتصال غدا',
    }

    def raw_summary_never_reaches_crm():
        malos = []
        for idioma, texto in RESUMENES.items():
            note = cn.call_note('ANSWERED', attempt=1, action='COMPLETE',
                                summary=texto)
            if texto in note:
                malos.append(f'{idioma}: el texto crudo aparece en la nota')
            if 'Agent summary' in note:
                malos.append(f'{idioma}: se adjuntó un resumen no garantizado')
            if not cn.is_english(note):
                malos.append(f'{idioma}: la nota dejó de ser inglés')
        assert not malos, '; '.join(malos)
    s.check('resúmenes en español, hindi, nepalí y árabe NO llegan al CRM',
            raw_summary_never_reaches_crm)

    def note_is_still_valid_english():
        for texto in RESUMENES.values():
            note = cn.call_note('ANSWERED', attempt=1, action='COMPLETE',
                                summary=texto)
            assert note.startswith('Call answered.'), \
                f'la nota perdió su texto canónico: {note!r}'
            assert cn.is_english(note)
    s.check('la nota sigue siendo inglés válido y conserva su texto canónico',
            note_is_still_valid_english)

    def declared_english_summary_is_used():
        texto = 'Customer asked for a callback tomorrow morning.'
        note = cn.call_note('ANSWERED', summary=texto, summary_language='en')
        assert texto in note and 'Agent summary:' in note, \
            'un resumen declarado en inglés debería poder usarse'
        note2 = cn.call_note('ANSWERED', summary=texto, summary_is_english=True)
        assert texto in note2
    s.check('un resumen declarado en inglés SÍ se usa',
            declared_english_summary_is_used)

    def undeclared_summary_is_dropped_even_if_it_looks_english():
        """Sin idioma declarado se descarta aunque parezca inglés: la
        heurística existe para cazar regresiones, no para autorizar
        contenido de idioma desconocido."""
        texto = 'Customer asked for a callback tomorrow.'
        note = cn.call_note('ANSWERED', summary=texto)
        assert texto not in note, \
            'se adjuntó un resumen sin idioma declarado'
    s.check('sin idioma declarado, el resumen se descarta aunque parezca inglés',
            undeclared_summary_is_dropped_even_if_it_looks_english)

    def declared_english_but_actually_not_is_dropped():
        """Si la fuente miente sobre el idioma, la comprobación lo caza."""
        note = cn.call_note('ANSWERED', summary='El cliente quiere que lo llamen.',
                            summary_language='en')
        assert 'cliente' not in note, \
            'un resumen mal declarado como inglés entró al CRM'
    s.check('un resumen mal declarado como inglés también se descarta',
            declared_english_but_actually_not_is_dropped)

    # ══ errores crudos ═══════════════════════════════════════════════
    s.section('ningún error crudo del proveedor entra al CRM')

    def raw_provider_error_never_reaches_crm():
        crudos = ['Pago rechazado por banco', 'Cuenta duplicada en el sistema',
                  'लेनदेन विफल', 'Transaction declined by issuing bank']
        malos = []
        for crudo in crudos:
            for nota in (cn.payment_note('LINK_FAILED', error=crudo),
                         cn.account_note('FAILED', error=crudo),
                         cn.error_note('PROVIDER_ERROR', detail=crudo),
                         cn.reconciliation_note('CALL_STUCK', detail=crudo)):
                if crudo in nota:
                    malos.append(f'{crudo[:24]!r} aparece en {nota[:48]!r}')
                if not cn.is_english(nota):
                    malos.append(f'nota no inglesa: {nota[:48]!r}')
        assert not malos, '; '.join(malos[:4])
    s.check('errores crudos del proveedor NO llegan al CRM, en ningún idioma',
            raw_provider_error_never_reaches_crm)

    def error_becomes_a_code():
        nota = cn.payment_note('LINK_FAILED', error='Pago rechazado por banco')
        assert '[code PROVIDER_ERROR]' in nota, \
            f'el error no se convirtió en código: {nota!r}'
        nota2 = cn.payment_note('LINK_FAILED', error='AUTH_ERROR')
        assert 'code AUTH_ERROR' in nota2, 'se perdió un código del contrato'
    s.check('el error se convierte en un código publicable', error_becomes_a_code)

    def technical_identifiers_still_pass():
        """§2: códigos, IDs y enums siguen siendo exentos."""
        for token in ('HTTP_503', 'PROVIDER_ERROR', '4001', 'order:LM123'):
            assert cn.safe_detail(token) == token, \
                f'se descartó un identificador técnico legítimo: {token}'
        for prosa in ('Pago rechazado por banco', 'Payment declined by bank',
                      'timeout waiting for provider'):
            assert cn.safe_detail(prosa) is None, \
                f'pasó prosa como identificador técnico: {prosa!r}'
    s.check('los identificadores técnicos pasan; la prosa no',
            technical_identifiers_still_pass)

    s.section('los workflows no mandan español al CRM')

    def no_spanish_to_crm():
        offenders = []
        for fname, wf in wfs.items():
            for node in wf['nodes']:
                p = node.get('parameters', {})
                blob = json.dumps(p, ensure_ascii=False)
                if node['type'] == 'n8n-nodes-base.httpRequest':
                    url = str(p.get('url', ''))
                    if not CRM_WRITE_RE.search(url):
                        continue
                    body = str(p.get('jsonBody', ''))
                    # el cuerpo se arma con lmnotes o con campos ya validados
                    for field in HUMAN_FIELDS:
                        for m in re.finditer(rf'{field}\s*:\s*[\'"]([^\'"]{{4,}})[\'"]', body):
                            if SPANISH_IN_CRM.search(m.group(1)):
                                offenders.append(f'{fname}::{node["name"]}::{m.group(1)[:50]}')
                if node['type'] == 'n8n-nodes-base.code':
                    code = p.get('jsCode', '')
                    # literales asignados a notes/crm_note dentro de un Code node
                    for m in re.finditer(r'(crm_note|crm_notes|notes)\s*[:=]\s*[\'"]([^\'"]{6,})[\'"]',
                                         code):
                        if SPANISH_IN_CRM.search(m.group(2)):
                            offenders.append(f'{fname}::{node["name"]}::{m.group(2)[:50]}')
        assert not offenders, 'literales en español hacia el CRM:\n  ' + '\n  '.join(offenders)
    s.check('ningún nodo escribe un literal en español en un campo del CRM',
            no_spanish_to_crm)

    def crm_writes_use_catalog():
        """Todo POST de nota al CRM tiene que tomar el texto del catálogo."""
        writers, ok = [], []
        for fname, wf in wfs.items():
            for node in wf['nodes']:
                if node['type'] != 'n8n-nodes-base.httpRequest':
                    continue
                p = node.get('parameters', {})
                url = str(p.get('url', ''))
                body = str(p.get('jsonBody', ''))
                if '/followups' not in url:
                    continue
                if 'notes' not in body:
                    continue
                writers.append(f'{fname}::{node["name"]}')
                # el texto tiene que venir de un campo calculado (crm_note/crm_notes)
                if re.search(r'\$json\.(crm_note|crm_notes)', body):
                    ok.append(f'{fname}::{node["name"]}')
        assert writers, 'no se encontró ningún POST de nota al CRM'
        missing = sorted(set(writers) - set(ok))
        assert not missing, ('estos nodos escriben notes sin usar el catálogo: ' +
                             ', '.join(missing))
    s.check('todo POST de nota toma el texto de lmnotes.js, no de un literal',
            crm_writes_use_catalog)

    def user_messages_english():
        """Los mensajes que el agente le dice al cliente también en inglés
        (v1 respondía en hinglish desde un Code node)."""
        offenders = []
        for fname, wf in wfs.items():
            for node in S.code_nodes(wf):
                code = node['parameters'].get('jsCode', '')
                for m in re.finditer(r'message_to_user\s*:\s*[\'"]([^\'"]{6,})[\'"]', code):
                    txt = m.group(1)
                    if not cn.is_english(txt) or re.search(r'\b(karne|aaya|hai|mein|kar)\b', txt):
                        offenders.append(f'{fname}::{node["name"]}::{txt[:60]}')
        assert not offenders, 'mensajes al cliente fuera de inglés:\n  ' + '\n  '.join(offenders)
    s.check('los mensajes al cliente están en inglés (v1 usaba hinglish)',
            user_messages_english)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
