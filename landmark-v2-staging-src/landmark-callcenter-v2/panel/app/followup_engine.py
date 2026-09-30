"""
followup_engine.py — Implementación de REFERENCIA del Follow-up Engine V2.1
==========================================================================

El motor real será un sub-workflow de n8n (TEMPLATE_FOLLOWUP_ENGINE_V2). Este
módulo existe para:

  1. fijar la semántica exacta de forma ejecutable y testeable;
  2. que el panel pueda validar políticas y simular "qué pasaría con el
     intento N si el resultado es X";
  3. servir de oráculo en los tests del sub-workflow cuando se construya.

Principio: el motor NO conoce proveedores. Recibe un resultado normalizado
(FOLLOWUP_ENGINE_CONTRACT_V2_1.json) y una política. Nada de "if stringee",
nada de "if India", nada de módulo %3.

Resolución de una regla, en este orden:
  1. result efectivo = result_aliases.get(result, result)
  2. regla con (result efectivo, attempt exacto)
  3. regla con (result efectivo, attempt "*")
  4. unmatched_action de la política (por defecto "NONE")
"""

import json
import re
from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

RESULTS = ['DISPATCHED', 'ANSWERED', 'NO_ANSWER', 'BUSY', 'FAILED', 'VOICEMAIL',
           'CALLBACK', 'WRONG_NUMBER', 'DNC', 'UNKNOWN']
FINAL_RESULTS = [r for r in RESULTS if r != 'DISPATCHED']
ACTIONS = ['RETRY', 'CLOSE', 'COMPLETE', 'CALLBACK', 'NONE']

_DELAY_RE = re.compile(r'^\+(\d{1,4})(m|h|d|bd)$')

# Pista para el paso CRM del motor. El enum real de LeadStudio es
# PENDING_VERIFICATION (PV-1): los valores conocidos salen de WF14 v1.
CRM_STATUS_HINT = {
    'RETRY': 'NO_ANSWER',
    'CLOSE': 'CLOSED',
    'COMPLETE': 'CONTACTED',
    'CALLBACK': None,     # v1 WF9 no parcheaba status en callback: PENDING_VERIFICATION
    'NONE': None,
}


class PolicyError(ValueError):
    pass


def validate_policy(policy):
    """Valida la forma de una política. Devuelve el dict normalizado."""
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except (ValueError, TypeError) as ex:
            raise PolicyError(f'policy no es JSON válido: {ex}')
    if not isinstance(policy, dict):
        raise PolicyError('policy debe ser un objeto JSON')

    codes = policy.get('no_answer_sip_codes', [])
    if not isinstance(codes, list) or any(not re.match(r'^\d{3}$', str(c)) for c in codes):
        raise PolicyError('no_answer_sip_codes debe ser una lista de códigos SIP de 3 dígitos')

    aliases = policy.get('result_aliases', {}) or {}
    if not isinstance(aliases, dict):
        raise PolicyError('result_aliases debe ser un objeto')
    for k, v in aliases.items():
        if k not in FINAL_RESULTS or v not in FINAL_RESULTS:
            raise PolicyError(f'alias inválido {k}→{v}: ambos deben ser resultados finales')
        if k == v:
            raise PolicyError(f'alias circular {k}→{v}')

    rules = policy.get('rules')
    if not isinstance(rules, list) or not rules:
        raise PolicyError('rules debe ser una lista no vacía')
    seen = set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise PolicyError(f'rules[{i}] debe ser un objeto')
        res, att, act = r.get('result'), r.get('attempt'), r.get('action')
        if res not in FINAL_RESULTS:
            raise PolicyError(f'rules[{i}].result inválido: {res!r}')
        if att != '*' and not (isinstance(att, int) and not isinstance(att, bool) and att >= 1):
            raise PolicyError(f'rules[{i}].attempt debe ser entero >= 1 o "*"')
        if act not in ACTIONS:
            raise PolicyError(f'rules[{i}].action inválido: {act!r}')
        delay = r.get('delay')
        if act == 'RETRY':
            if not delay or not _DELAY_RE.match(str(delay)):
                raise PolicyError(f'rules[{i}]: RETRY exige delay +Nm/+Nh/+Nd/+Nbd')
        elif delay not in (None, ''):
            raise PolicyError(f'rules[{i}]: delay solo aplica a RETRY')
        key = (res, att)
        if key in seen:
            raise PolicyError(f'regla duplicada para result={res} attempt={att}')
        seen.add(key)

    cb = policy.get('callback_default')
    if cb is not None and not _DELAY_RE.match(str(cb)):
        raise PolicyError('callback_default debe ser +Nm/+Nh/+Nd/+Nbd')

    ua = policy.get('unmatched_action', 'NONE')
    if ua not in ('NONE', 'CLOSE'):
        raise PolicyError('unmatched_action solo admite NONE o CLOSE')
    return policy


