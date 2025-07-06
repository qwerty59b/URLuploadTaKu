# Telegram Video Download Bot 🤖

Bot de Telegram para descargar y subir videos usando yt-dlp. Soporta múltiples formatos incluyendo M3U8 y maneja archivos grandes mediante división automática.

## Características principales

- 📥 Descarga videos con yt-dlp
- ⬆️ Sube videos directamente a Telegram
- ✂️ Divide automáticamente archivos >1990 MB
- 🔄 Actualización de yt-dlp con `/update`
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

1. Clonar el repositorio:
```bash
git clone https://github.com/tu-usuario/tu-repo.git
cd tu-repo
