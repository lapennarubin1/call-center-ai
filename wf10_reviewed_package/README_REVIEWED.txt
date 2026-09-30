WF10 REVIEWED DEPLOY PACKAGE — 2026-09-29

Se revisó el paquete de Claude y se añadió compatibilidad con el wf10-worker.py real de producción:
- timeout webhook: 45s -> 120s
- match del worker por UNIQUEID + ventana -120/+900, igual que WF10
- el worker entiende respuestas terminales del WF10 nuevo: attached, attached_already_synced, not_pending, invalid_audio, put_rejected
- ante 503 sigue reintentando

No hace backfill histórico.
No toca WF2/WF9/wf_call_events/lifecycle.
No retira el cron viejo durante apply.

Uso:
  bash DEPLOY_WF10_REVIEWED.sh check
  bash DEPLOY_WF10_REVIEWED.sh apply
  # dejar llamadas nuevas 10-15 min
  bash DEPLOY_WF10_REVIEWED.sh validate 30
  # sólo después de validar bien:
  bash DEPLOY_WF10_REVIEWED.sh finalize 60

Rollback:
  bash DEPLOY_WF10_REVIEWED.sh rollback
