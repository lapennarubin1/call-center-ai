#!/bin/bash
export MYSQL_PWD="RwPass2026xK"
LEAD_ID="d36258f3-91d5-4cc3-844d-7653972a6efd"
FECHA="${1:-2026-09-23 10:00:00}"   # ej: "2026-09-17 10:00:00" o "2026-09-01 10:00:00"

echo "── ¿Ya llegó por el sync de WF14-CRM? ──"
mysql -u panel_rw asterisk -e "SELECT lead_id, phone, country, provider, status, stage, last_contacted_at, synced_at FROM crm_leads WHERE lead_id='$LEAD_ID';"
mysql -u panel_rw asterisk -e "SELECT lead_id, phone, country, provider, stage, created_at, account_opened_at, synced_at FROM crm_conversions WHERE lead_id='$LEAD_ID';"

echo
echo "── Insertando/backdateando directo en crm_leads y crm_conversions con fecha: $FECHA ──"
mysql -u panel_rw asterisk -e "
INSERT INTO crm_leads (lead_id, full_name, phone, country, country_code, language, status, stage, call_attempts, do_not_call, last_contacted_at, provider, synced_at)
VALUES ('$LEAD_ID','Dani Dani Dani','+911234567890','india','+91',NULL,'NOT_CONTACTED','INTERESTED',0,1,'$FECHA',NULL,NOW())
ON DUPLICATE KEY UPDATE last_contacted_at='$FECHA', stage='INTERESTED', synced_at=NOW();

INSERT INTO crm_conversions (lead_id, full_name, phone, country, provider, stage, created_at, synced_at, account_opened_at)
VALUES ('$LEAD_ID','Dani Dani Dani','+911234567890','india',NULL,'INTERESTED','$FECHA',NOW(),'$FECHA')
ON DUPLICATE KEY UPDATE stage='INTERESTED', account_opened_at='$FECHA', created_at='$FECHA', synced_at=NOW();
"

echo
echo "── Resultado ──"
mysql -u panel_rw asterisk -e "SELECT lead_id, phone, country, provider, stage, created_at, account_opened_at FROM crm_conversions WHERE lead_id='$LEAD_ID';"
echo
echo "Ahora andá al panel → pestaña CRM → India → filtrá por el día '$FECHA' y buscá a Dani Dani Dani."
echo "OJO: WF14-CRM corre cada 5 min y va a volver a pisar esto con la fecha real (hoy) en el próximo sync."
echo "Si querés repetir el test, volvé a correr: bash /root/backdate_panel_dani.sh \"2026-09-17 10:00:00\""
