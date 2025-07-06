import os
import subprocess
import asyncio
import time
import re
import logging
import mimetypes
import uuid
import json
import shutil
from collections import defaultdict
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from split_upload import split_and_upload

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

# Almacenamiento de tareas activas
active_tasks = {}
task_counters = defaultdict(int)
current_downloads = {}

def is_owner(user_id):
    return user_id == OWNER_ID

class DownloadProgress:
    def __init__(self, message, task_id):
        self.message = message
        self.last_update = 0
        self.progress_text = ""
        self.start_time = time.time()
        self.task_id = task_id
        self.cancelled = False
        self.keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])

    async def update(self, text):
        if self.cancelled:
            return
            
        current_time = time.time()
        # Actualizar solo si han pasado más de 20 segundos
        if current_time - self.last_update > 20:
            self.last_update = current_time
            if text != self.progress_text:
                self.progress_text = text
                try:
                    await self.message.edit(f"[{self.task_id}] {text}", reply_markup=self.keyboard)
                except Exception as e:
                    logger.error(f"Error al actualizar progreso: {str(e)}")

def cancel_task(task_id):
    """Cancela una tarea por su ID"""
    if task_id in current_downloads:
        logger.info(f"Cancelling task {task_id}")
        # Matar el proceso asociado
        process = current_downloads[task_id]
        try:
            if process.poll() is None:  # Si el proceso aún está en ejecución
                process.terminate()
                logger.info(f"Proceso {task_id} terminado")
        except Exception as e:
            logger.error(f"Error terminando proceso: {str(e)}")
        
        # Marcar progreso como cancelado
        if task_id in active_tasks:
            active_tasks[task_id].cancelled = True
        
        # Eliminar de las estructuras de seguimiento
        if task_id in current_downloads:
            del current_downloads[task_id]
        if task_id in active_tasks:
            del active_tasks[task_id]
        
        return True
    return False

async def get_video_formats(url):
    """Obtiene los formatos de video disponibles usando yt-dlp"""
    cmd = ["yt-dlp", "-F", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"Error al obtener formatos: {result.stderr}")
            return None
        
        # Parsear la salida
        formats = []
        for line in result.stdout.split('\n'):
            if re.match(r'^\d+\s+\w+', line):
                parts = line.split()
                format_id = parts[0]
                resolution = next((p for p in parts if re.match(r'^\d+x\d+', p)), "")
                if resolution:
                    formats.append((format_id, resolution))
        return formats
    except Exception as e:
        logger.error(f"Error en get_video_formats: {e}")
        return None

async def download_content(url, custom_filename=None, progress_callback=None, task_id=None, format_id=None):
    """Descarga contenido usando yt-dlp con formato específico si se especifica"""
    download_path = f"/tmp/downloads/{task_id}"
    os.makedirs(download_path, exist_ok=True)
    
    try:
        cmd = [
            "yt-dlp",
            "-o", f"{download_path}/%(title)s.%(ext)s",
            "--no-playlist",
            "--concurrent-fragments", "5",
            "--no-part",
            url
        ]
        
        if format_id:
            cmd.extend(["-f", format_id])
        if custom_filename:
            cmd.extend(["-o", f"{download_path}/{custom_filename}"])

        logger.info(f"Iniciando descarga yt-dlp: {' '.join(cmd)}")
        
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        # Registrar proceso para posible cancelación
        current_downloads[task_id] = process
        
        # Procesar salida para obtener progreso
        for line in process.stdout:
            if progress_callback and task_id in active_tasks and not active_tasks[task_id].cancelled:
                if "ETA" in line and "]" in line:
                    match = re.search(r'(\d+\.?\d*)%', line)
                    if match:
                        percent = match.group(1)
                        await progress_callback(f"⏬ Descargando... {percent}%")
        
        process.wait()
        if process.returncode != 0:
            logger.error(f"Error en descarga yt-dlp: {process.returncode}")
            return None
        
        # Buscar el archivo descargado
        files = os.listdir(download_path)
        return os.path.join(download_path, files[0]) if files else None
        
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
        "Archivos >1990MB se dividirán automáticamente\n\n"
        "Comandos disponibles:\n"
        "/start - Muestra este mensaje\n"
        "/update - Actualiza herramientas (solo propietario)"
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
        update_cmd = [
            "pip", "install", "--upgrade", 
            "yt-dlp[default,curl-cffi]"
        ]
        result = subprocess.run(
            update_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"Error al actualizar: {result.stdout}")
        
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
        with open(log_file, "w") as f:
            f.write(str(e))
        
        await client.send_document(
            chat_id=message.chat.id,
            document=log_file,
            caption="❌ Error al actualizar herramientas"
        )
        await msg.edit("⚠️ Actualización fallida. Ver log para detalles.")

@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query: CallbackQuery):
    """Maneja todos los callbacks de botones"""
    data = callback_query.data
    task_id = data.split('_')[-1]
    
    if data.startswith("cancel_"):
        if cancel_task(task_id):
            await callback_query.answer("✅ Tarea cancelada")
            await callback_query.message.edit(f"[{task_id}] ❌ Tarea cancelada por el usuario")
        else:
            await callback_query.answer("⚠️ Tarea no encontrada o ya completada")
    
    elif data.startswith("format_"):
        format_id = data.split('_')[1]
        task_data = active_tasks.get(task_id, {})
        
        if not task_data:
            await callback_query.answer("⚠️ Tarea expirada")
            return
            
        await callback_query.answer(f"Seleccionado formato: {format_id}")
        await callback_query.message.edit(f"[{task_id}] ⏬ Preparando descarga...")
        
        # Iniciar descarga con formato seleccionado
        file_path = await download_content(
            task_data['url'], 
            task_data.get('custom_name'),
            task_data['progress'].update,
            task_id,
            format_id
        )
        
        await process_download_completion(client, task_id, file_path, callback_query.message)

