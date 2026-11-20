# Ajay Glory — Telegram + Railway

Este paquete conserva el motor original, el panel web y sus funciones de cuentas, estadísticas, experiencia, escuadras y gremio. Telegram usa los mismos `bot_state`, workers y callbacks del motor; no existe un segundo motor paralelo.

## Variables de entorno

Copia `.env.example` como referencia y configura los valores en Railway (no subas `.env`):

- `TELEGRAM_BOT_TOKEN`: token entregado por `@BotFather`.
- `TELEGRAM_ADMIN_IDS`: IDs numéricos separados por comas. `/id` solo muestra el ID y no otorga permisos.
- `DATA_DIR`: directorio persistente para cuentas, cachés, configuraciones y estados. En Railway usa `/data` con un Volume.
- `PORT`: lo asigna Railway; el panel escucha en `0.0.0.0`.

Sin `TELEGRAM_BOT_TOKEN`, el motor y el panel web siguen arrancando, pero Telegram queda desactivado.

## Telegram

1. Abre `@BotFather`, ejecuta `/newbot`, elige nombre y usuario, y guarda el token únicamente como secreto de Railway.
2. Despliega el servicio una vez sin necesidad de conocer tu ID, abre el bot y envía `/id`.
3. Añade ese número a `TELEGRAM_ADMIN_IDS` y reinicia el servicio.
4. Usa `/start`. Los comandos administrativos y todos los botones verifican el ID; los usuarios no autorizados son rechazados.

Comandos: `/start`, `/id`, `/status`, `/iniciar`, `/detener`, `/cuentas`, `/stats`, `/logs`, `/eliminar UID`, `/gremio ID REGION`, `/ayuda`. La eliminación exige confirmación mediante botón.

El panel incluye inicio/parada segura, estadísticas del motor real, cuentas paginadas sin contraseñas/tokens, EXP, configuración del gremio y actividad. Las notificaciones importantes se deduplican durante un intervalo para evitar spam.

## Railway

- Start command: `python app.py` (también está en `railway.json`).
- Añade un Volume montado en `/data` y define `DATA_DIR=/data`.
- Configura las variables de Telegram en **Variables**.
- Mantén el repositorio de GitHub privado y no subas `accounts.json`, `token_cache.json`, `devices.json`, `accounts_archive.json`, `guild_config.json`, `banned_accounts.json`, `.env` ni logs.
- Railway puede reiniciar el servicio automáticamente con la política incluida.

## Actualizar GitHub

```bash
git clone <URL_DEL_REPOSITORIO_PRIVADO>
# copia el contenido de este ZIP dentro del repositorio
# revisa .gitignore y confirma que no hay secretos
git add .
git commit -m "Integrate private Telegram control and Railway persistence"
git push origin main
```

Después conecta el repositorio en Railway o pulsa **Redeploy**. Los datos importantes deben estar en el Volume, no en el contenedor efímero.

## Verificaciones realizadas

Se verificó la compilación sintáctica con `python3 -m compileall`. No se afirma una conexión real a Telegram, una autenticación de juego ni una partida real porque requieren un token de BotFather, IDs autorizados, cuentas y servicios externos configurados por el propietario.
