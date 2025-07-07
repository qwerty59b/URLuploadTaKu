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
from collections import defaultdict
from pyrogram import Client, filters
from pyrogram.types import Message
from split_upload import split_and_upload
from datetime import datetime
from urllib.parse import urlparse, unquote, parse_qs

# Configuración
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
MAX_DIRECT_SIZE = 1990 * 1024 * 1024  # 1990 MB
MAX_CONCURRENT_TASKS = 5  # Máximo de tareas simultáneas

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

app = Client(
    "download_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Sistema de colas global
task_queue = asyncio.Queue()
active_tasks = {}
queued_tasks = {}
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
progress_last_update = {}
user_active_tasks = {}  # Inicialización añadida

# Lista de dominios compatibles con yt-dlp (ejemplos principales)
YTDLP_DOMAINS = [
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", 
    "twitter.com", "tiktok.com", "twitch.tv", "vimeo.com",
    "dailymotion.com", "bilibili.com", "nicovideo.jp", "soundcloud.com"
]

def is_owner(user_id):
    return user_id == OWNER_ID

def requires_ytdlp(url):
    """Determina si la URL requiere yt-dlp para descargar"""
    # Verificar palabras clave en la URL
    url_lower = url.lower()
    if any(key in url_lower for key in ["m3u8", "hls", "mpd", "dash"]):
        return True
    
    # Verificar dominios compatibles
    try:
        domain = urlparse(url).netloc.lower()
        return any(d in domain for d in YTDLP_DOMAINS)
    except:
        return False

def get_filename_from_url(url):
    """Extrae el nombre de archivo desde la URL con manejo de errores"""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        
        # Buscar nombre de archivo en la ruta
        if "/" in path:
            filename = path.split("/")[-1]
            if "." in filename and len(filename) > 4:
                return filename
        
        # Buscar en parámetros de consulta
        query = parse_qs(parsed.query)
        for key in ["filename", "name", "file"]:
            if key in query:
                value = query[key][0]
                if "." in value:
                    return value
    except Exception as e:
        logger.error(f"Error extrayendo nombre: {str(e)}")
    
    # Nombres por defecto basados en tipo de contenido
    if "video" in url:
        return "video.mp4"
    elif "audio" in url:
        return "audio.mp3"
    elif "image" in url:
        return "image.jpg"
    
    return f"archivo_{int(time.time())}.bin"

async def progress_bar(
    current: int, 
    total: int, 
    status_msg: str, 
    progress_message: Message, 
    filename: str, 
    task_id: str, 
    start_time: float
):
    """Muestra una barra de progreso similar a main.py"""
    present = time.time()
    key = f"{task_id}_{status_msg}"
    
    # Actualizar solo si han pasado >5 segundos o es la última actualización
    if key not in progress_last_update:
        progress_last_update[key] = 0
        
    if present - progress_last_update[key] > 15 or current == total:
        progress_last_update[key] = present
        
        try:
            speed = current / (present - start_time) if present - start_time > 0 else 0
            percentage = current * 100 / total if total > 0 else 0
            time_to_complete = round(((total - current) / speed)) if speed > 0 else 0
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
                f"**⚡ Speed**: {humanize.naturalsize(speed)}/s\n"
                f"**📚 Done**: {humanize.naturalsize(current)}\n"
                f"**💾 Size**: {humanize.naturalsize(total)}\n"
                f"**⏰ Time Left**: {time_to_complete_str}"
            )

            await progress_message.edit(current_message)
        except Exception as e:
            logger.error(f"Error updating progress: {str(e)}")