def apply_delay(delay, now_utc, tz_name):
    """Aplica un delay de política.

    +Nm / +Nh : duración absoluta.
    +Nd       : N días de calendario, misma hora de reloj LOCAL (respeta DST).
    +Nbd      : N días hábiles (lun-vie) en el huso del país, misma hora local.
                Feriados: NO contemplados (PENDING_VERIFICATION si se requieren).
    """
    m = _DELAY_RE.match(str(delay or ''))
    if not m:
        raise PolicyError(f'delay inválido: {delay!r}')
    n, unit = int(m.group(1)), m.group(2)
    if unit == 'm':
        return now_utc + timedelta(minutes=n)
    if unit == 'h':
        return now_utc + timedelta(hours=n)

    tz = ZoneInfo(tz_name)
    local = now_utc.astimezone(tz)
    if unit == 'd':
        target = local.replace(tzinfo=None) + timedelta(days=n)
    else:  # bd
        target = local.replace(tzinfo=None)
        added = 0
        while added < n:
            target += timedelta(days=1)
            if target.weekday() < 5:
                added += 1
    return target.replace(tzinfo=tz).astimezone(_tz.utc)


def _find_rule(policy, result, attempt):
    exact = wildcard = None
    for r in policy['rules']:
        if r['result'] != result:
            continue
        if r['attempt'] == attempt:
            exact = r
        elif r['attempt'] == '*':
            wildcard = r
    return exact or wildcard


def resolve(policy, attempt, result, now_utc=None, tz_name='UTC', callback_at=None,
            sip_code=None):
    """Decide la acción de follow-up.

    Devuelve un dict — el bloque 'decision' del FOLLOWUP_ENGINE_CONTRACT:
      action, schedule_next_at (ISO Z o None), rule_matched, effective_result,
      crm_status_hint.

    sip_code: el adapter SIP lo reporta crudo con result=FAILED. Qué códigos
    significan "no contestó" es POLÍTICA (no_answer_sip_codes), no del adapter:
    por eso la reclasificación ocurre acá y no en el proveedor.
    """
    policy = validate_policy(policy)
    if sip_code is not None and result in ('FAILED', 'UNKNOWN'):
        result = classify_sip_code(policy, sip_code)
    if result == 'DISPATCHED':
        raise PolicyError('DISPATCHED no es un resultado final: el motor no se llama con él')
    if result not in FINAL_RESULTS:
        raise PolicyError(f'resultado desconocido: {result!r}')
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise PolicyError('attempt debe ser entero >= 1')

    now_utc = now_utc or datetime.now(_tz.utc)
    effective = (policy.get('result_aliases') or {}).get(result, result)
    rule = _find_rule(policy, effective, attempt)
    action = rule['action'] if rule else policy.get('unmatched_action', 'NONE')

    schedule = None
    if action == 'RETRY':
        schedule = apply_delay(rule['delay'], now_utc, tz_name)
    elif action == 'CALLBACK':
        cb = _parse_ts(callback_at)
        if cb and cb > now_utc:
            schedule = cb
        else:
            schedule = apply_delay(policy.get('callback_default') or '+24h', now_utc, tz_name)

    return {
        'result': result,
        'sip_code': str(sip_code) if sip_code is not None else None,
        'effective_result': effective,
        'attempt': attempt,
        'action': action,
        'delay': rule.get('delay') if rule else None,
        'schedule_next_at': schedule.strftime('%Y-%m-%dT%H:%M:%SZ') if schedule else None,
        'rule_matched': ({'result': rule['result'], 'attempt': rule['attempt']} if rule else None),
        'crm_status_hint': CRM_STATUS_HINT.get(action),
    }


def classify_sip_code(policy, sip_code):
    """Traduce un código SIP a resultado normalizado usando la POLÍTICA.

    Los adapters no deciden qué SIP es "no contestó": lo dice la política de la
    ruta. Un código no listado es FAILED (no UNKNOWN: sabemos que falló).
    """
    policy = validate_policy(policy)
    if sip_code is None:
        return 'UNKNOWN'
    if str(sip_code) in [str(c) for c in policy.get('no_answer_sip_codes', [])]:
        return 'NO_ANSWER'
    return 'FAILED'


def _parse_ts(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=_tz.utc)
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
    except ValueError:
        return None
