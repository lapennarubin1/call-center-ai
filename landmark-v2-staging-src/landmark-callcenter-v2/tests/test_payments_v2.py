#!/usr/bin/env python3
"""Pagos: dos modos, ninguno inventado (§25).

La prueba más importante de este fichero es la de PARIDAD: la firma que
calcula el port de Python tiene que salir idéntica a la del WF7 que corre
hoy en producción. Si se desvía, OkPay rechaza el cobro y el cliente se
queda esperando un link que nunca llega.
"""
import json
import os
import random
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, mysql_available, fresh_mysql_db,     # noqa: E402
                    mysql_conn, run_sql_file, DB)

import payments as P                                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE_JS = os.path.join(HERE, 'fixtures', 'okpay_reference.js')
PARTS = ('001_multi_country_config_v2_2.sql', '002_callcenter_suite_v2.sql',
         '003_legacy_compat_tables.sql', '004_billing_v2.sql',
         '005_legacy_backup_v2.sql', '006_payments_v2.sql')

OK_DIRECT = {
    'enabled': 1, 'mode': 'DIRECT_PROVIDER', 'adapter_key': 'OKPAY_V1',
    'endpoint': 'https://api.wpay.one/v1/Collect', 'currency': 'INR',
    'credential_ref': 'OKPAY_API', 'callback_url': 'https://n8n.example/webhook/okpay-v2',
    'return_url': 'https://site.example/sign-in', 'router_key': None,
    'config_json': '{"min_amount":2000,"max_amount":500000}',
}
OK_ROUTER = {
    'enabled': 1, 'mode': 'UNIVERSAL_ROUTER', 'router_key': 'GENERIC_JSON_V1',
    'endpoint': 'https://router.example/payments', 'credential_ref': 'ROUTER_API',
    'currency': 'INR', 'adapter_key': None, 'config_json': None,
}


def build_cases(n=300):
    random.seed(20260921)
    monedas = ['INR', 'NPR', 'USD', 'MXN', '']
    tel = ['+919999999999', '919812345678', '', '+9779800000000', '+5215512345678']
    imp = ['5000', '2000.50', '100', '999999', '0.01', '']
    pays = ['UPI', 'UPI_QR', 'CARD', None]
    leads = ['L-1', 'lead_abc', '', 'ID=99', 'Ñ-áé', 'José', 'a b c']
    out = []
    for _ in range(n):
        out.append({
            'ctx': {
                'currency': random.choice(monedas),
                'out_trade_no': 'LM' + ''.join(random.choice('ABCDEF0123456789')
                                               for _ in range(12)),
                'amount': random.choice(imp),
                'callback_url': random.choice(['https://a.example/cb',
                                               'https://b.example/hook?x=1', '']),
                'return_url': random.choice(['https://site.example/sign-in', '']),
                'phone': random.choice(tel),
                'lead_id': random.choice(leads)},
            'mch_id': random.choice(['2764', '9001']),
            'sign_key': ''.join(random.choice('abcdef0123456789') for _ in range(32)),
            'pay_type': random.choice(pays)})
    # bordes explícitos: el '=' que clean() elimina, y acentos Latin-1
    out.append({'ctx': {'currency': 'INR', 'out_trade_no': 'A=B', 'amount': '1',
                        'callback_url': 'https://c', 'return_url': '',
                        'phone': '+91=99', 'lead_id': 'x=y'},
                'mch_id': '2764', 'sign_key': 'k' * 32, 'pay_type': 'UPI'})
    out.append({'ctx': {'currency': 'INR', 'out_trade_no': 'LMÑ', 'amount': '9',
                        'callback_url': 'https://c', 'return_url': 'https://r',
                        'phone': '+919', 'lead_id': 'Ñáéíóú'},
                'mch_id': '2764', 'sign_key': 'ff' * 16, 'pay_type': 'UPI'})
    return out


