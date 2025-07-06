import os
import subprocess
import asyncio
import time
import re
import logging
import mimetypes
from kunigram import Client, filters
from kunigram.types import Message
from split_upload import split_and_upload

# Configuración
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH']
BOT_TOKEN = os.environ['BOT_TOKEN']
OWNER_ID = int(os.environ['OWNER_ID'])
MAX_DIRECT_SIZE = 1990 * 1024 * 1024  # 1990 MB

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='bot.log'
)
logger = logging.getLogger(__name__)

app = Client(
    "download_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def is_owner(user_id):
    return user_id == OWNER_ID

class DownloadProgress:
    def __init__(self, message):
        self.message = message
        self.last_update = 0
        self.progress_text = ""
        self.start_time = time.time()

    async def update(self, text):
        current_time = time.time()
        # Actualizar solo si han pasado más de 20 segundos
        if current_time - self.last_update > 20:
            self.last_update = current_time
            if text != self.progress_text:
                self.progress_text = text
                try:
                    await self.message.edit(text)
                except Exception as e:
                    logger.error(f"Error al actualizar progreso: {str(e)}")

async def download_content(url, custom_filename=None, progress_callback=None):
    """Descarga contenido usando wget o yt-dlp según el tipo de enlace"""
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    
    # Determinar si es un enlace directo a archivo
    is_direct = any(url.lower().endswith(ext) for ext in [
        '.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', 
        '.mp3', '.wav', '.ogg', '.m4a',
        '.zip', '.rar', '.7z', '.tar', '.gz',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx',
        '.jpg', '.jpeg', '.png', '.gif', '.bmp'
    ])
    
    if is_direct:
        # Descargar con wget
        return await download_with_wget(url, download_path, custom_filename, progress_callback)
    else:
        # Descargar con yt-dlp
        return await download_with_ytdlp(url, download_path, custom_filename, progress_callback)

async def download_with_wget(url, download_path, custom_filename, progress_callback):
    """Descarga contenido usando wget para enlaces directos"""
    try:
        filename = custom_filename if custom_filename else os.path.basename(url)
        output_path = os.path.join(download_path, filename)
        
        cmd = [
            "wget",
            "-O", output_path,
            "--progress=dot:giga",
            "--no-check-certificate",
            url
        ]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        # Procesar salida para obtener progreso
        total_size = 0
        downloaded = 0
        
        for line in process.stdout:
            if progress_callback:
                # Buscar tamaño total
                if "Length:" in line and "[" not in line:
                    size_match = re.search(r'Length: (\d+)', line)
                    if size_match:
                        total_size = int(size_match.group(1))
                
                # Buscar progreso actual
                match = re.search(r'(\d+)%', line)
                if match:
                    percent = match.group(1)
                    downloaded = total_size * int(percent) / 100
                    await progress_callback(f"⏬ Descargando... {percent}%")
                elif "saved" in line:
                    await progress_callback("✅ Descarga completada")
        
        process.wait()
        if process.returncode != 0:
            return None
        
        return output_path
        
    except Exception as e:
        logger.error(f"Error en descarga wget: {e}")
        return None

async def download_with_ytdlp(url, download_path, custom_filename, progress_callback):
    """Descarga contenido usando yt-dlp"""
    try:
        cmd = [
            "yt-dlp",
            "-o", f"{download_path}/%(title)s.%(ext)s",
            "--no-playlist",
            url
        ]
        
        # Ejecutar yt-dlp capturando salida
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        # Procesar salida para obtener progreso
        for line in process.stdout:
            if progress_callback:
                if "ETA" in line and "]" in line:
                    # Extraer porcentaje de progreso
                    match = re.search(r'(\d+\.\d+)%', line)
                    if match:
                        percent = match.group(1)
                        await progress_callback(f"⏬ Descargando... {percent}%")
        
        process.wait()
        if process.returncode != 0:
            return None
        
        # Buscar el archivo descargado
        files = os.listdir(download_path)
        if not files:
            return None
            
        original_file = os.path.join(download_path, files[0])
        
        # Renombrar si se especifica
        if custom_filename:
            new_file = os.path.join(download_path, custom_filename)
            os.rename(original_file, new_file)
            return new_file
        return original_file
        
    except Exception as e:
        logger.error(f"Error en descarga yt-dlp: {e}")
        return None

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply(
        "🤖 **Bot de Descargas Avanzado**\n\n"
        "Envía un enlace para subir el archivo a Telegram\n\n"
        "Soporte para:\n"
        "- Videos (MP4, MKV, AVI, etc.)\n"
        "- Streams (M3U8, YouTube, etc.)\n"
        "- Archivos directos (ZIP, PDF, imágenes, etc.)\n\n"
        "Para renombrar: `http://ejemplo.com/archivo.mp4 | mi_archivo.mp4`\n\n"
        "Archivos >1990MB se dividirán automáticamente"
    )

@app.on_message(filters.command("update") & filters.private)
async def update_bot(client: Client, message: Message):
    """Actualiza herramientas y reinicia el bot (solo owner)"""
    if not is_owner(message.from_user.id):
        await message.reply("❌ Solo el propietario puede usar este comando")
        return
        
    msg = await message.reply("🔄 Actualizando herramientas...")
    log_file = "/tmp/update_error.log"
    
    try:
        # Actualizar yt-dlp y wget
        update_cmd = [
            "pip", "install", "--upgrade", 
            "yt-dlp[default,curl-cffi]", 
            "wget"
        ]
        result = subprocess.run(
            update_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"Error al actualizar: {result.stdout}")
        
        # Obtener versiones
        version_cmd = ["yt-dlp", "--version"]
        version_result = subprocess.run(version_cmd, stdout=subprocess.PIPE, text=True)
        ytdlp_version = version_result.stdout.strip()
        
        wget_cmd = ["wget", "--version"]
        wget_result = subprocess.run(wget_cmd, stdout=subprocess.PIPE, text=True)
        wget_version = wget_result.stdout.split('\n')[0]
        
        await msg.edit(
            f"✅ Herramientas actualizadas:\n"
            f"- yt-dlp: {ytdlp_version}\n"
            f"- wget: {wget_version}\n\n"
            "Reiniciando bot..."
        )
        await asyncio.sleep(3)
        os._exit(0)
        
    except Exception as e:
        # Guardar log de error
        with open(log_file, "w") as f:
            f.write(str(e))
        
        await client.send_document(
            chat_id=message.chat.id,
            document=log_file,
            caption="❌ Error al actualizar herramientas"
        )
        await msg.edit("⚠️ Actualización fallida. Ver log para detalles.")

@app.on_message(filters.text & ~filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    user_input = message.text
    parts = user_input.split(" | ", 1)
    url = parts[0].strip()
    custom_name = parts[1].strip() if len(parts) > 1 else None
    
    if not url.startswith(("http://", "https://")):
        return
    
    msg = await message.reply("⏬ Iniciando descarga...")
    progress = DownloadProgress(msg)
    
    # Descargar contenido
    file_path = await download_content(
        url, 
        custom_name,
        progress.update
    )
    
    if not file_path or not os.path.exists(file_path):
        await msg.edit("❌ Error al descargar el contenido")
        return
    
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    
    # Determinar si se necesita dividir
    if file_size > MAX_DIRECT_SIZE:
        await msg.edit(f"📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
        await split_and_upload(client, message, msg, file_path)
    else:
        await msg.edit(f"✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo a Telegram...")
        try:
            # Detectar tipo MIME para enviar como video si es posible
            mime_type, _ = mimetypes.guess_type(file_path)
            is_video = mime_type and mime_type.startswith('video/')
            
            if is_video:
                await client.send_video(
                    chat_id=message.chat.id,
                    video=file_path,
                    progress=upload_progress_callback,
                    progress_args=(msg,)
                )
            else:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    progress=upload_progress_callback,
                    progress_args=(msg,)
                )
            await msg.edit("✅ Subida completada")
        except Exception as e:
            await msg.edit(f"❌ Error en subida: {str(e)}")
    # Limpieza
    if os.path.exists(file_path):
        os.remove(file_path)

async def upload_progress_callback(current, total, msg):
    """Muestra progreso de subida cada 20 segundos"""
    current_time = time.time()
    if not hasattr(upload_progress_callback, 'last_update'):
        upload_progress_callback.last_update = 0
    
    # Actualizar solo si han pasado más de 20 segundos
    if current_time - upload_progress_callback.last_update > 20:
        upload_progress_callback.last_update = current_time
        percent = current * 100 / total
        try:
            await msg.edit(f"⬆️ Subiendo... {percent:.1f}%")
        except Exception:
            pass  # Ignorar errores de actualización

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado ⚡")
    app.run()