async def download_with_aiohttp(url, filepath, progress_callback, task_id, filename, start_time):
    """Descarga usando aiohttp (similar a main.py)"""
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
                            "📥 Downloading",
                            progress_callback.progress_message,
                            filename,
                            task_id,
                            start_time
                        )
        return True
    except Exception as e:
        logger.error(f"aiohttp download error: {str(e)}")
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
        
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        active_tasks[task_id]['process'] = process
        progress_pattern = re.compile(
            r'\[download\]\s+(\d+\.\d+)%.*?(\d+\.\d+)([KM]iB)/s.*?ETA\s+(\d+:\d+)'
        )
        
        while True:
            if task_id not in active_tasks:
                process.terminate()
                return False
                
            line = process.stdout.readline()
            if not line:
                break
                
            match = progress_pattern.search(line)
            if match:
                percent = float(match.group(1))
                downloaded = (percent / 100) * progress_callback.total_size
                await progress_callback(
                    int(downloaded),
                    progress_callback.total_size,
                    "📥 Downloading",
                    progress_callback.progress_message,
                    filename,
                    task_id,
                    start_time
                )
        
        process.wait()
        return process.returncode == 0
    except Exception as e:
        logger.error(f"yt-dlp error: {str(e)}")
        return False

async def download_content(url, filepath, progress_callback, task_id, filename, start_time):
    """Elige el método de descarga basado en el tipo de URL"""
    if requires_ytdlp(url):
        logger.info(f"Usando yt-dlp para URL especial: {url}")
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
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        file_path
    ]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        metadata = json.loads(result.stdout)
        
        # Buscar el stream de video
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
        logger.error(f"Error al obtener metadatos: {str(e)}")
        return None

def generate_thumbnail(video_path, task_id):
    """Genera una miniatura para el video"""
    try:
        thumb_path = f"/tmp/thumb_{task_id}.jpg"
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-ss', '00:00:05',
            '-vframes', '1',
            '-q:v', '2',
            thumb_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return thumb_path
    except Exception as e:
        logger.error(f"Error generando miniatura: {e}")
        return None

async def task_processor():
    """Procesa tareas desde la cola global"""
    while True:
        # Obtener la siguiente tarea de la cola
        task_data = await task_queue.get()
        task_id = task_data['task_id']
        
        try:
            async with task_semaphore:
                # Actualizar estado a activo
                active_tasks[task_id] = {
                    'start_time': time.time(),
                    'progress_message': task_data['msg'],
                    'process': None
                }
                
                # Eliminar de tareas en espera
                if task_id in queued_tasks:
                    del queued_tasks[task_id]
                
                # Iniciar proceso de descarga
                await process_download(
                    task_data['url'],
                    task_data['custom_name'],
                    task_data['message'],
                    task_data['msg'],
                    task_id
                )
        except Exception as e:
            logger.error(f"Error procesando tarea {task_id}: {str(e)}")
            await task_data['msg'].edit(f"[{task_id}] ❌ Error en procesamiento: {str(e)}")
        finally:
            # Limpiar tarea completada
            if task_id in active_tasks:
                del active_tasks[task_id]
            task_queue.task_done()

