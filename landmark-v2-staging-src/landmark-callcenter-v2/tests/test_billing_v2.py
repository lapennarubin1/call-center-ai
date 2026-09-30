#!/usr/bin/env python3
"""Facturación (§34-42).

Lo que estas pruebas defienden:
  · Provider1 se da de alta UNA vez, no dos (§34)
  · los 5 modelos existen y no son un ENUM de SQL (§35)
  · dos rutas del mismo país pueden tener tarifas distintas (§36)
  · Stringee es MONTHLY_FLAT, no exige precio/min y NUNCA muestra $0/min (§37)
  · EFFECTIVE COST va etiquetado y nunca se presenta como tarifa (§38)
  · el coste se calcula distinto por modelo, y CUSTOM no se inventa (§39)
  · depósitos, precios e historial sobreviven a la migración (§40)
  · apagar un proveedor no borra su facturación (§41)
"""
import os
import sys
from decimal import Decimal

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, mysql_available, fresh_mysql_db,   # noqa: E402
                    mysql_conn, run_sql_file, DB)

import billing as B                                                 # noqa: E402

LEGACY_SEED = """
CREATE TABLE sip_providers (
  id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(150) NOT NULL,
  active TINYINT(1) NOT NULL DEFAULT 1, billing_start_date DATE NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE sip_provider_pricing (
  id INT AUTO_INCREMENT PRIMARY KEY, provider_id INT NOT NULL,
  country VARCHAR(50) NOT NULL, trunk_name VARCHAR(100) NOT NULL,
  price_per_minute DECIMAL(10,4) NOT NULL,
  UNIQUE KEY uniq_provider_country (provider_id, country));
CREATE TABLE sip_deposits (
  id INT AUTO_INCREMENT PRIMARY KEY, provider_id INT NOT NULL,
  amount_usd DECIMAL(10,2) NOT NULL, reference VARCHAR(255),
  deposit_date DATE NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
"""
PARTS = ('001_multi_country_config_v2_2.sql', '002_callcenter_suite_v2.sql',
         '003_legacy_compat_tables.sql', '004_billing_v2.sql',
         '005_legacy_backup_v2.sql')
ACTOR = 'test-master'


def build_db(name):
    """Base con el legacy REAL sembrado + la migración completa encima."""
    fresh_mysql_db(name)
    c = mysql_conn(name)
    with c.cursor() as cur:
        for stmt in [s for s in LEGACY_SEED.split(';') if s.strip()]:
            cur.execute(stmt)
        cur.execute("INSERT INTO sip_providers (name, active) VALUES "
                    "('650098 @ 2.28.59.175', 1)")
        pid = cur.lastrowid
        for country, trunk, price in (('india', 'proveedor1', '0.3000'),
                                      ('mexico', 'proveedor-mx', '0.3000'),
                                      ('nepal', 'proveedor-nepal', '0.3600')):
            cur.execute("INSERT INTO sip_provider_pricing "
                        "(provider_id,country,trunk_name,price_per_minute) "
                        "VALUES (%s,%s,%s,%s)", (pid, country, trunk, price))
        cur.execute("INSERT INTO sip_deposits "
                    "(provider_id,amount_usd,reference,deposit_date) "
                    "VALUES (%s,'500.00','wire-001','2026-01-15')", (pid,))
    c.close()
    for part in PARTS:
        run_sql_file(name, os.path.join(ROOT, 'sql', 'parts', part))
    return pid


