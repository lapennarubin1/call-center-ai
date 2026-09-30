# Panel de Control — Landmark Markets

Panel de reportes y análisis sobre el CDR de Asterisk y la base de leads.
Corre en el mismo VPS, no depende de servicios externos y no toca ni
Asterisk, ni el CDR, ni los workflows existentes.

---

## Qué muestra

**Telefonía** (fuente: CDR de Asterisk, en tiempo real)

| Métrica | Qué responde |
|---|---|
| Intentos de llamada | Cuántas marcaciones salieron |
| Contestadas · **ASR** | Cuántas descolgaron, y qué % del total |
| **NER** (red efectiva) | Separa "no contestaron" de "falló la red" — si esto baja, el problema es del proveedor, no de los leads |
| **ACD** (duración media) | Cuánto dura una conversación en promedio |
| Minutos facturados | Con redondeo por minuto completo, igual que `menu.sh` |
| Costo · por contestada · por intento | Cuánto cuesta cada cosa realmente |
| Fallos técnicos | Llamadas que ni siquiera llegaron a sonar |
| Conversaciones reales | Contestadas de más de 30 s — el resto es cuelgue inmediato |

**Embudo comercial** (fuente: hojas `leads` y `conversions`, vía WF14)

Intentos → Contestadas → Conversación real → Cuenta abierta, con el
porcentaje que sobrevive en cada paso, **CPA** (costo por cuenta abierta)
y costo por contacto.

**Períodos:** hoy, ayer, semana, mes, año, últimos 7/30/90 días y rango
personalizado. Todo exportable a CSV listo para Excel.

---

## Instalación

### 1. Subir los archivos al VPS

```bash
scp -r panel root@200.141.5.142:/root/
```

### 2. Instalar

```bash
ssh root@200.141.5.142
cd /root/panel
sudo bash deploy/install.sh
```

El instalador crea las tablas, instala las dependencias en un entorno
virtual aislado, configura el servicio `systemd` y lo arranca. Al terminar
muestra la URL y la contraseña generada — **anotala, no se vuelve a mostrar**.

Se puede volver a ejecutar cuantas veces haga falta sin romper nada:
conserva la contraseña ya configurada y no duplica datos.

### 3. Sincronizar los leads (WF14)

El panel **no consulta Google Sheets en vivo**, y eso es deliberado: la
cuota de la API es de 60 lecturas por minuto y WF2 ya la consume casi
entera. Si el panel leyera en vivo, chocaría con el mismo error de cuota
que rompía WF9.

En su lugar, **WF14** copia las hojas a MySQL cada 15 minutos:

1. Importar `deploy/WF14_Sync_Panel.json` en n8n
2. Crear la credencial MySQL que pide el workflow:
   - Host `localhost` · Base `asterisk`
   - Usuario `panel_rw` · Contraseña `PanelPass2026xK`
3. Activarlo

Mientras WF14 no corra, el panel muestra igual todas las métricas de
telefonía (esas salen del CDR directo) y avisa en pantalla que faltan
los datos de conversión. Si WF14 se cae más tarde, el panel muestra un
aviso rojo con cuántos minutos lleva sin actualizarse — no muestra
cifras viejas como si fueran de hoy.

---

## Uso diario

**Abrir:** `http://200.141.5.142:8080`

**Exportar:** los botones *Resumen* y *Cuentas* arriba a la derecha, y
*Descargar CSV* en la tabla de detalle. Los CSV salen con BOM y separador
`;` para que Excel en español los abra bien sin pasos extra.

**Cambiar la tarifa** sin reinstalar — útil si el proveedor cambia el precio:

```bash
nano /opt/landmark-panel/.env      # editar LM_RATE
systemctl restart landmark-panel
```

También se puede probar una tarifa distinta al vuelo sin tocar nada:
`http://200.141.5.142:8080/?period=month&rate=0.08`

---

## Operación

```bash
systemctl status  landmark-panel     # ver si está corriendo
systemctl restart landmark-panel     # reiniciar
journalctl -u landmark-panel -f      # registros en vivo
curl localhost:8080/health           # comprobación rápida
```

**Cambiar la contraseña:**

```bash
nano /opt/landmark-panel/.env        # editar LM_USER / LM_PASS
systemctl restart landmark-panel
```

---

## Seguridad

El panel pide usuario y contraseña, pero **viaja por HTTP sin cifrar**.
Mientras el firewall del VPS siga desactivado, cualquiera que conozca la
IP y el puerto puede llegar a la pantalla de login.

Antes de darle acceso a más gente, conviene cerrar el puerto 8080 a todo
salvo tu IP:

```bash
ufw allow from TU_IP_FIJA to any port 8080
ufw deny 8080
```

Esto queda dentro del pendiente que ya tenías anotado de activar
`nftables` y `fail2ban` en producción.

El usuario de base de datos `panel_rw` sólo puede **leer** el CDR — no
puede modificar ni borrar registros de llamadas.

---

## Arquitectura

```
  CDR de Asterisk (MySQL)  ─────────────┐
  lectura directa, sin intermediarios   │
                                        ▼
  Google Sheets ──WF14 (15 min)──▶  MySQL  ──▶  Panel Flask  ──▶  Navegador
  leads + conversions              panel_*        :8080            SVG puro
```

Todo el dibujo de gráficos es SVG generado en el servidor: no hay
librerías de terceros ni CDN, así que el panel funciona igual aunque el
navegador no tenga salida a internet.

Las consultas se cachean 60 segundos, así refrescar la página muchas
veces no vuelve a agregar cientos de miles de filas.

---

## Archivos

```
app/
  server.py       Servidor web, autenticación, API y exportación
  analytics.py    Todas las métricas — SQL portable MySQL/SQLite
  charts.py       Geometría de los gráficos
  templates/      Pantallas
  static/         Hoja de estilos
deploy/
  install.sh              Instalador
  schema.sql              Tablas del panel + índices del CDR
  requirements.txt        Dependencias
  WF14_Sync_Panel.json    Sincronización Sheets → MySQL
tests/
  make_mock_db.py    Genera una base de prueba con el esquema real
  test_analytics.py  Métricas
  test_charts.py     Geometría
  test_e2e.py        Rutas HTTP completas
```

### Correr las pruebas

```bash
cd panel
python3 tests/make_mock_db.py
python3 tests/test_analytics.py
python3 tests/test_charts.py
python3 tests/test_e2e.py
```

Se validaron contra una base de **652.677 registros** generada con el
esquema exacto de Asterisk y tus volúmenes reales: ~2.000-2.500 llamadas
diarias, ~12 % de contestación, ventana 11:00-19:00 IST y domingos sin
operación.