async def process_download(url, custom_name, message, msg, task_id):
    """Procesa la descarga y subida del archivo"""
    file_path = None
    try:
        await msg.edit(f"[{task_id}] ⏬ Iniciando descarga...")
        start_time = time.time()
        
        # Configurar callback de progreso
        async def progress_callback(current, total, status, progress_msg, name, tid, stime):
            await progress_bar(current, total, status, progress_msg, name, tid, stime)
        
        # Adjuntar datos necesarios al callback
        progress_callback.progress_message = msg
        progress_callback.total_size = 0  # Se actualizará después
        
        # Obtener nombre de archivo mejorado
        original_filename = get_filename_from_url(url)
        filename = custom_name or original_filename
        
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
            await msg.edit(f"[{task_id}] ❌ Error al descargar el contenido")
            return
        
        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)
        
        # Determinar si se necesita dividir
        if file_size > MAX_DIRECT_SIZE:
            await msg.edit(f"[{task_id}] 📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
            await split_and_upload(app, message, msg, file_path, task_id)
        else:
            await msg.edit(f"[{task_id}] ✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo a Telegram...")
            try:
                # Detectar tipo MIME
                mime_type, _ = mimetypes.guess_type(file_path)
                is_video = mime_type and mime_type.startswith('video/')
                
                if is_video:
                    # Obtener metadatos del video
                    metadata = get_video_metadata(file_path)
                    duration = 0
                    thumb = None
                    
                    if metadata:
                        duration = int(metadata['duration'])
                        size_mb = metadata['size'] / (1024 * 1024)
                        caption = (
                            f"📹 {os.path.basename(file_path)}\n"
                            f"💾 {size_mb:.2f} MB\n"
                            f"🖥️ {metadata['resolution']}\n"
                            f"⏱️ {duration} seg"
                        )
                        
                        # Generar miniatura
                        thumb = generate_thumbnail(file_path, task_id)
                    else:
                        caption = f"📹 {os.path.basename(file_path)}"
                    
                    # Función de progreso para subida
                    async def upload_callback(current, total):
                        await progress_bar(
                            current, 
                            total,
                            "📤 Uploading",
                            msg,
                            filename,
                            task_id,
                            start_time
                        )
                    
                    await app.send_video(
                        chat_id=message.chat.id,
                        video=file_path,
                        caption=caption,
                        duration=duration,
                        thumb=thumb,
                        progress=upload_callback
                    )
                    
                    # Eliminar miniatura temporal
                    if thumb and os.path.exists(thumb):
                        os.remove(thumb)
                else:
                    async def upload_callback(current, total):
                        await progress_bar(
                            current, 
                            total,
                            "📤 Uploading",
                            msg,
                            filename,
                            task_id,
                            start_time
                        )
                    
                    await app.send_document(
                        chat_id=message.chat.id,
                        document=file_path,
                        progress=upload_callback
                    )
                await msg.edit(f"[{task_id}] ✅ Subida completada")
            except Exception as e:
                await msg.edit(f"[{task_id}] ❌ Error en subida: {str(e)}")
    except Exception as e:
        await msg.edit(f"[{task_id}] ❌ Error en proceso: {str(e)}")
    finally:
        # Limpieza final
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error deleting file: {str(e)}")
        
        # Limpiar registro de progreso
        for key in list(progress_last_update.keys()):
            if key.startswith(task_id):
                del progress_last_update[key]

# ... (resto del código: comandos /start, /queue, /update, etc. sin cambios)

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply(
        "🤖 **Bot de Descargas Avanzado**\n\n"
        "Envía un enlace directo a un archivo o video de YouTube/Facebook/Instagram\n\n"
        "También puedes agregar un nombre personalizado después del enlace:\n"
        "`https://ejemplo.com/video.mp4 | Mi Video Personalizado.mp4`\n\n"
        "Comandos disponibles:\n"
        "/start - Muestra este mensaje\n"
        "/update - Actualiza herramientas (solo propietario)\n\n"
        "⚠️ Solo puedes tener 1 tarea activa a la vez"
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
        # Actualizar solo yt-dlp
        update_cmd = [
            "pip", "install", "--upgrade", 
            "yt-dlp"
        ]
        result = subprocess.run(
            update_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"Error al actualizar: {result.stdout}")
        
        # Obtener versión de yt-dlp
        version_cmd = ["yt-dlp", "--version"]
        version_result = subprocess.run(version_cmd, stdout=subprocess.PIPE, text=True)
        ytdlp_version = version_result.stdout.strip()
        
        await msg.edit(
            f"✅ Herramientas actualizadas:\n"
            f"- yt-dlp: {ytdlp_version}\n\n"
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

# Filtro para manejar enlaces
@app.on_message(filters.text | filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    if message.text.startswith('/'):
        return
    
    user_id = message.from_user.id
    if user_id in user_active_tasks:
        task_id_actual = user_active_tasks[user_id]
        if task_id_actual in active_tasks or task_id_actual in queued_tasks:
            await message.reply(
                "⚠️ Ya tienes una tarea en curso o en cola.\n"
                f"ID de tarea actual: `{task_id_actual}`\n\n"
                "Por favor espera a que finalice."
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
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path)
    original_filename = os.path.basename(path) or "file"
    filename = custom_name or original_filename
    
    msg = await message.reply(f"[{task_id}] ⏳ Tarea añadida a la cola. Posición: {task_queue.qsize()+1}")
    
    # Crear datos de tarea
    task_data = {
        'url': url,
        'custom_name': custom_name,
        'message': message,
        'msg': msg,
        'task_id': task_id,
        'user_id': user_id
    }
    
    # Añadir a la cola
    await task_queue.put(task_data)
    queued_tasks[task_id] = task_data

    
    # Configurar callback de progreso
    async def progress_callback(current, total, status, progress_msg, name, tid, stime):
        await progress_bar(current, total, status, progress_msg, name, tid, stime)
    
    # Adjuntar datos necesarios al callback
    progress_callback.progress_message = msg
    progress_callback.total_size = 0  # Se actualizará después
    
    # Crear ruta de descarga
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    file_path = os.path.join(download_path, filename)
    
    # Registrar tarea activa
    active_tasks[task_id] = {
        'progress': progress_callback,
        'start_time': start_time
    }
    
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
        await msg.edit(f"[{task_id}] ❌ Error al descargar el contenido")
        # Limpiar tarea fallida
        if task_id in active_tasks:
            del active_tasks[task_id]
        if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
            del user_active_tasks[user_id]
        return
    
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    
    # Determinar si se necesita dividir
    if file_size > MAX_DIRECT_SIZE:
        await msg.edit(f"[{task_id}] 📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
        await split_and_upload(client, message, msg, file_path, task_id)
    else:
        await msg.edit(f"[{task_id}] ✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo a Telegram...")
        try:
            # Detectar tipo MIME
            mime_type, _ = mimetypes.guess_type(file_path)
            is_video = mime_type and mime_type.startswith('video/')
            
            if is_video:
                # Obtener metadatos del video
                metadata = get_video_metadata(file_path)
                duration = 0
                thumb = None
                
                if metadata:
                    duration = int(metadata['duration'])
                    size_mb = metadata['size'] / (1024 * 1024)
                    caption = (
                        f"📹 {os.path.basename(file_path)}\n"
                        f"💾 {size_mb:.2f} MB\n"
                        f"🖥️ {metadata['resolution']}\n"
                        f"⏱️ {duration} seg"
                    )
                    
                    # Generar miniatura
                    thumb = generate_thumbnail(file_path, task_id)
                else:
                    caption = f"📹 {os.path.basename(file_path)}"
                
                # Función de progreso para subida
                async def upload_callback(current, total):
                    await progress_bar(
                        current, 
                        total,
                        "📤 Uploading",
                        msg,
                        filename,
                        task_id,
                        start_time
                    )
                
                await client.send_video(
                    chat_id=message.chat.id,
                    video=file_path,
                    caption=caption,
                    duration=duration,
                    thumb=thumb,
                    progress=upload_callback
                )
                
                # Eliminar miniatura temporal
                if thumb and os.path.exists(thumb):
                    os.remove(thumb)
            else:
                async def upload_callback(current, total):
                    await progress_bar(
                        current, 
                        total,
                        "📤 Uploading",
                        msg,
                        filename,
                        task_id,
                        start_time
                    )
                
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    progress=upload_callback
                )
            await msg.edit(f"[{task_id}] ✅ Subida completada")
        except Exception as e:
            await msg.edit(f"[{task_id}] ❌ Error en subida: {str(e)}")
    
    # Limpieza final
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Error deleting file: {str(e)}")
    
    # Eliminar tarea de seguimiento
    if task_id in active_tasks:
        del active_tasks[task_id]
    if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
        del user_active_tasks[user_id]
    
    # Limpiar registro de progreso
    for key in list(progress_last_update.keys()):
        if key.startswith(task_id):
            del progress_last_update[key]

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado con Pyrofork ⚡")
    app.run()
