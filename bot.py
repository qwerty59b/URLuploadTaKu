import os
import subprocess
import asyncio
import time
import re
import logging
import mimetypes
import uuid
import json
import urllib.parse
from collections import defaultdict
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from split_upload import split_and_upload
from datetime import datetime

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

app = Client(
    "download_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Almacenamiento de tareas y elecciones
active_tasks = {}
task_counters = defaultdict(int)
current_downloads = {}
pending_choices = {}

# ============================== NUEVO: Sistema de colas ==============================
class DownloadQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.active_tasks = {}
        self.queue_status = {}
        self.processing = False

    async def add_task(self, task_data):
        task_id = task_data['task_id']
        self.queue_status[task_id] = "En cola"
        await self.queue.put(task_data)
        return self.queue.qsize()

    async def process_queue(self):
        if self.processing:
            return
            
        self.processing = True
        while not self.queue.empty():
            task_data = await self.queue.get()
            task_id = task_data['task_id']
            
            try:
                self.queue_status[task_id] = "Procesando"
                await handle_download_task(task_data)
                self.queue_status[task_id] = "Completado"
            except Exception as e:
                self.queue_status[task_id] = f"Error: {str(e)}"
                logger.error(f"Error en tarea {task_id}: {e}")
            finally:
                self.queue.task_done()
                if task_id in self.queue_status:
                    del self.queue_status[task_id]
                # Pequeña pausa entre tareas
                await asyncio.sleep(2)
        
        self.processing = False

    def get_queue_size(self):
        return self.queue.qsize()

    def get_queue_status(self):
        return self.queue_status

# Cola global para gestionar todas las tareas
download_queue = DownloadQueue()

# ===================== FIN del nuevo sistema de colas =====================

def is_owner(user_id):
    return user_id == OWNER_ID

class DownloadProgress:
    def __init__(self, message, task_id):
        self.message = message
        self.last_update = 0
        self.progress_text = ""
        self.start_time = time.time()
        self.task_id = task_id
        self.last_percent = 0
        self.last_speed = ""
        self.last_eta = ""

    async def update(self, data):
        current_time = time.time()
        # Actualizar solo si han pasado más de 15 segundos o si hay cambios importantes
        if current_time - self.last_update > 15 or 'force' in data:
            elapsed = current_time - self.start_time
            elapsed_str = self.format_time(elapsed)
            
            # Extraer datos
            percent = data.get('percent', self.last_percent)
            speed = data.get('speed', self.last_speed)
            eta = data.get('eta', self.last_eta)
            
            # Actualizar últimos valores
            self.last_percent = percent
            self.last_speed = speed
            self.last_eta = eta
            
            # Construir texto de progreso
            progress_text = (
                f"[{self.task_id}] ⏬ Descargando\n"
                f"📊 {percent}\n"
                f"⚡ {speed}\n"
                f"⏱️ {elapsed_str} / ETA {eta}"
            )
            
            if progress_text != self.progress_text:
                self.progress_text = progress_text
                self.last_update = current_time
                try:
                    await self.message.edit(progress_text)
                except Exception as e:
                    logger.error(f"Error al actualizar progreso: {str(e)}")
    
    def format_time(self, seconds):
        """Formatea segundos a HH:MM:SS"""
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"

async def download_content(url, custom_filename=None, progress_callback=None, task_id=None, method="yt-dlp"):
    """Descarga contenido usando el método especificado"""
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    
    if method == "aria2":
        return await download_with_aria2(url, custom_filename, progress_callback, task_id)
    else:
        return await download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id)

