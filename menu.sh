#!/bin/bash

while true; do
clear
echo "========================================"
echo "     LANDMARK MARKETS — PBX MONITOR"
echo "========================================"
echo " 1. Live calls today (real-time)"
echo " 2. Call history — last 30 days"
echo " 3. Active channels now"
echo " 4. System status"
echo " 5. Trunk SIP status"
echo " 6. Top 10 answered numbers (today)"
echo " 7. Failed calls analysis (today)"
echo " 8. Hourly breakdown (today)"
echo " 9. Asterisk service status"
echo "10. Restart Asterisk"
echo "11. Exit"
echo "========================================"
read -p "Choose an option: " opt

case $opt in
1)
  watch -n 10 'mysql -t asterisk -e "SELECT calldate, src, dst, duration, billsec, disposition FROM cdr WHERE DATE(calldate)=CURDATE() ORDER BY calldate DESC LIMIT 30;"'
  ;;
2)
  mysql -t asterisk -e "SELECT DATE(calldate) as date, COUNT(*) as total, SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END) as answered, SUM(CASE WHEN disposition='NO ANSWER' THEN 1 ELSE 0 END) as no_answer, CONCAT(ROUND(SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)*100.0/COUNT(*),1),'%') as ans_pct, CONCAT(ROUND(SUM(CASE WHEN disposition='NO ANSWER' THEN 1 ELSE 0 END)*100.0/COUNT(*),1),'%') as no_ans_pct, SUM(CEIL(billsec/60)) as minutes, ROUND(SUM(CEIL(billsec/60))*0.06,4) as cost_usd FROM cdr WHERE calldate >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY DATE(calldate) ORDER BY date DESC;"
  read -p "Press Enter to continue..."
  ;;
3)
  asterisk -rx "core show channels"
  read -p "Press Enter to continue..."
  ;;
4)
  echo "--- CPU & RAM ---"
  top -bn1 | head -5
  echo "--- Disk ---"
  df -h /
  echo "--- Uptime ---"
  uptime
  read -p "Press Enter to continue..."
  ;;
5)
  echo "========================================"
  echo "  TRUNK STATUS — proveedor1 (Technologia Hub)"
  echo "  IP: 37.27.181.236:5080 | User: 650098"
  echo "  Auth: IP Whitelist (no register needed)"
  echo "========================================"
  echo ""
  echo "--- Active SIP Channels ---"
  asterisk -rx "sip show channels"
  echo ""
  echo "--- Calls Today via Trunk ---"
  mysql -t asterisk -e "SELECT COUNT(*) as total_calls, SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END) as answered, CONCAT(ROUND(SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)*100.0/COUNT(*),1),'%') as ans_pct, SUM(CEIL(billsec/60)) as minutes, ROUND(SUM(CEIL(billsec/60))*0.06,4) as cost_usd FROM cdr WHERE DATE(calldate)=CURDATE();"
  echo ""
  echo "--- Last 5 Calls ---"
  mysql -t asterisk -e "SELECT calldate, dst, billsec as secs, disposition FROM cdr WHERE DATE(calldate)=CURDATE() ORDER BY calldate DESC LIMIT 5;"
  read -p "Press Enter to continue..."
  ;;
6)
  mysql -t asterisk -e "SELECT dst, COUNT(*) as calls, ROUND(SUM(billsec)/60,2) as minutes FROM cdr WHERE DATE(calldate)=CURDATE() AND disposition='ANSWERED' GROUP BY dst ORDER BY calls DESC LIMIT 10;"
  read -p "Press Enter to continue..."
  ;;
7)
  mysql -t asterisk -e "SELECT disposition, COUNT(*) as total, CONCAT(ROUND(COUNT(*)*100.0/(SELECT COUNT(*) FROM cdr WHERE DATE(calldate)=CURDATE()),2),'%') as pct FROM cdr WHERE DATE(calldate)=CURDATE() GROUP BY disposition ORDER BY total DESC;"
  read -p "Press Enter to continue..."
  ;;
8)
  mysql -t asterisk -e "SELECT HOUR(calldate) as hour, COUNT(*) as total, SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END) as answered, ROUND(SUM(billsec)/60,2) as minutes FROM cdr WHERE DATE(calldate)=CURDATE() GROUP BY HOUR(calldate) ORDER BY hour;"
  read -p "Press Enter to continue..."
  ;;
9)
  systemctl status asterisk --no-pager
  read -p "Press Enter to continue..."
  ;;
10)
  read -p "Are you sure you want to restart Asterisk? (yes/no): " confirm
  if [ "$confirm" = "yes" ]; then
    systemctl restart asterisk
    echo "Asterisk restarted."
  else
    echo "Cancelled."
  fi
  read -p "Press Enter to continue..."
  ;;
11)
  exit 0
  ;;
*)
  echo "Invalid option."
  read -p "Press Enter to continue..."
  ;;
esac
done
