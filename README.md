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

Comandos: `/start`, `/id`, `/status`, `/iniciar`, `/detener`, `/cuentas`, `/stats`, `/logs`, `/generar CANTIDAD HILOS ID_CLAN REGION`, `/eliminar UID`, `/gremio ID REGION`, `/ayuda`. La eliminación exige confirmación mediante botón.

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

## Generar cuentas, unir al clan y jugar

El botón **GENERAR + CLAN + JUGAR** muestra el formato del comando. Ejemplo: `/generar 4 2 2068400598 NA`. El flujo reutiliza `gen.py` y `dashboard_server._run_automation`: genera las cuentas, consulta la información del clan, envía las solicitudes de unión y finalmente llama al callback original `on_glory_start`. El botón **PROGRESO** muestra la etapa, cuentas creadas, unidas y fallidas.

## Exportar e importar cuentas desde Telegram

En el panel usa **📤 EXPORTAR CUENTAS** para recibir un respaldo JSON privado. También puedes usar `/exportar`. El archivo incluye las cuentas persistentes, caché de tokens, dispositivos, configuración del clan, cuentas bloqueadas y estado visible. Los tokens y contraseñas son secretos: no compartas el archivo ni lo subas a GitHub.

Para restaurar, pulsa **📥 IMPORTAR CUENTAS**, adjunta el JSON exportado o usa `/importar` y después adjunta el archivo. La importación fusiona por UID/token, no borra las cuentas existentes y conserva el orden del respaldo. Si Glory está activo, las cuentas importadas se reinician con el motor original y vuelven a organizarse en escuadras automáticamente.

## Seguimiento separado de puntos del clan

- `/meta_clan 4500` establece la meta que se muestra para cada cuenta (se guarda en `clan_points.json`).
- `/puntos` muestra el registro individual.
- `/puntos UID CANTIDAD` registra/actualiza manualmente los puntos del clan de esa cuenta.
- Los puntos del clan son independientes de la EXP y del nivel de cuenta. Este registro es manual; no consulta automáticamente los puntos del juego ni desconecta cuentas al alcanzar la meta.

## Importación desde archivos KING BOT

El importador acepta una lista JSON de objetos con `uid` y `password` (como el `accounts.json` de KING BOT), además del respaldo completo de Ajay. Fusiona por UID, conserva las cuentas existentes, evita duplicados, respeta la región guardada en cada cuenta y agrega los registros al pool persistente. Si Glory está activo, solicita al callback del motor que incorpore cada cuenta importada. La exportación entrega un JSON descargable desde Telegram e incluye el registro de puntos del clan.

Nota: este paquete mantiene el motor y las regiones originales de Ajay. No se sustituyó el generador de cuentas por el generador de KING BOT ni se añadió automatización de creación masiva o de partidas para acumular puntos. La gestión/importación/exportación y el registro de progreso quedan separados de ese proceso.

## Renovación de tokens desde Telegram

En el panel hay una acción **RENOVAR TOKENS VENCIDOS** y cada cuenta tiene su propio botón **Renovar UID** en la lista de cuentas. La renovación funciona cuando la cuenta conserva UID y contraseña de invitado; el motor vuelve a autenticarse, guarda las credenciales nuevas y, si Glory está activo, reinicia el worker de esa cuenta. Las cuentas importadas únicamente con un access token no pueden recuperar un token ya vencido a partir del mismo token: se debe importar un token vigente o las credenciales UID/contraseña. La renovación masiva procesa las cuentas una por una y muestra los resultados.