async def download_with_aria2(url, custom_filename, progress_callback, task_id):
    """Descarga un archivo directo usando aria2 con 16 conexiones"""
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    
    # Determinar nombre de archivo
    if custom_filename:
        output_file = os.path.join(download_path, custom_filename)
    else:
        parsed_url = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = str(uuid.uuid4())
        output_file = os.path.join(download_path, filename)
    
    # Comando aria2c con 16 conexiones
    cmd = [
        "aria2c",
        "-x", "16",
        "-s", "16",
        "-j", "16",
        "-d", download_path,
        "-o", os.path.basename(output_file),
        url
    ]
    
    logger.info(f"Ejecutando aria2c: {' '.join(cmd)}")
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Registrar proceso
        current_downloads[task_id] = process
        
        # Procesar salida para progreso
        pattern = re.compile(r'(\d+)%[^(]*?(\d+\.\d+)([KM])iB/s.*?ETA:([\d:]+)')
        
        while True:
            line = process.stdout.readline()
            if not line:
                break
                
            if progress_callback and task_id in active_tasks:
                match = pattern.search(line)
                if match:
                    percent = match.group(1)
                    speed = f"{match.group(2)} {match.group(3)}iB/s"
                    eta = match.group(4)
                    
                    await progress_callback({
                        'percent': f"{percent}%",
                        'speed': speed,
                        'eta': eta
                    })
        
        process.wait()
        if process.returncode != 0:
            logger.error(f"Error en descarga aria2: {process.returncode}")
            return None
        
        return output_file
    except Exception as e:
        logger.error(f"Error en descarga aria2: {e}")
        return None
    finally:
        if task_id in current_downloads:
            del current_downloads[task_id]

