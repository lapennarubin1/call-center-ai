# WF10 — rollback exacto

Se puede revertir cada parte por separado. Ninguna parte borra datos.

## A) WF10 en n8n → volver a la versión anterior (mismo workflow)

1. Abrí **WF10** (`TW1CHksqyf66MjQz`). Dejalo activo.
2. **Ctrl+A** → **Delete**, sin guardar.
3. `⋯` → **Import from File…** → `WF10_ORIGINAL_para_rollback.json` (es el archivo que me mandaste, sin cambios).
4. **Ctrl+S**. Vuelve la versión anterior, con la misma URL y el mismo id.

También podés usar `⋯` → **Workflow history** → la versión anterior → **Restore**, si tu n8n la tiene.

Las claves que dejó la versión nueva en el *static data* (`wf10_first_run_ms`, `wf10_fail`, `wf10_fail_asterisk`) no afectan a la versión vieja.

## B) Asterisk → dialplan y cron como estaban (sin reiniciar Asterisk)

```bash
cd /root/wf10
./ASTERISK_WF10_FIX.sh rollback            # usa el último apply vigente; o: ./ASTERISK_WF10_FIX.sh rollback /root/wf10-fix-backups/<fecha>
./ASTERISK_WF10_FIX.sh status              # debe decir: contextos con el fix aplicado: 0
crontab -l | grep send_recordings          # si habías hecho finalize, la línea vuelve activa
```

- **Qué restaura.** Los archivos del dialplan exactamente como estaban (desde el backup), con `dialplan reload` y sin restart. Si habías corrido `finalize`, también restaura el crontab.
- **Controles.** Se niega si los archivos fueron editados después del `apply`; en ese caso revisá y usá `FORCE=1`. También funciona con Asterisk caído: restaura los archivos y se cargan al arrancar.
- **Post-script.** `/usr/local/bin/wf10-mixmon-post.sh` **queda instalado a propósito**, porque las llamadas que empezaron antes del rollback lo ejecutan al colgar. Sin referencias en el dialplan no hace nada. Para borrarlo cuando no queden llamadas de antes:

```bash
asterisk -rx "core show channels count"     # esperar a que no queden llamadas iniciadas antes del rollback
rm -f /usr/local/bin/wf10-mixmon-post.sh
```

## C) Datos → no hay nada que deshacer

- WF10 nuevo solo hizo `UPDATE wf_call_followups SET recording_synced = 1` de filas cuya grabación LeadStudio **confirmó**, y `INSERT` en `wf10_sent_recordings` de esas mismas. Las dos cosas reflejan lo que ya está en el CRM.
- No hubo `DELETE`, `DROP`, cambios de esquema ni backfill.
- Los scripts de preflight y validación solo escribieron sus propios informes: `/root/wf10_preflight_*.txt` y `/root/wf10_validate_*.txt`.