def main():
    s = Suite('facturación V2')

    # ══ sin base: lo que es lógica pura ══════════════════════════════
    s.section('modelos de facturación (§35)')

    def five_models():
        for m in ('PER_MINUTE', 'MONTHLY_FLAT', 'PER_CALL', 'INCLUDED', 'CUSTOM'):
            assert B.is_supported(m), f'falta el modelo {m}'
    s.check('los 5 modelos exigidos están soportados', five_models)

    def not_a_sql_enum():
        sql = open(os.path.join(ROOT, 'sql', 'parts', '004_billing_v2.sql'),
                   encoding='utf-8').read()
        assert 'billing_model VARCHAR' in sql, 'billing_model no es VARCHAR'
        import re
        assert not re.search(r"(?i)billing_model\s+ENUM", sql), \
            'billing_model se declaró como ENUM de SQL'
    s.check('billing_model es VARCHAR + registro, no ENUM de SQL', not_a_sql_enum)

    def unknown_model_is_visible_not_fatal():
        meta = B.model_meta('WEIRD_MODEL')
        assert 'unsupported' in meta['label'].lower(), \
            'un modelo desconocido no se marca como no soportado'
        issues = B.validate_provider_billing(
            {'billing_model': 'WEIRD_MODEL', 'billing_currency': 'USD'})
        assert issues, 'un modelo no soportado se reporta como válido'
    s.check('un modelo desconocido se muestra, no revienta la vista',
            unknown_model_is_visible_not_fatal)

    # ══ Stringee · §37 ═══════════════════════════════════════════════
    s.section('Stringee es cuota fija, no $0/min (§37)')

    STRINGEE = {'billing_model': 'MONTHLY_FLAT', 'billing_currency': 'USD',
                'monthly_fee': '1200.00', 'adapter_key': 'STRINGEE_WORKER'}

    def no_zero_per_minute():
        d = B.price_per_minute_display(STRINGEE, {'price_per_minute': None})
        assert d['text'] == 'N/A', f"muestra {d['text']!r} en vez de N/A"
        assert d['is_rate'] is False
        assert '0' not in d['text'], 'sigue apareciendo un cero como tarifa'
    s.check("Price/min de un MONTHLY_FLAT es 'N/A', nunca '$0/min'",
            no_zero_per_minute)

    def zero_rate_is_not_invented_either():
        """Ni siquiera si alguien dejó un 0 guardado en la ruta."""
        d = B.price_per_minute_display(STRINGEE, {'price_per_minute': '0.0000'})
        assert d['text'] == 'N/A', \
            'un 0 guardado se muestra como tarifa de un proveedor de cuota fija'
    s.check('un 0 guardado tampoco se muestra como tarifa', zero_rate_is_not_invented_either)

    def usage_is_unlimited():
        assert B.usage_display(STRINGEE) == 'Unlimited'
    s.check("el uso de un MONTHLY_FLAT se describe 'Unlimited'", usage_is_unlimited)

    def monthly_flat_does_not_require_rate():
        rutas = [{'route_key': 'IN_STRINGEE', 'price_per_minute': None,
                  'archived_at': None}]
        issues = B.validate_provider_billing(STRINGEE, rutas)
        assert not issues, f'se le exige tarifa a un MONTHLY_FLAT: {issues}'
    s.check('a un MONTHLY_FLAT no se le exige precio por minuto',
            monthly_flat_does_not_require_rate)

    def monthly_flat_requires_fee():
        issues = B.validate_provider_billing(
            {'billing_model': 'MONTHLY_FLAT', 'billing_currency': 'USD',
             'monthly_fee': None})
        assert any('monthly_fee' in i['field'] for i in issues), \
            'un MONTHLY_FLAT sin cuota se reporta válido'
    s.check('un MONTHLY_FLAT sin cuota mensual es NOT READY',
            monthly_flat_requires_fee)

    # ══ coste efectivo · §38 ═════════════════════════════════════════
    s.section('EFFECTIVE COST no es una tarifa (§38)')

    def effective_is_labelled():
        ec = B.effective_cost(STRINGEE, {'calls': 4000, 'answered': 1200,
                                         'talk_minutes': 3000})
        assert ec['label'] == 'EFFECTIVE COST'
        assert ec['is_contract_rate'] is False
        assert 'not the' in ec['disclaimer'].lower()
        assert set(ec['items']) == {'per_call', 'per_answered_call', 'per_talk_minute'}
    s.check('el coste efectivo viaja etiquetado y marcado como no-tarifa',
            effective_is_labelled)

    def effective_math():
        ec = B.effective_cost(STRINGEE, {'calls': 4000, 'answered': 1200,
                                         'talk_minutes': 3000})
        assert ec['items']['per_call']['value'] == Decimal('1200.00') / 4000
        assert ec['items']['per_answered_call']['value'] == Decimal('1200.00') / 1200
        assert ec['items']['per_talk_minute']['value'] == Decimal('1200.00') / 3000
    s.check('cuota ÷ llamadas, ÷ contestadas y ÷ minutos hablados', effective_math)

    def effective_never_divides_by_zero():
        ec = B.effective_cost(STRINGEE, {'calls': 0, 'answered': 0, 'talk_minutes': 0})
        for k, v in ec['items'].items():
            assert v['value'] is None, f'{k} inventó un número sin uso'
    s.check('sin uso, el coste efectivo es None y no divide por cero',
            effective_never_divides_by_zero)

    def effective_not_for_per_minute():
        ec = B.effective_cost({'billing_model': 'PER_MINUTE', 'billing_currency': 'USD'},
                              {'calls': 100})
        assert ec['applicable'] is False, \
            'se calcula coste efectivo para un proveedor que ya tiene tarifa'
    s.check('no se calcula coste efectivo donde ya hay tarifa real',
            effective_not_for_per_minute)

    # ══ coste contractual por modelo · §39 ═══════════════════════════
    s.section('coste contractual por modelo (§39)')

    USAGE = {'by_route': {'IN_PROVEEDOR1': {'billable_minutes': 100, 'billable_calls': 40},
                          'MX_PROVEEDOR1': {'billable_minutes': 50, 'billable_calls': 20}}}

    def per_minute_cost():
        c = B.contract_cost({'billing_model': 'PER_MINUTE', 'billing_currency': 'USD'},
                            USAGE, {'IN_PROVEEDOR1': '0.30', 'MX_PROVEEDOR1': '0.50'})
        assert c['amount'] == Decimal('0.30') * 100 + Decimal('0.50') * 50
        assert 'minute' in c['basis']
    s.check('PER_MINUTE = minutos facturables × tarifa de ruta', per_minute_cost)

    def per_call_cost():
        c = B.contract_cost({'billing_model': 'PER_CALL', 'billing_currency': 'USD'},
                            USAGE, {'IN_PROVEEDOR1': '0.10', 'MX_PROVEEDOR1': '0.20'})
        assert c['amount'] == Decimal('0.10') * 40 + Decimal('0.20') * 20
    s.check('PER_CALL = llamadas facturables × tarifa por llamada', per_call_cost)

    def monthly_flat_cost_is_the_fee():
        c = B.contract_cost(STRINGEE, USAGE)
        assert c['amount'] == Decimal('1200.00')
        assert 'did not generate' in (c['note'] or ''), \
            'no se aclara que las llamadas no generaron la cuota'
    s.check('MONTHLY_FLAT cuesta la cuota, y se aclara que no la generó cada llamada',
            monthly_flat_cost_is_the_fee)

    def custom_is_not_invented():
        c = B.contract_cost({'billing_model': 'CUSTOM', 'billing_currency': 'USD'}, USAGE)
        assert c['amount'] is None, 'CUSTOM inventó un importe'
        assert 'not calculated' in (c['note'] or '').lower()
    s.check('CUSTOM no inventa un cálculo: devuelve None, no 0',
            custom_is_not_invented)

    def included_is_zero_with_reason():
        c = B.contract_cost({'billing_model': 'INCLUDED', 'billing_currency': 'USD'}, USAGE)
        assert c['amount'] == Decimal('0')
        assert 'no separate charge' in (c['note'] or '').lower()
    s.check('INCLUDED es 0 con su motivo escrito', included_is_zero_with_reason)

    def missing_rate_is_flagged_not_silent():
        c = B.contract_cost({'billing_model': 'PER_MINUTE', 'billing_currency': 'USD'},
                            USAGE, {'IN_PROVEEDOR1': '0.30'})     # falta MX
        assert 'MX_PROVEEDOR1' in (c['note'] or ''), \
            'una ruta sin tarifa se suma como 0 sin avisar'
    s.check('una ruta sin tarifa se avisa, no se cuenta como 0',
            missing_rate_is_flagged_not_silent)

    # ══ con MariaDB ══════════════════════════════════════════════════
    if not mysql_available():
        for n in ('identidad única de proveedor', 'tarifas por ruta',
                  'preservación de SIP Balance', 'apagar no borra facturación'):
            s.skip(n, 'sin MariaDB')
        return s.finish()

    s.section('una sola identidad de proveedor (§34)')
    NAME = 'lm_test_billing'
    legacy_id = build_db(NAME)
    db = DB(mysql_conn(NAME))

    def linked_not_duplicated():
        provs = {p['code']: p for p in B.providers_billing(db)}
        p1 = provs['proveedor1']
        assert p1['legacy_sip_provider_id'] == legacy_id, \
            'Provider1 no quedó vinculado con su ficha de SIP Balance'
        assert p1['legacy'] and p1['legacy']['name'] == '650098 @ 2.28.59.175'
        assert not p1['legacy_missing']
    s.check('Provider1 apunta a su ficha de SIP Balance, no se duplica',
            linked_not_duplicated)

    def stringee_needs_no_sip_balance():
        provs = {p['code']: p for p in B.providers_billing(db)}
        st = provs['stringee']
        assert st['legacy_expected'] is False, \
            'se le exige ficha de SIP Balance a Stringee, que no es un trunk SIP'
        assert st['legacy_missing'] is False
    s.check('a Stringee no se le exige ficha de SIP Balance',
            stringee_needs_no_sip_balance)

    def one_legacy_one_voice():
        try:
            B.link_legacy_provider(db, ACTOR, 'stringee', legacy_id)
            raise AssertionError('se vinculó la misma ficha legacy a dos proveedores')
        except B.BillingError as ex:
            assert 'already linked' in str(ex)
    s.check('una ficha de SIP Balance no se vincula a dos proveedores',
            one_legacy_one_voice)

    s.section('tarifas por ruta (§36)')

    def two_routes_same_country_different_rates():
        """Lo que sip_provider_pricing NO podía: su UNIQUE(provider,país)
        obliga a un único precio por país y proveedor."""
        db.execute("""INSERT INTO call_routes
                        (route_key, iso, provider_id, enabled, priority,
                         capacity_default, trunk_name)
                      SELECT 'IN_PROVEEDOR1_BIS','IN',id,0,15,1,'proveedor1-bis'
                        FROM voice_providers WHERE code='proveedor1'""")
        B.set_route_rate(db, ACTOR, 'IN_PROVEEDOR1', price_per_minute='0.3000')
        B.set_route_rate(db, ACTOR, 'IN_PROVEEDOR1_BIS', price_per_minute='0.4500')
        rates = B.route_rates(db)
        assert rates['IN_PROVEEDOR1'] == Decimal('0.3000')
        assert rates['IN_PROVEEDOR1_BIS'] == Decimal('0.4500'), \
            'dos rutas del mismo país no pueden tener tarifas distintas'
    s.check('dos rutas de India bajo Provider1 con tarifas distintas',
            two_routes_same_country_different_rates)

    def legacy_pricing_untouched_by_route_rate():
        r = db.one("SELECT price_per_minute FROM sip_provider_pricing WHERE country='india'")
        assert str(r['price_per_minute']) == '0.3000', \
            'cambiar la tarifa de una ruta modificó el precio legacy'
    s.check('cambiar una tarifa de ruta no toca sip_provider_pricing',
            legacy_pricing_untouched_by_route_rate)

    def negative_rate_rejected():
        try:
            B.set_route_rate(db, ACTOR, 'IN_PROVEEDOR1', price_per_minute='-1')
            raise AssertionError('se aceptó una tarifa negativa')
        except B.BillingError:
            pass
    s.check('una tarifa negativa se rechaza', negative_rate_rejected)

    s.section('SIP Balance sobrevive (§40)')

    def deposits_survive():
        r = db.one("SELECT COUNT(*) AS n, SUM(amount_usd) AS t FROM sip_deposits")
        assert r['n'] == 1 and str(r['t']) == '500.00', \
            'la migración perdió depósitos'
    s.check('los depósitos siguen ahí tras la migración', deposits_survive)

    def legacy_prices_survive():
        rows = {r['country']: str(r['price_per_minute'])
                for r in db.q("SELECT country, price_per_minute FROM sip_provider_pricing")}
        assert rows == {'india': '0.3000', 'mexico': '0.3000', 'nepal': '0.3600'}, \
            f'los precios legacy cambiaron: {rows}'
    s.check('los precios legacy siguen intactos', legacy_prices_survive)

    def rerun_does_not_reset():
        db.execute("UPDATE call_routes SET price_per_minute='0.9999' WHERE route_key='NP_PROVEEDOR1'")
        db.execute("UPDATE sip_provider_pricing SET price_per_minute='0.4200' WHERE country='india'")
        for part in PARTS:
            run_sql_file(NAME, os.path.join(ROOT, 'sql', 'parts', part))
        assert str(db.one("SELECT price_per_minute AS p FROM call_routes "
                          "WHERE route_key='NP_PROVEEDOR1'")['p']) == '0.9999', \
            'la re-ejecución pisó una tarifa de ruta editada a mano'
        assert str(db.one("SELECT price_per_minute AS p FROM sip_provider_pricing "
                          "WHERE country='india'")['p']) == '0.4200', \
            'la re-ejecución pisó un precio legacy editado a mano'
    s.check('re-ejecutar la migración no resetea precios editados',
            rerun_does_not_reset)

    def route_rate_inherited_once():
        r = db.one("SELECT price_per_minute AS p FROM call_routes WHERE route_key='IN_PROVEEDOR1'")
        assert r['p'] is not None, 'la ruta no heredó tarifa del precio legacy'
    s.check('una ruta nueva hereda la tarifa legacy de su país',
            route_rate_inherited_once)

    s.section('apagar ≠ borrar (§41)')

    def disabling_keeps_billing():
        db.execute("UPDATE voice_providers SET enabled=0 WHERE code='proveedor1'")
        p = {x['code']: x for x in B.providers_billing(db)}['proveedor1']
        assert p['enabled'] == 0
        assert p['billing_model'] == 'PER_MINUTE', 'apagarlo perdió el modelo'
        assert p['legacy_sip_provider_id'] == legacy_id, 'apagarlo perdió el vínculo'
        assert db.one("SELECT COUNT(*) AS n FROM sip_deposits")['n'] == 1, \
            'apagar el proveedor borró depósitos'
        assert db.one("SELECT COUNT(*) AS n FROM call_routes "
                      "WHERE provider_id=(SELECT id FROM voice_providers "
                      "WHERE code='proveedor1')")['n'] > 0, \
            'apagar el proveedor borró sus rutas'
        db.execute("UPDATE voice_providers SET enabled=1 WHERE code='proveedor1'")
    s.check('apagar un proveedor conserva modelo, vínculo, rutas y depósitos',
            disabling_keeps_billing)

    s.section('auditoría (§53/§54)')

    def changes_are_audited():
        antes = len(B.audit_log(db, 'PROVIDER'))
        B.set_provider_billing(db, ACTOR, 'stringee', 'MONTHLY_FLAT',
                               currency='USD', monthly_fee='1500.00',
                               reason='contract renewal')
        filas = B.audit_log(db, 'PROVIDER')
        assert len(filas) > antes, 'un cambio de facturación no dejó rastro'
        assert any(f['actor'] == ACTOR and f['reason'] == 'contract renewal'
                   for f in filas)
    s.check('cada cambio de facturación deja quién, qué y por qué',
            changes_are_audited)

    def audit_stores_no_secrets():
        for f in B.audit_log(db, limit=500):
            blob = ' '.join(str(f.get(k) or '') for k in
                            ('field', 'old_value', 'new_value', 'reason'))
            assert 'password' not in blob.lower() and 'token' not in blob.lower(), \
                f'la auditoría guardó algo con pinta de secreto: {blob[:80]}'
    s.check('la auditoría no guarda nada con pinta de secreto',
            audit_stores_no_secrets)

    def switching_model_clears_stale_fee():
        B.set_provider_billing(db, ACTOR, 'stringee', 'PER_MINUTE', currency='USD')
        p = {x['code']: x for x in B.providers_billing(db)}['stringee']
        assert p['monthly_fee'] is None, \
            'al cambiar de modelo quedó colgando una cuota mensual que ya no se cobra'
        B.set_provider_billing(db, ACTOR, 'stringee', 'MONTHLY_FLAT',
                               currency='USD', monthly_fee='1200.00')
    s.check('cambiar de modelo no deja colgando la cuota anterior',
            switching_model_clears_stale_fee)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