async def download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id):
    """Descarga contenido usando yt-dlp con soporte para HLS/m3u8"""
    try:
        # Configuración especial para enlaces HLS/m3u8
        extra_params = []
        if re.search(r'\.m3u8$|\.mpd$', url, re.IGNORECASE):
            extra_params = [
                '--hls-use-mpegts',
                '--downloader', 'ffmpeg',
                '--downloader-args', 'ffmpeg:-c copy -bsf:a aac_adtstoasc'
            ]
        
        # Formato predeterminado
        cmd = [
            "yt-dlp",
            "-o", f"{download_path}/%(title)s.%(ext)s",
            "--no-playlist",
            "--concurrent-fragments", "5",
            "--hls-prefer-native",
            "--merge-output-format", "mp4",
            "--newline",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            url
        ] + extra_params
        
        logger.info(f"Iniciando descarga yt-dlp: {' '.join(cmd)}")
        
        # Ejecutar yt-dlp
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Registrar proceso
        current_downloads[task_id] = process
        
        # Procesar salida para obtener progreso
        progress_pattern = re.compile(
            r'\[download\]\s+(\d+\.\d+)%.*?(\d+\.\d+)([KM]iB)/s.*?ETA\s+(\d+:\d+)'
        )
        hls_pattern = re.compile(
            r'\[download\]\s+(\d+\.\d+)%.*?(\d+\.\d+)([KM]iB)/s'
        )
        
        last_progress_update = time.time()
        start_time = time.time()
        last_percent = 0
        
        for line in iter(process.stdout.readline, ''):
            if progress_callback and task_id in active_tasks:
                # Intentar extraer datos de progreso
                match = progress_pattern.search(line) or hls_pattern.search(line)
                if match:
                    percent = match.group(1)
                    
                    if len(match.groups()) >= 3:
                        speed_value = match.group(2)
                        speed_unit = match.group(3)
                        speed = f"{speed_value} {speed_unit}/s"
                    else:
                        speed = "calculando..."
                    
                    eta = match.group(4) if len(match.groups()) >= 4 else "calculando..."
                    
                    await progress_callback({
                        'percent': f"{percent}%",
                        'speed': speed,
                        'eta': eta
                    })
                    last_progress_update = time.time()
                    last_percent = float(percent)
                else:
                    # Actualizar solo porcentaje si está disponible
                    match_percent = re.search(r'(\d+\.\d+)%', line)
                    if match_percent:
                        percent = match_percent.group(1)
                        if abs(float(percent) - last_percent) > 5:
                            await progress_callback({
                                'percent': f"{percent}%",
                                'force': True
                            })
                            last_percent = float(percent)
                    
                    # Actualizar si ha pasado mucho tiempo sin progreso
                    elif time.time() - last_progress_update > 30:
                        await progress_callback({
                            'percent': f"{last_percent}%",
                            'speed': "calculando...",
                            'eta': "calculando...",
                            'force': True
                        })
                        last_progress_update = time.time()
        
        process.wait()
        if process.returncode != 0:
            logger.error(f"Error en descarga yt-dlp: {process.returncode}")
            return None
        
        # Buscar el archivo descargado
        files = os.listdir(download_path)
        if not files:
            return None
            
        # Encontrar el archivo más reciente
        files.sort(key=lambda x: os.path.getmtime(os.path.join(download_path, x)), reverse=True)
        original_file = os.path.join(download_path, files[0])
        
        # Renombrar si se especifica
        if custom_filename:
            _, ext = os.path.splitext(original_file)
            if not custom_filename.endswith(ext):
                custom_filename += ext
                
            new_file = os.path.join(download_path, custom_filename)
            os.rename(original_file, new_file)
            return new_file
        return original_file
        
    except Exception as e:
        logger.error(f"Error en descarga yt-dlp: {e}")
        return None
    finally:
        if task_id in current_downloads:
            del current_downloads[task_id]

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

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply(
        "🤖 **Bot de Descargas Avanzado**\n\n"
        "Envía un enlace directo a un archivo o video (YouTube, Facebook, Instagram, HLS/m3u8)\n\n"
        "También puedes agregar un nombre personalizado después del enlace:\n"
        "`https://ejemplo.com/video.mp4 | Mi Video Personalizado.mp4`\n\n"
        "Comandos disponibles:\n"
        "/start - Muestra este mensaje\n"
        "/update - Actualiza herramientas (solo propietario)\n"
        "/queue - Muestra el estado de la cola de descargas\n\n"
        "⚠️ Ahora puedes tener múltiples tareas en cola"
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
        # Actualizar yt-dlp y aria2
        update_cmd = [
            "pip", "install", "--upgrade", 
            "yt-dlp[default,curl-cffi]",
            "aria2p"
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
        
        aria2_version = subprocess.run(["aria2c", "--version"], capture_output=True, text=True).stdout.split('\n')[0]
        
        await msg.edit(
            f"✅ Herramientas actualizadas:\n"
            f"- yt-dlp: {ytdlp_version}\n"
            f"- aria2: {aria2_version}\n\n"
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

# ================== NUEVO: Comando para ver la cola ==================
@app.on_message(filters.command("queue"))
async def show_queue(client: Client, message: Message):
    """Muestra el estado actual de la cola de descargas"""
    queue_status = download_queue.get_queue_status()
    queue_size = download_queue.get_queue_size()
    
    if not queue_status and queue_size == 0:
        await message.reply("✅ La cola de descargas está vacía")
        return
        
    response = "📋 **Estado de la cola de descargas**\n\n"
    
    # Mostrar tareas en cola
    if queue_size > 0:
        response += f"🔄 **Tareas pendientes:** {queue_size}\n"
    
    # Mostrar tareas activas
    if queue_status:
        response += "\n**Tareas en progreso:**\n"
        for task_id, status in queue_status.items():
            response += f"• `{task_id}`: {status}\n"
    
    await message.reply(response)

# ================== FIN del nuevo comando ==================

@app.on_message(filters.text | filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    if message.text.startswith('/'):
        return
    
    user_input = message.text
    parts = user_input.split(" | ", 1)
    url = parts[0].strip()
    custom_name = parts[1].strip() if len(parts) > 1 else None
    
    if not url.startswith(("http://", "https://")):
        return
    
    # Generar ID único para esta elección
    choice_id = str(uuid.uuid4())[:8]
    pending_choices[choice_id] = {
        "url": url,
        "custom_name": custom_name,
        "timestamp": time.time()
    }
    
    # Crear teclado con opciones
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Aria2c (16 conexiones)", callback_data=f"aria2:{choice_id}"),
                InlineKeyboardButton("yt-dlp", callback_data=f"ytdlp:{choice_id}")
            ]
        ]
    )
    
    await message.reply(
        "Selecciona el método de descarga:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r'^(aria2|ytdlp):'))
async def handle_download_choice(client, callback_query):
    """Maneja la elección del método de descarga"""
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    # Parsear datos del callback
    parts = data.split(':', 1)
    method = parts[0]
    choice_id = parts[1]
    
    # Recuperar información de la elección
    choice_info = pending_choices.get(choice_id)
    if not choice_info:
        await callback_query.answer("❌ La opción ha expirado. Por favor inicia una nueva descarga.", show_alert=True)
        await callback_query.message.delete()
        return
    
    # Eliminar elección pendiente
    del pending_choices[choice_id]
    
    url = choice_info["url"]
    custom_name = choice_info["custom_name"]
    
    # Generar ID único para la tarea
    task_id = str(uuid.uuid4())[:8].upper()
    task_counters[callback_query.message.chat.id] += 1
    
    # Eliminar mensaje de selección
    await callback_query.message.delete()
    
    # Preparar datos de la tarea
    task_data = {
        'task_id': task_id,
        'url': url,
        'custom_name': custom_name,
        'method': method,
        'chat_id': callback_query.message.chat.id,
        'user_id': user_id
    }
    
    # ============== NUEVO: Agregar a la cola en lugar de ejecutar ==============
    queue_position = await download_queue.add_task(task_data)
    
    # Iniciar procesamiento de la cola
    asyncio.create_task(download_queue.process_queue())
    
    # Enviar confirmación de encolado
    await callback_query.message.reply(
        f"✅ **Tarea añadida a la cola**\n\n"
        f"**ID de tarea:** `{task_id}`\n"
        f"**Método:** `{method}`\n"
        f"**Posición en cola:** `{queue_position}`\n\n"
        "Usa /queue para ver el estado de la cola"
    )
    # ================== FIN de la modificación ==================

# ============== NUEVA FUNCIÓN: Manejo de tareas desde la cola ==============
async def handle_download_task(task_data):
    """Procesa una tarea de descarga desde la cola"""
    task_id = task_data['task_id']
    url = task_data['url']
    custom_name = task_data['custom_name']
    method = task_data['method']
    chat_id = task_data['chat_id']
    
    # Iniciar mensaje de progreso
    msg = await app.send_message(chat_id, f"[{task_id}] ⏬ Iniciando descarga con {method}...")
    progress = DownloadProgress(msg, task_id)
    active_tasks[task_id] = progress
    
    # Descargar contenido
    file_path = await download_content(
        url, 
        custom_name,
        progress.update,
        task_id,
        method=method
    )
    
    if not file_path or not os.path.exists(file_path):
        await msg.edit(f"[{task_id}] ❌ Error al descargar el contenido")
        if task_id in active_tasks:
            del active_tasks[task_id]
        return
    
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    
    # Determinar si se necesita dividir
    if file_size > MAX_DIRECT_SIZE:
        await msg.edit(f"[{task_id}] 📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
        await split_and_upload(client, msg, file_path, task_id)
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
                
                await app.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=caption,
                    duration=duration,
                    thumb=thumb,
                    progress=upload_progress_callback,
                    progress_args=(msg, task_id)
                )
                
                # Eliminar miniatura temporal
                if thumb and os.path.exists(thumb):
                    os.remove(thumb)
            else:
                await app.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    progress=upload_progress_callback,
                    progress_args=(msg, task_id)
                )
            await msg.edit(f"[{task_id}] ✅ Subida completada")
        except Exception as e:
            await msg.edit(f"[{task_id}] ❌ Error en subida: {str(e)}")
    
    # Limpieza
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    
    # Eliminar tarea de seguimiento
    if task_id in active_tasks:
        del active_tasks[task_id]
# ================== FIN de la nueva función ==================

async def upload_progress_callback(current, total, msg, task_id):
    """Muestra progreso de subida cada 15 segundos"""
    current_time = time.time()
    if not hasattr(upload_progress_callback, 'last_update'):
        upload_progress_callback.last_update = {}
    
    if task_id not in upload_progress_callback.last_update:
        upload_progress_callback.last_update[task_id] = 0
    
    # Actualizar cada 15 segundos
    if current_time - upload_progress_callback.last_update[task_id] > 15:
        upload_progress_callback.last_update[task_id] = current_time
        percent = current * 100 / total
        try:
            await msg.edit(
                f"[{task_id}] ⬆️ Subiendo...\n"
                f"📊 {percent:.1f}%\n"
                f"📦 {current//(1024*1024)}MB/{total//(1024*1024)}MB"
            )
        except Exception:
            pass  # Ignorar errores de actualización

# Limpiar elecciones pendientes periódicamente
async def clean_pending_choices():
    while True:
        await asyncio.sleep(3600)  # Cada hora
        current_time = time.time()
        expired = []
        for choice_id, choice_info in pending_choices.items():
            if current_time - choice_info["timestamp"] > 3600:  # 1 hora de expiración
                expired.append(choice_id)
        
        for choice_id in expired:
            del pending_choices[choice_id]

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado con Pyrofork ⚡")
    
    # Crear tarea de limpieza después de iniciar el cliente
    async def main():
        await app.start()
        asyncio.create_task(clean_pending_choices())
        await asyncio.Event().wait()  # Mantener el bot en ejecución
        
    try:
        app.run(main())
    finally:
        app.stop()