async def process_download_completion(client, task_id, file_path, progress_message):
    """Procesa el resultado de la descarga y maneja la subida"""
    # Verificar si la tarea fue cancelada durante la descarga
    if task_id not in active_tasks or active_tasks[task_id]['progress'].cancelled:
        if file_path and os.path.exists(file_path):
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
        return
    
    if not file_path or not os.path.exists(file_path):
        await progress_message.edit(f"[{task_id}] ❌ Error al descargar el contenido")
        if task_id in active_tasks:
            del active_tasks[task_id]
        return
    
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    
    # Determinar si se necesita dividir
    if file_size > MAX_DIRECT_SIZE:
        await progress_message.edit(f"[{task_id}] 📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
        await split_and_upload(
            client, 
            active_tasks[task_id]['original_message'], 
            progress_message, 
            file_path, 
            task_id
        )
    else:
        await progress_message.edit(f"[{task_id}] ✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo a Telegram...")
        try:
            # Detectar tipo MIME
            mime_type, _ = mimetypes.guess_type(file_path)
            is_video = mime_type and mime_type.startswith('video/')
            
            if is_video:
                metadata = get_video_metadata(file_path)
                caption = f"📹 {os.path.basename(file_path)}"
                if metadata:
                    size_mb = metadata['size'] / (1024 * 1024)
                    caption = (
                        f"📹 {os.path.basename(file_path)}\n"
                        f"💾 {size_mb:.2f} MB\n"
                        f"🖥️ {metadata['resolution']}\n"
                        f"⏱️ {metadata['duration']:.2f} seg"
                    )
                
                await client.send_video(
                    chat_id=active_tasks[task_id]['original_message'].chat.id,
                    video=file_path,
                    caption=caption,
                    progress=upload_progress_callback,
                    progress_args=(progress_message, task_id)
                )
            else:
                await client.send_document(
                    chat_id=active_tasks[task_id]['original_message'].chat.id,
                    document=file_path,
                    progress=upload_progress_callback,
                    progress_args=(progress_message, task_id)
                )
            await progress_message.edit(f"[{task_id}] ✅ Subida completada")
        except Exception as e:
            await progress_message.edit(f"[{task_id}] ❌ Error en subida: {str(e)}")
    
    # Limpieza
    if file_path and os.path.exists(file_path):
        shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
    
    # Eliminar tarea de seguimiento
    if task_id in active_tasks:
        del active_tasks[task_id]

# Filtro para manejar enlaces
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
    
    # Generar ID único para la tarea
    task_id = str(uuid.uuid4())[:8].upper()
    
    # Verificar si es un video con múltiples resoluciones
    is_video_source = any(domain in url for domain in ["youtube.com", "youtu.be", "vimeo.com"])
    if is_video_source:
        formats = await get_video_formats(url)
        if formats:
            buttons = []
            for format_id, resolution in formats:
                buttons.append([InlineKeyboardButton(
                    f"📹 {resolution}", 
                    callback_data=f"format_{format_id}_{task_id}"
                )])
            
            buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")])
            keyboard = InlineKeyboardMarkup(buttons)
            
            msg = await message.reply(
                f"[{task_id}] Seleccione la resolución deseada:",
                reply_markup=keyboard
            )
            
            # Almacenar información de la tarea
            active_tasks[task_id] = {
                'url': url,
                'custom_name': custom_name,
                'progress': DownloadProgress(msg, task_id),
                'original_message': message
            }
            return
    
    # Si no hay formatos disponibles o no es video
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])
    msg = await message.reply(
        f"[{task_id}] ⏬ Iniciando descarga...",
        reply_markup=keyboard
    )
    
    # Almacenar información de la tarea
    progress = DownloadProgress(msg, task_id)
    active_tasks[task_id] = {
        'url': url,
        'custom_name': custom_name,
        'progress': progress,
        'original_message': message
    }
    
    # Descargar contenido
    file_path = await download_content(
        url, 
        custom_name,
        progress.update,
        task_id
    )
    
    await process_download_completion(client, task_id, file_path, msg)

async def upload_progress_callback(current, total, msg, task_id):
    """Muestra progreso de subida cada 20 segundos"""
    if not hasattr(upload_progress_callback, 'last_update'):
        upload_progress_callback.last_update = {}
    
    if task_id not in upload_progress_callback.last_update:
        upload_progress_callback.last_update[task_id] = 0
    
    # Verificar si la tarea fue cancelada
    if task_id in active_tasks and active_tasks[task_id]['progress'].cancelled:
        return
    
    current_time = time.time()
    if current_time - upload_progress_callback.last_update[task_id] > 20:
        upload_progress_callback.last_update[task_id] = current_time
        percent = current * 100 / total
        try:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])
            await msg.edit(f"[{task_id}] ⬆️ Subiendo... {percent:.1f}%", reply_markup=keyboard)
        except Exception:
            pass

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado con Pyrofork ⚡")
    app.run()
