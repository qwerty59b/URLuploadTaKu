# Telegram File Download Bot 🤖

Bot de Telegram para descargar y subir cualquier tipo de archivo usando wget para enlaces directos y yt-dlp para streams/videos. Maneja archivos grandes mediante división automática con 7z sin compresión.

## Características principales

- 📥 Descarga archivos directos con **wget**
- 📹 Descarga videos/streams con **yt-dlp**
- ⬆️ Sube archivos directamente a Telegram
- ✂️ Divide automáticamente archivos >1990 MB usando **7z sin compresión**
- 🔄 Actualización de herramientas con `/update`
- 🏷️ Renombrado personalizado: `url | nombre_personalizado.ext`
- ⏱️ Progreso de descarga/subida con límites de Telegram
- 🔒 Comandos protegidos para el propietario

## Requisitos

- Python 3.10+
- Docker (opcional)
- Cuenta de Telegram con API ID/HASH

## Variables de entorno

| Variable    | Descripción                     | Ejemplo               |
|-------------|---------------------------------|-----------------------|
| `API_ID`    | ID de la API de Telegram        | `1234567`            |
| `API_HASH`  | Hash de la API de Telegram      | `abcdef12345`        |
| `BOT_TOKEN` | Token del bot de Telegram       | `123456:ABC-DEF1234` |
| `OWNER_ID`  | ID del propietario del bot      | `123456789`          |

## Despliegue con Docker

```bash
git clone https://github.com/tu-usuario/tu-repo.git
cd tu-repo

# Crear .env con tus credenciales
echo "API_ID=123456" > .env
echo "API_HASH=abcdef123456" >> .env
echo "BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" >> .env
echo "OWNER_ID=123456789" >> .env

docker build -t file-bot .
docker run -d --name bot --env-file .env file-bot
