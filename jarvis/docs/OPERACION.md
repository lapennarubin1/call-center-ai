# Operación diaria

## Comandos

```bash
systemctl status jarvis          # ¿está corriendo?
systemctl restart jarvis         # reiniciar
journalctl -u jarvis -f          # ver el log en vivo
journalctl -u jarvis -n 100      # últimas 100 líneas

cd /opt/jarvis
venv/bin/python -m app.tools check              # diagnóstico completo
venv/bin/python -m app.tools voices             # listar voces de ElevenLabs
venv/bin/python -m app.tools skills             # catálogo de capacidades
venv/bin/python -m app.tools try saldo_sip      # probar una skill sin la interfaz
venv/bin/python -m app.tools try resumen_llamadas pais=india periodo=hoy
```

## Cuando algo no anda

**No abre la página**
```bash
systemctl status jarvis
curl -s localhost:8090/health
```
El `/health` dice exactamente qué falta.

**Responde pero sin voz**
La respuesta está en pantalla igual — es un fallo de ElevenLabs, no del cerebro.
```bash
venv/bin/python -m app.tools check    # mirá la línea de ElevenLabs
```
Causas típicas: clave vencida, cuota agotada, o un ID de voz que ya no existe en la cuenta.

**Dice "no pude consultar..."**
Es un problema de la base o de una integración, y Jarvis te lo está diciendo bien.
```bash
venv/bin/python -m app.tools check
```

**No controla el call center**
Necesita `LM_N8N_API_KEY` en el `.env` y que el panel tenga la tabla `n8n_switches`
(versión v24 o superior).

**Contesta cosas raras o desactualizadas**
Preguntale directamente: *"¿cómo venís funcionando?"* — te muestra qué skills viene
usando, cuáles fallan y qué aprendió.

## Mantenimiento

**Ver qué acciones se ejecutaron**
```bash
sqlite3 /opt/jarvis/data/jarvis.db \
  "SELECT created_at, skill, params FROM audit_log ORDER BY id DESC LIMIT 20;"
```

**Ver qué aprendió**
```bash
sqlite3 /opt/jarvis/data/jarvis.db "SELECT lesson FROM lessons;"
sqlite3 /opt/jarvis/data/jarvis.db "SELECT topic, content FROM memories;"
```

**Borrar una lección mal aprendida**
```bash
sqlite3 /opt/jarvis/data/jarvis.db "DELETE FROM lessons WHERE id = <id>;"
```

**Limpiar historial viejo** (la memoria y las lecciones NO se tocan)
```bash
sqlite3 /opt/jarvis/data/jarvis.db \
  "DELETE FROM conversations WHERE created_at < datetime('now','-30 days');"
```

**Backup**
```bash
sqlite3 /opt/jarvis/data/jarvis.db ".backup /root/jarvis-$(date +%F).db"
```

## Actualizar

```bash
cd /opt/jarvis
cp -r app /root/app-backup-$(date +%F)     # por las dudas
# copiar los archivos nuevos sobre app/
systemctl restart jarvis
venv/bin/python -m app.tools check
```

El `.env` y la carpeta `data/` nunca se tocan al actualizar.
