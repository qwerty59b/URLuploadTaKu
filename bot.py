import os
import subprocess
import asyncio
import time
import re
import logging
import mimetypes
import uuid
import json
import math
import humanize
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from split_upload import split_and_upload
from urllib.parse import urlparse, unquote, parse_qs

# Configuración
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
MAX_DIRECT_SIZE = 1990 * 1024 * 1024  # 1990 MB

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suprimir advertencias específicas de FloodWait
logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)

app = Client(
    "download_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Sistema de seguimiento de tareas
active_tasks = {}
progress_last_update = {}
user_active_tasks = {}

# Lista de dominios compatibles con yt-dlp
YTDLP_DOMAINS = [
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", 
    "twitter.com", "tiktok.com", "twitch.tv", "vimeo.com",
    "dailymotion.com", "bilibili.com", "nicovideo.jp", "soundcloud.com"
]

def is_owner(user_id):
    return user_id == OWNER_ID

def requires_ytdlp(url):
    """Determina si la URL requiere yt-dlp para descargar"""
    url_lower = url.lower()
    if any(key in url_lower for key in ["m3u8", "hls", "mpd", "dash"]):
        return True
    
    try:
        domain = urlparse(url).netloc.lower()
        return any(d in domain for d in YTDLP_DOMAINS)
    except:
        return False

def get_filename_from_url(url):
    """Extrae el nombre de archivo desde la URL"""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        
        if "/" in path:
            filename = path.split("/")[-1]
            if "." in filename and len(filename) > 4:
                return filename
        
        query = parse_qs(parsed.query)
        for key in ["filename", "name", "file"]:
            if key in query:
                value = query[key][0]
                if "." in value:
                    return value
    except Exception as e:
        logger.error(f"Error extrayendo nombre: {str(e)}")
    
    # Nombres por defecto
    if "video" in url:
        return "video.mp4"
    elif "audio" in url:
        return "audio.mp3"
    elif "image" in url:
        return "image.jpg"
    
    return f"archivo_{int(time.time())}.bin"

async def safe_edit_message(message: Message, text: str):
    """Edita un mensaje de forma segura manejando FloodWait"""
    try:
        await message.edit(text)
    except FloodWait as e:
        logger.warning(f"FloodWait: Esperando {e.value} segundos")
        await asyncio.sleep(e.value)
        await message.edit(text)
    except Exception as e:
        logger.error(f"Error editando mensaje: {str(e)}")

async def progress_bar(
    current: int, 
    total: int, 
    status_msg: str, 
    progress_message: Message, 
    filename: str, 
    task_id: str, 
    start_time: float
):
    """Muestra una barra de progreso con manejo de FloodWait"""
    present = time.time()
    key = f"{task_id}_{status_msg}"
    
    # Actualizar máximo cada 20 segundos o al completar
    last_update = progress_last_update.get(key, 0)
    if present - last_update < 20 and current != total:
        return
        
    progress_last_update[key] = present
    
    try:
        elapsed = present - start_time
        speed = current / elapsed if elapsed > 0 else 0
        percentage = current * 100 / total if total > 0 else 0
        time_to_complete = round((total - current) / speed) if speed > 0 else 0
        time_to_complete_str = humanize.naturaldelta(time_to_complete)

        progressbar = "[{0}{1}]".format(
            "".join(["🟢" for _ in range(math.floor(percentage / 10))]),
            "".join(["⚫" for _ in range(10 - math.floor(percentage / 10))]),
        )

        current_message = (
            f"[{task_id}] **{status_msg}**\n"
            f"**{filename}**\n"
            f"📊 {round(percentage, 2)}%\n"
            f"{progressbar}\n"
            f"**⚡ Velocidad**: {humanize.naturalsize(speed)}/s\n"
            f"**📚 Progreso**: {humanize.naturalsize(current)}\n"
            f"**💾 Tamaño**: {humanize.naturalsize(total)}\n"
            f"**⏰ Tiempo restante**: {time_to_complete_str}"
        )

        await safe_edit_message(progress_message, current_message)
    except Exception as e:
        logger.error(f"Error actualizando progreso: {str(e)}")

async def download_with_aiohttp(url, filepath, progress_callback, task_id, filename, start_time):
    """Descarga usando aiohttp con manejo de cancelación"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return False
                
                total_size = int(response.headers.get('Content-Length', 0))
                downloaded = 0
                chunk_size = 1024 * 1024  # 1 MB
                
                with open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        if task_id not in active_tasks:
                            return False
                            
                        f.write(chunk)
                        downloaded += len(chunk)
                        await progress_callback(
                            downloaded, 
                            total_size,
                            "📥 Descargando",
                            progress_callback.progress_message,
                            filename,
                            task_id,
                            start_time
                        )
        return True
    except Exception as e:
        logger.error(f"Error aiohttp: {str(e)}")
        return False

async def download_with_ytdlp(url, filepath, progress_callback, task_id, filename, start_time):
    """Descarga usando yt-dlp con progreso unificado"""
    try:
        cmd = [
            "yt-dlp",
            "-o", filepath,
            "--no-playlist",
            "--concurrent-fragments", "5",
            "--newline",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            url
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        active_tasks[task_id]['process'] = process
        progress_pattern = re.compile(
            r'\[download\]\s+(\d+\.\d+)%.*?(\d+\.\d+)([KM]iB)/s.*?ETA\s+(\d+:\d+)'
        )
        
        while True:
            if task_id not in active_tasks:
                process.terminate()
                return False
                
            line = await process.stdout.readline()
            if not line:
                break
                
            line = line.decode().strip()
            match = progress_pattern.search(line)
            if match:
                percent = float(match.group(1))
                downloaded = (percent / 100) * progress_callback.total_size
                await progress_callback(
                    int(downloaded),
                    progress_callback.total_size,
                    "📥 Descargando",
                    progress_callback.progress_message,
                    filename,
                    task_id,
                    start_time
                )
        
        await process.wait()
        return process.returncode == 0
    except Exception as e:
        logger.error(f"Error yt-dlp: {str(e)}")
        return False

async def download_content(url, filepath, progress_callback, task_id, filename, start_time):
    """Elige el método de descarga basado en el tipo de URL"""
    if requires_ytdlp(url):
        logger.info(f"Usando yt-dlp para URL: {url}")
        return await download_with_ytdlp(
            url, 
            filepath,
            progress_callback,
            task_id,
            filename,
            start_time
        )
    else:
        logger.info(f"Descargando directamente: {url}")
        return await download_with_aiohttp(
            url, 
            filepath,
            progress_callback,
            task_id,
            filename,
            start_time
        )

def get_video_metadata(file_path):
    """Obtiene metadatos del video usando ffprobe"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        metadata = json.loads(result.stdout)
        
        video_stream = next((stream for stream in metadata['streams'] if stream['codec_type'] == 'video'), None)
        
        if video_stream:
            width = int(video_stream.get('width', 0))
            height = int(video_stream.get('height', 0))
            duration = float(metadata['format'].get('duration', 0))
            size = int(metadata['format'].get('size', 0))
            return {
                'resolution': f"{width}x{height}",
                'duration': duration,
                'size': size
            }
        return None
    except Exception as e:
        logger.error(f"Error obteniendo metadatos: {str(e)}")
        return None

def generate_thumbnail(video_path, task_id):
    """Genera una miniatura para el video"""
    try:
        thumb_path = f"/tmp/thumb_{task_id}.jpg"
        subprocess.run(
            ['ffmpeg', '-i', video_path, '-ss', '00:00:05', '-vframes', '1', '-q:v', '2', thumb_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return thumb_path
    except Exception as e:
        logger.error(f"Error generando miniatura: {e}")
        return None

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await safe_edit_message(
        message,
        "🤖 **Bot de Descargas Avanzado**\n\n"
        "Envía un enlace directo a un archivo o video\n\n"
        "Puedes agregar un nombre personalizado:\n"
        "`https://ejemplo.com/video.mp4 | Mi Video.mp4`\n\n"
        "Comandos:\n"
        "/start - Muestra este mensaje\n"
        "/update - Actualiza herramientas (propietario)\n\n"
        "⚠️ Solo 1 tarea activa por usuario"
    )

@app.on_message(filters.command("update") & filters.private)
async def update_bot(client: Client, message: Message):
    """Actualiza herramientas y reinicia el bot"""
    if not is_owner(message.from_user.id):
        await message.reply("❌ Solo el propietario puede usar este comando")
        return
        
    msg = await message.reply("🔄 Actualizando herramientas...")
    log_file = "/tmp/update_error.log"
    
    try:
        result = subprocess.run(
            ["pip", "install", "--upgrade", "yt-dlp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"Error actualizando: {result.stdout}")
        
        version_result = subprocess.run(
            ["yt-dlp", "--version"],
            stdout=subprocess.PIPE,
            text=True
        )
        ytdlp_version = version_result.stdout.strip()
        
        await safe_edit_message(
            msg,
            f"✅ Herramientas actualizadas:\n"
            f"- yt-dlp: {ytdlp_version}\n\n"
            "Reiniciando bot..."
        )
        await asyncio.sleep(3)
        os._exit(0)
        
    except Exception as e:
        with open(log_file, "w") as f:
            f.write(str(e))
        
        await client.send_document(
            chat_id=message.chat.id,
            document=log_file,
            caption="❌ Error al actualizar herramientas"
        )
        await safe_edit_message(msg, "⚠️ Actualización fallida. Ver log para detalles.")

@app.on_message(filters.text & ~filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    user_id = message.from_user.id
    
    # Verificar si el usuario tiene una tarea activa
    if user_id in user_active_tasks:
        task_id_actual = user_active_tasks[user_id]
        if task_id_actual in active_tasks:
            await message.reply(
                "⚠️ Ya tienes una tarea en curso.\n"
                f"ID de tarea: `{task_id_actual}`\n\n"
                "Espera a que finalice antes de enviar otra."
            )
            return
    
    user_input = message.text
    parts = user_input.split(" | ", 1)
    url = parts[0].strip()
    custom_name = parts[1].strip() if len(parts) > 1 else None
    
    if not url.startswith(("http://", "https://")):
        return
    
    # Generar ID único para la tarea
    task_id = str(uuid.uuid4())[:8].upper()
    user_active_tasks[user_id] = task_id
    
    # Obtener nombre de archivo
    original_filename = get_filename_from_url(url)
    filename = custom_name or original_filename
    
    msg = await message.reply(f"[{task_id}] ⏬ Iniciando descarga...")
    
    # Registrar tarea activa
    start_time = time.time()
    active_tasks[task_id] = {
        'start_time': start_time,
        'progress_message': msg,
        'process': None
    }
    
    file_path = None
    try:
        # Configurar callback de progreso
        async def progress_callback(current, total, status, progress_msg, name, tid, stime):
            await progress_bar(current, total, status, progress_msg, name, tid, stime)
        
        progress_callback.progress_message = msg
        progress_callback.total_size = 0
        
        # Crear ruta de descarga
        download_path = "/tmp/downloads"
        os.makedirs(download_path, exist_ok=True)
        file_path = os.path.join(download_path, filename)
        
        # Descargar contenido
        success = await download_content(
            url, 
            file_path,
            progress_callback,
            task_id,
            filename,
            start_time
        )
        
        if not success or not os.path.exists(file_path):
            await safe_edit_message(msg, f"[{task_id}] ❌ Error al descargar el contenido")
            return
        
        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)
        
        # Manejar archivos grandes
        if file_size > MAX_DIRECT_SIZE:
            await safe_edit_message(
                msg, 
                f"[{task_id}] 📦 Archivo grande ({size_mb:.2f} MB). Dividiendo..."
            )
            await split_and_upload(client, message, msg, file_path, task_id)
        else:
            await safe_edit_message(
                msg, 
                f"[{task_id}] ✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo..."
            )
            
            # Detectar tipo de archivo
            mime_type, _ = mimetypes.guess_type(file_path)
            is_video = mime_type and mime_type.startswith('video/')
            
            # Función de progreso para subida
            async def upload_callback(current, total):
                await progress_bar(
                    current, 
                    total,
                    "📤 Subiendo",
                    msg,
                    filename,
                    task_id,
                    start_time
                )
            
            if is_video:
                # Procesar video
                metadata = get_video_metadata(file_path)
                duration = int(metadata['duration']) if metadata else 0
                size_mb = metadata['size'] / (1024 * 1024) if metadata else size_mb
                resolution = metadata['resolution'] if metadata else "Desconocida"
                
                caption = (
                    f"📹 {os.path.basename(file_path)}\n"
                    f"💾 {size_mb:.2f} MB\n"
                    f"🖥️ {resolution}\n"
                    f"⏱️ {duration} seg"
                )
                
                thumb = generate_thumbnail(file_path, task_id) if metadata else None
                
                await client.send_video(
                    chat_id=message.chat.id,
                    video=file_path,
                    caption=caption,
                    duration=duration,
                    thumb=thumb,
                    progress=upload_callback
                )
                
                # Limpiar miniatura
                if thumb and os.path.exists(thumb):
                    os.remove(thumb)
            else:
                # Procesar otros tipos de archivos
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    progress=upload_callback
                )
                
            await safe_edit_message(msg, f"[{task_id}] ✅ Subida completada")
    except Exception as e:
        await safe_edit_message(msg, f"[{task_id}] ❌ Error: {str(e)}")
    finally:
        # Limpieza final
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error eliminando archivo: {str(e)}")
        
        # Limpiar registro de tareas
        if task_id in active_tasks:
            del active_tasks[task_id]
        if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
            del user_active_tasks[user_id]
        
        # Limpiar registro de progreso
        for key in list(progress_last_update.keys()):
            if key.startswith(task_id):
                del progress_last_update[key]

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado ⚡")
    app.run()
