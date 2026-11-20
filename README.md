# Ajay Glory — paquete de despliegue para Railway

Este paquete contiene el código del panel/servicio Python preparado para escuchar en el puerto `PORT` que Railway asigna.

## Estado actual
- El servicio se inicia con `python app.py`.
- El panel web usa `0.0.0.0` y el puerto de Railway (`PORT`).
- **Telegram todavía no está integrado**. La conexión del bot de Telegram se hará en una siguiente etapa.
- No se incluyen cuentas, contraseñas, tokens, cachés, APK, logs ni entornos virtuales del ZIP original.

## Publicación en Railway
1. Sube el contenido de esta carpeta a un repositorio **privado** de GitHub.
2. En Railway: New Project → Deploy from GitHub Repo → selecciona el repositorio.
3. Railway detectará `requirements.txt` y `railway.json`.
4. El comando de inicio debe ser `python app.py`.
5. Para conservar archivos que el programa genere (cuentas, caché y configuración), configura un Volume de Railway y monta su ruta en el directorio de trabajo del servicio. Sin almacenamiento persistente, los archivos locales pueden perderse en nuevos despliegues.

## Datos privados
No subas `accounts.json`, `token_cache.json`, `devices.json`, `accounts_archive.json`, `guild_config.json`, `.env` ni registros con credenciales a GitHub. Introduce cualquier secreto solo en el entorno privado del servicio cuando sea necesario.

## Importante
El archivo original contiene datos de acceso a cuentas de juego. Este paquete los excluye deliberadamente. Revisa y rota cualquier contraseña/token que ya se haya compartido públicamente.