def main():
    s = Suite('pagos V2')

    # ══ el endpoint inventado ya no está · §25 ═══════════════════════
    s.section('el endpoint inventado de LeadStudio ya no existe (§25)')

    def no_invented_endpoint_anywhere():
        """Busca el endpoint en posición de USO, no en prosa.

        Documentar que se eliminó es correcto y tiene que seguir
        permitido; lo que no puede existir es una URL viva. Por eso sólo
        cuenta si aparece dentro de comillas (literal de cadena, valor
        JSON, parámetro de un nodo) o como URL suelta tras http(s)://.
        """
        import re
        usado = re.compile(
            r"""(?x)
              (?: ["'][^"'\n]*?/api/leads/[^"'\n]*?/payment-link )
            | (?: https?://[^\s"']*?/api/leads/[^\s"']*?/payment-link )
            """)
        malos = []
        for sub in ('workflows', 'panel', 'sql', 'tools', 'contracts'):
            base = os.path.join(ROOT, sub)
            for dp, dn, fn in os.walk(base):
                dn[:] = [d for d in dn if d != '__pycache__']
                for f in fn:
                    ruta = os.path.join(dp, f)
                    try:
                        txt = open(ruta, encoding='utf-8', errors='replace').read()
                    except Exception:
                        continue
                    for m in usado.finditer(txt):
                        malos.append(f'{os.path.relpath(ruta, ROOT)}: {m.group(0)[:60]}')
        assert not malos, ('todavía hay un uso vivo de /api/leads/{id}/payment-link: '
                           + '; '.join(sorted(set(malos))))

    def invented_endpoint_check_actually_works():
        """Control positivo: si el detector no ve una URL sembrada, el
        check anterior no demuestra nada."""
        import re, tempfile
        usado = re.compile(
            r"""(?x)
              (?: ["'][^"'\n]*?/api/leads/[^"'\n]*?/payment-link )
            | (?: https?://[^\s"']*?/api/leads/[^\s"']*?/payment-link )
            """)
        vivos = ['"https://crm.example/api/leads/123/payment-link"',
                 "url: 'https://x/api/leads/{{id}}/payment-link'",
                 'https://crm.example/api/leads/7/payment-link']
        for v in vivos:
            assert usado.search(v), f'el detector no ve un uso vivo: {v}'
        prosa = ['-- se elimina POST /api/leads/{id}/payment-link',
                 '    POST /api/leads/{id}/payment-link']
        for v in prosa:
            assert not usado.search(v), f'el detector confunde prosa con uso: {v}'
    s.check('control positivo: el detector distingue uso de prosa',
            invented_endpoint_check_actually_works)
    s.check('nadie apunta ya a POST /api/leads/{id}/payment-link',
            no_invented_endpoint_anywhere)

    def only_two_modes():
        assert P.PAYMENT_MODES == ('DIRECT_PROVIDER', 'UNIVERSAL_ROUTER')
    s.check('los modos de pago son exactamente los dos exigidos', only_two_modes)

    # ══ paridad de firma ═════════════════════════════════════════════
    s.section('la firma coincide con el WF7 de producción')

    if not os.path.exists(REFERENCE_JS):
        s.skip('paridad de firma OkPay', 'falta tests/fixtures/okpay_reference.js')
    else:
        casos = build_cases()
        ruta = os.path.join(HERE, '_okpay_cases.json')
        with open(ruta, 'w', encoding='utf-8') as fh:
            json.dump(casos, fh)
        try:
            r = subprocess.run(['node', REFERENCE_JS, ruta],
                               capture_output=True, text=True, timeout=120)
            ref = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError):
            ref = None
        finally:
            if os.path.exists(ruta):
                os.remove(ruta)

        if ref is None:
            s.skip('paridad de firma OkPay', 'node no disponible')
        else:
            def sign_parity():
                malos = []
                for i, (c, j) in enumerate(zip(casos, ref)):
                    py = P.okpay_build_request(c['ctx'], c['mch_id'],
                                               c['sign_key'], c['pay_type'] or 'UPI')
                    if py['sign'] != j['sign']:
                        malos.append(f"#{i} firma py={py['sign'][:12]} "
                                     f"prod={j['sign'][:12]} lead={c['ctx']['lead_id']!r}")
                    elif py['form_body'] != j['form_body']:
                        malos.append(f'#{i} cuerpo distinto')
                assert not malos, (f'{len(malos)} de {len(casos)} casos difieren '
                                   'de producción: ' + '; '.join(malos[:3]))
            s.check(f'las {len(casos)} firmas coinciden con el WF7 real',
                    sign_parity)

            def latin1_not_utf8():
                """El MD5 de producción empaqueta charCodeAt byte a byte: es
                latin-1. Firmar en utf-8 daría otra firma y OkPay rechazaría."""
                import hashlib
                ctx = {'currency': 'INR', 'out_trade_no': 'X', 'amount': '1',
                       'callback_url': '', 'return_url': '', 'phone': '',
                       'lead_id': 'Ñ'}
                got = P.okpay_build_request(ctx, '2764', 'k')['sign']
                base = 'attach=Ñ&currency=INR&mchId=2764&money=1&out_trade_no=X&pay_type=UPI&key=k'
                assert got == hashlib.md5(base.encode('latin-1')).hexdigest()
                assert got != hashlib.md5(base.encode('utf-8')).hexdigest()
            s.check('la firma se calcula en latin-1, como producción, no en utf-8',
                    latin1_not_utf8)

    def three_way_parity():
        """Python (panel) ↔ JS (nodos del workflow) ↔ producción.

        Son tres implementaciones del mismo algoritmo en tres sitios
        distintos. Si cualquiera se desvía, el cobro falla en el cliente.
        """
        casos = build_cases(120)
        ruta = os.path.join(HERE, '_okpay_3way.json')
        with open(ruta, 'w', encoding='utf-8') as fh:
            json.dump(casos, fh)
        script = os.path.join(HERE, '_okpay_3way.js')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(f"""
const fs = require('fs');
eval(fs.readFileSync({json.dumps(os.path.join(ROOT, 'tools', 'js', 'lmpay.js'))}, 'utf8'));
const ref = require({json.dumps(REFERENCE_JS)});
const cases = JSON.parse(fs.readFileSync({json.dumps(ruta)}, 'utf8'));
const out = cases.map(function (c) {{
  let mine = null, err = null;
  try {{ mine = lmOkpayBuild(c.ctx, c.mch_id, c.sign_key, c.pay_type || 'UPI'); }}
  catch (e) {{ err = String(e.message); }}
  const theirs = ref.buildOkPayReference(c.ctx, c.mch_id, c.sign_key, c.pay_type);
  return {{ mine_sign: mine && mine.sign, mine_body: mine && mine.form_body,
            ref_sign: theirs.sign, ref_body: theirs.form_body, err: err }};
}});
console.log(JSON.stringify(out));
""")
        try:
            r = subprocess.run(['node', script], capture_output=True, text=True,
                               timeout=180)
            assert r.returncode == 0, f'el comparador JS falló: {r.stderr[-300:]}'
            filas = json.loads(r.stdout)
        finally:
            for f in (ruta, script):
                if os.path.exists(f):
                    os.remove(f)

        malos = []
        for i, (c, row) in enumerate(zip(casos, filas)):
            py = P.okpay_build_request(c['ctx'], c['mch_id'], c['sign_key'],
                                       c['pay_type'] or 'UPI')
            if row['err']:
                malos.append(f'#{i} el gemelo JS falló: {row["err"]}')
                continue
            if not (py['sign'] == row['mine_sign'] == row['ref_sign']):
                malos.append(f"#{i} py={py['sign'][:10]} js={row['mine_sign'][:10]} "
                             f"prod={row['ref_sign'][:10]}")
            elif not (py['form_body'] == row['mine_body'] == row['ref_body']):
                malos.append(f'#{i} cuerpos distintos')
        assert not malos, (f'{len(malos)} de {len(casos)} divergen entre las tres '
                           'implementaciones: ' + '; '.join(malos[:3]))
    s.check('Python, el gemelo JS y producción firman idéntico (3 vías)',
            three_way_parity)

    def js_twin_also_refuses_non_latin1():
        from _suite import run_node, load_js
        r = run_node(load_js()['lmpay.js'], """
          try { lmOkpayBuild({currency:'INR',out_trade_no:'X',amount:'1',
                              callback_url:'',return_url:'',phone:'',
                              lead_id:'\u0928\u092e\u0938\u094d\u0924\u0947'},
                             '2764','k');
                console.log(JSON.stringify({ok:false}));
          } catch (e) { console.log(JSON.stringify({ok:String(e.message)})); }
        """)
        assert 'LM_PAY_NON_LATIN1' in str(r.get('ok')), \
            'el gemelo JS firma texto que la pasarela no puede validar'
    s.check('el gemelo JS también rechaza lo que no cabe en latin-1',
            js_twin_also_refuses_non_latin1)

    def beyond_latin1_fails_loudly():
        """Un carácter que no cabe en un byte rompe el empaquetado de
        producción y da una firma corrupta. Mejor fallar aquí que mandarle
        al cliente un link que la pasarela va a rechazar."""
        ctx = {'currency': 'INR', 'out_trade_no': 'X', 'amount': '1',
               'callback_url': '', 'return_url': '', 'phone': '',
               'lead_id': 'नमस्ते'}
        try:
            P.okpay_build_request(ctx, '2764', 'k')
            raise AssertionError('firmó un texto que la pasarela no puede validar')
        except P.PaymentConfigError as ex:
            assert 'cannot' in str(ex).lower()
    s.check('un carácter fuera de latin-1 falla en claro, no en silencio',
            beyond_latin1_fails_loudly)

    def signing_key_never_leaks():
        ctx = {'currency': 'INR', 'out_trade_no': 'X', 'amount': '1',
               'callback_url': 'https://c', 'return_url': '', 'phone': '+91',
               'lead_id': 'L'}
        clave = 'SUPERSECRETSIGNINGKEY1234567890'
        r = P.okpay_build_request(ctx, '2764', clave)
        assert clave not in json.dumps(r), 'la clave de firma sale en el resultado'
        assert clave not in r['form_body']
    s.check('la clave de firma nunca aparece en el resultado',
            signing_key_never_leaks)

    def credentials_not_in_config():
        """§61: ni el merchant id ni la clave viven en la base.

        Se mira el SQL sin sus comentarios: explicar por qué NO se siembran
        tiene que seguir permitido; sembrarlas, no.
        """
        import re
        sql = open(os.path.join(ROOT, 'sql', 'parts', '006_payments_v2.sql'),
                   encoding='utf-8').read()
        ejecutable = '\n'.join(ln for ln in sql.splitlines()
                               if not ln.lstrip().startswith('--'))
        # la clave real del WF7 de producción, que no puede viajar nunca
        assert 'a421742f0b404661b8ddb323c51cc99b' not in sql, \
            'la clave de firma de producción está en la migración'
        for mala in ('mchId', 'sign_key', 'signKey'):
            assert mala not in ejecutable, \
                f'la migración siembra {mala!r} en la base'
        # y tampoco un merchant id suelto
        assert not re.search(r"'\s*2764\s*'", ejecutable), \
            'la migración siembra el merchant id de producción'
        meta = P.PAYMENT_ADAPTERS['OKPAY_V1']
        assert 'sign_key' in meta['requires_credential_fields']
        assert 'sign_key' not in meta['requires_config'], \
            'la clave de firma se pide como configuración en vez de credencial'
    s.check('mch_id y sign_key son credencial, nunca configuración en la BD',
            credentials_not_in_config)

    # ══ respuesta ════════════════════════════════════════════════════
    s.section('normalización de la respuesta')

    def success_needs_all_three():
        ok = {'statusCode': 200, 'body': {'code': 0, 'data': {'url': 'https://pay/x',
                                                              'transaction_Id': 'T1'}}}
        r = P.okpay_parse_response(ok)
        assert r['success'] and r['payment_url'] == 'https://pay/x' and r['payment_id'] == 'T1'
        # falta uno de los tres -> no es éxito
        for mal in ({'statusCode': 500, 'body': {'code': 0, 'data': {'url': 'https://p'}}},
                    {'statusCode': 200, 'body': {'code': 4001, 'data': {'url': 'https://p'}}},
                    {'statusCode': 200, 'body': {'code': 0, 'data': {}}}):
            assert not P.okpay_parse_response(mal)['success'], \
                f'se dio por bueno un pago incompleto: {mal}'
    s.check('éxito sólo con HTTP 2xx + code 0 + URL, las tres cosas',
            success_needs_all_three)

    def errors_are_classified():
        for status, esperado in ((401, 'AUTH_ERROR'), (403, 'AUTH_ERROR'),
                                 (503, 'PROVIDER_ERROR'), (400, 'PROVIDER_ERROR')):
            r = P.okpay_parse_response({'statusCode': status, 'body': {'code': 9}})
            assert r['error_class'] == esperado, \
                f'{status} se clasificó {r["error_class"]}, se esperaba {esperado}'
        r = P.okpay_parse_response({'body': {}})
        assert r['error_class'] == 'UNKNOWN', 'sin respuesta no es UNKNOWN'
    s.check('los errores se clasifican: AUTH, PROVIDER y UNKNOWN',
            errors_are_classified)

    def crm_messages_are_english():
        from crm_notes import is_english
        for caso in ({'statusCode': 200, 'body': {'code': 0,
                                                  'data': {'url': 'https://p'}}},
                     {'statusCode': 500, 'body': {'code': 9, 'msg': 'error'}}):
            m = P.okpay_parse_response(caso)['crm_message']
            assert is_english(m), f'mensaje de CRM no inglés: {m!r}'
        # Producción mandaba hinglish. Que no vuelva.
        r = P.okpay_parse_response({'statusCode': 500, 'body': {'code': 9}})
        for mala in ('karne', 'aaya', 'thodi', 'mein'):
            assert mala not in r['crm_message'].lower(), \
                'volvió el mensaje en hinglish del WF7 de producción'
    s.check('los mensajes que van al CRM están en inglés (§26)',
            crm_messages_are_english)

    def router_success_needs_url():
        r = P.router_parse_response({'statusCode': 200,
                                     'body': {'success': True, 'payment_url': None}})
        assert not r['success'], 'un router que dice success sin URL se dio por bueno'
        assert r['error_class'] == 'PROVIDER_ERROR'
        r2 = P.router_parse_response({'statusCode': 200,
                                      'body': {'success': True,
                                               'payment_url': 'https://p',
                                               'payment_id': 'P1',
                                               'provider': 'okpay'}})
        assert r2['success'] and r2['provider'] == 'okpay'
    s.check('un router que dice éxito sin URL no es un éxito',
            router_success_needs_url)

    # ══ validación por modo ══════════════════════════════════════════
    s.section('validación por modo, sin fallback silencioso (§25)')

    def direct_ok():
        assert P.validate_payment_tool(OK_DIRECT) == []
    s.check('India con OkPay bien configurado valida', direct_ok)

    def router_ok():
        assert P.validate_payment_tool(OK_ROUTER) == []
    s.check('un router bien configurado valida', router_ok)

    def disabled_never_blocks():
        """§25: Nepal sin pago no puede bloquear llamar ni abrir cuentas."""
        np = {'enabled': 0, 'mode': 'DIRECT_PROVIDER', 'adapter_key': None,
              'currency': 'NPR'}
        assert P.validate_payment_tool(np) == [], \
            'un país con el pago apagado genera problemas de validación'
    s.check('Nepal con el pago deshabilitado no genera ningún problema',
            disabled_never_blocks)

    def enabled_but_unconfigured_is_not_ready():
        np = {'enabled': 1, 'mode': 'DIRECT_PROVIDER', 'adapter_key': None,
              'currency': 'NPR'}
        issues = P.validate_payment_tool(np)
        assert issues and any('adapter' in i['field'] for i in issues), \
            'Nepal habilitado y sin configurar se reporta como listo'
    s.check('Nepal habilitado pero sin configurar es NOT READY',
            enabled_but_unconfigured_is_not_ready)

    def unsupported_adapter_can_be_stored_but_not_run():
        cfg = dict(OK_DIRECT, adapter_key='MONETIX_V1')
        issues = P.validate_payment_tool(cfg)
        assert issues and 'not supported' in issues[0]['message']
        assert 'disabled' in issues[0]['message'], \
            'no se explica que puede guardarse mientras esté apagado'
        # guardado pero apagado: sin problemas
        assert P.validate_payment_tool(dict(cfg, enabled=0)) == []
    s.check('un adaptador no soportado se guarda apagado, pero no opera (§5)',
            unsupported_adapter_can_be_stored_but_not_run)

    def no_silent_fallback():
        mezcla = dict(OK_DIRECT, router_key='GENERIC_JSON_V1')
        issues = P.validate_payment_tool(mezcla)
        assert any('router' in i['field'] for i in issues), \
            'se admite un país con adaptador Y router: podría caerse de uno a otro'
        mezcla2 = dict(OK_ROUTER, adapter_key='OKPAY_V1')
        assert any('adapter' in i['field'] for i in P.validate_payment_tool(mezcla2))
    s.check('no se puede configurar los dos modos a la vez', no_silent_fallback)

    def resolve_never_falls_back():
        roto = dict(OK_DIRECT, credential_ref=None)
        try:
            P.resolve(roto)
            raise AssertionError('resolve() devolvió algo con configuración incompleta')
        except P.PaymentConfigError as ex:
            assert 'router' not in str(ex).lower() or 'must not' in str(ex).lower(), \
                'el error sugiere caerse al otro modo'
    s.check('resolve() falla en vez de caerse al otro modo',
            resolve_never_falls_back)

    def missing_callback_is_caught():
        issues = P.validate_payment_tool(dict(OK_DIRECT, callback_url=None))
        assert any('callback' in i['field'] for i in issues), \
            'se permite cobrar sin URL de aviso: el pago nunca se confirmaría'
    s.check('sin callback_url no se puede cobrar: el pago no se confirmaría',
            missing_callback_is_caught)

    def bad_urls_caught():
        for campo in ('endpoint', 'callback_url', 'return_url'):
            issues = P.validate_payment_tool(dict(OK_DIRECT, **{campo: 'not-a-url'}))
            assert any(campo in i['field'] for i in issues), \
                f'{campo} acepta algo que no es una URL'
    s.check('endpoint, callback y return tienen que ser URLs', bad_urls_caught)

    # ══ importes ═════════════════════════════════════════════════════
    s.section('importes')

    def amount_bounds():
        assert P.check_amount(OK_DIRECT, 5000) is None
        assert 'below the minimum' in P.check_amount(OK_DIRECT, 100)
        assert 'above the maximum' in P.check_amount(OK_DIRECT, 900000)
        assert 'greater than zero' in P.check_amount(OK_DIRECT, 0)
        assert 'not a valid number' in P.check_amount(OK_DIRECT, 'abc')
    s.check('los límites de importe se aplican con mensajes en inglés',
            amount_bounds)

    def bad_config_json_is_config_error():
        try:
            P.check_amount(dict(OK_DIRECT, config_json='{no json'), 5000)
            raise AssertionError('un config_json roto pasó como válido')
        except P.PaymentConfigError:
            pass
    s.check('un config_json roto es CONFIG_ERROR', bad_config_json_is_config_error)

    def idempotent_order_id():
        a = P.out_trade_no('L-1', 'attempt-3')
        b = P.out_trade_no('L-1', 'attempt-3')
        c = P.out_trade_no('L-1', 'attempt-4')
        assert a == b, 'el mismo pedido da dos identificadores: se cobraría dos veces'
        assert a != c
        assert a.startswith('LM') and len(a) == 26
    s.check('el mismo pedido da siempre el mismo out_trade_no', idempotent_order_id)

    # ══ base de datos ════════════════════════════════════════════════
    if not mysql_available():
        s.skip('configuración sembrada', 'sin MariaDB')
        return s.finish()

    s.section('lo que queda sembrado en la base')
    NAME = 'lm_test_payments'
    fresh_mysql_db(NAME)
    for p in PARTS:
        run_sql_file(NAME, os.path.join(ROOT, 'sql', 'parts', p))
    db = DB(mysql_conn(NAME))

    def seeded_india_is_direct_and_off():
        r = db.one("""SELECT enabled, mode, adapter_key, endpoint
                        FROM country_tool_configs
                       WHERE country_iso='IN' AND tool_type='CREATE_PAYMENT_LINK'""")
        assert r['mode'] == 'DIRECT_PROVIDER', f"India quedó en {r['mode']}"
        assert r['adapter_key'] == 'OKPAY_V1'
        assert r['enabled'] == 0, \
            'India queda ENCENDIDA sin credencial: el agente fallaría ante el cliente'
    s.check('India queda en DIRECT_PROVIDER/OKPAY_V1 y APAGADA hasta tener credencial',
            seeded_india_is_direct_and_off)

    def seeded_nepal_has_no_adapter():
        r = db.one("""SELECT enabled, mode, adapter_key FROM country_tool_configs
                       WHERE country_iso='NP' AND tool_type='CREATE_PAYMENT_LINK'""")
        assert r['enabled'] == 0 and r['adapter_key'] is None, \
            'Nepal quedó con un adaptador que no existe'
    s.check('Nepal queda sin adaptador: no hay uno soportado para Monetix',
            seeded_nepal_has_no_adapter)

    def no_config_router_left_for_payments():
        n = db.one("""SELECT COUNT(*) AS n FROM country_tool_configs
                       WHERE tool_type='CREATE_PAYMENT_LINK'
                         AND mode IN ('CONFIG_ROUTER','CUSTOM_ENDPOINT')""")['n']
        assert n == 0, f'{n} países de pago siguen en el modo inventado'
    s.check('ningún país de pago quedó en el modo inventado',
            no_config_router_left_for_payments)

    def account_tools_untouched():
        """La migración de pagos no puede tocar CREATE_ACCOUNT."""
        r = db.one("""SELECT mode, provider_key, market FROM country_tool_configs
                       WHERE country_iso='IN' AND tool_type='CREATE_ACCOUNT'""")
        assert r['mode'] == 'CONFIG_ROUTER' and r['provider_key'] == 'cashstudio' \
            and r['market'] == 'IND', f'CREATE_ACCOUNT cambió: {r}'
    s.check('la migración de pagos no toca CREATE_ACCOUNT', account_tools_untouched)

    def orders_table_is_idempotent():
        db.execute("""INSERT INTO wf_payment_orders
                        (out_trade_no, lead_id, country_iso, mode, adapter_key,
                         amount, currency)
                      VALUES ('LMTEST1','L-1','IN','DIRECT_PROVIDER','OKPAY_V1',
                              5000,'INR')""")
        try:
            db.execute("""INSERT INTO wf_payment_orders
                            (out_trade_no, lead_id, country_iso, mode, adapter_key,
                             amount, currency)
                          VALUES ('LMTEST1','L-1','IN','DIRECT_PROVIDER','OKPAY_V1',
                                  5000,'INR')""")
            raise AssertionError('se crearon dos pedidos con el mismo out_trade_no')
        except AssertionError:
            raise
        except Exception:
            pass
    s.check('la tabla de pedidos impide cobrar dos veces el mismo pedido',
            orders_table_is_idempotent)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
