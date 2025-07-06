import os
import subprocess
import asyncio
import time
import re
import logging
import mimetypes
import uuid
import json
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

# Almacenamiento de tareas activas
active_tasks = {}
task_counters = defaultdict(int)
current_downloads = {}
user_active_tasks = {}
pending_quality_selection = {}

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

async def download_content(url, custom_filename=None, progress_callback=None, task_id=None, format_id=None):
    """Descarga contenido usando yt-dlp para todos los tipos de enlaces"""
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    
    # Usar yt-dlp para todos los tipos de enlaces
    return await download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id, format_id)

async def download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id, format_id):
    """Descarga contenido usando yt-dlp con concurrencia mejorada"""
    try:
        cmd = [
            "yt-dlp",
            "-o", f"{download_path}/%(title)s.%(ext)s",
            "--no-playlist",
            "--concurrent-fragments", "5",  # Fragmentos concurrentes
            "--newline",  # Salida en formato nueva línea
            url
        ]
        
        if format_id:
            cmd.extend(["-f", format_id])
        
        logger.info(f"Iniciando descarga yt-dlp: {' '.join(cmd)}")
        
        # Ejecutar yt-dlp capturando salida
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Registrar proceso para posible cancelación
        current_downloads[task_id] = process
        
        # Procesar salida para obtener progreso
        progress_pattern = re.compile(
            r'\[download\]\s+(\d+\.\d+)%.*?(\d+\.\d+)([KM]iB)/s.*?ETA\s+(\d+:\d+)'
        )
        
        last_progress_update = time.time()
        start_time = time.time()
        last_percent = 0
        
        for line in iter(process.stdout.readline, ''):
            if progress_callback and task_id in active_tasks:
                # Intentar extraer datos de progreso
                match = progress_pattern.search(line)
                if match:
                    percent = match.group(1)
                    speed_value = match.group(2)
                    speed_unit = match.group(3)
                    eta = match.group(4)
                    
                    # Actualizar progreso
                    await progress_callback({
                        'percent': f"{percent}%",
                        'speed': f"{speed_value} {speed_unit}/s",
                        'eta': eta
                    })
                    last_progress_update = time.time()
                    last_percent = float(percent)
                else:
                    # Actualizar solo porcentaje si está disponible
                    match_percent = re.search(r'(\d+\.\d+)%', line)
                    if match_percent:
                        percent = match_percent.group(1)
                        # Forzar actualización solo si hay cambio significativo
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

def is_supported_site(url):
    """Verifica si la URL es de un sitio compatible para selección de calidad"""
    return any(domain in url for domain in ['youtube.com', 'youtu.be', 'facebook.com', 'instagram.com'])

async def get_available_formats(url):
    """Obtiene formatos disponibles usando yt-dlp"""
    try:
        cmd = [
            'yt-dlp',
            '--list-formats',
            '--no-playlist',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout
    except Exception as e:
        logger.error(f"Error obteniendo formatos: {e}")
        return None

def parse_formats(output):
    """Parsea la salida de yt-dlp --list-formats"""
    formats = []
    lines = output.split('\n')
    
    # Buscar la sección de formatos
    start_index = None
    for i, line in enumerate(lines):
        if "ID  EXT  RESOLUTION" in line:
            start_index = i + 1
            break
    
    if start_index is None:
        return formats
    
    # Procesar líneas de formatos
    for line in lines[start_index:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
            
        format_id = parts[0]
        resolution = parts[2] if parts[2] != 'audio' else 'Audio'
        note = ' '.join(parts[8:])
        
        formats.append({
            'id': format_id,
            'resolution': resolution,
            'note': note
        })
    
    return formats

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

@app.on_callback_query()
async def handle_quality_selection(client: Client, callback_query):
    """Maneja la selección de calidad/formato"""
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if user_id not in pending_quality_selection:
        await callback_query.answer("Esta selección ha expirado", show_alert=True)
        return
    
    context = pending_quality_selection[user_id]
    
    # Extraer el ID de formato
    format_id = data.split("_")[1]
    
    await callback_query.message.edit(f"🔄 Descargando con el formato seleccionado...")
    
    # Generar ID único para la tarea
    task_id = str(uuid.uuid4())[:8].upper()
    task_counters[callback_query.message.chat.id] += 1
    
    # Registrar tarea para el usuario
    user_active_tasks[user_id] = task_id
    
    progress = DownloadProgress(callback_query.message, task_id)
    active_tasks[task_id] = progress
    
    # Descargar contenido con el formato seleccionado
    file_path = await download_content(
        context['url'], 
        context.get('custom_name'),
        progress.update,
        task_id,
        format_id
    )
    
    # Procesar el archivo descargado
    if not file_path or not os.path.exists(file_path):
        await callback_query.message.edit(f"[{task_id}] ❌ Error al descargar el contenido")
        if task_id in active_tasks:
            del active_tasks[task_id]
        if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
            del user_active_tasks[user_id]
        return
    
    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    
    # Determinar si se necesita dividir
    if file_size > MAX_DIRECT_SIZE:
        await callback_query.message.edit(f"[{task_id}] 📦 Archivo grande detectado ({size_mb:.2f} MB > 1990 MB). Dividiendo...")
        await split_and_upload(client, callback_query.message, callback_query.message, file_path, task_id)
    else:
        await callback_query.message.edit(f"[{task_id}] ✅ Descarga completa ({size_mb:.2f} MB)\n⬆️ Subiendo a Telegram...")
        try:
            # Detectar tipo MIME para enviar como video si es posible
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
                
                await client.send_video(
                    chat_id=callback_query.message.chat.id,
                    video=file_path,
                    caption=caption,
                    duration=duration,
                    thumb=thumb,
                    progress=upload_progress_callback,
                    progress_args=(callback_query.message, task_id)
                )
                
                # Eliminar miniatura temporal
                if thumb and os.path.exists(thumb):
                    os.remove(thumb)
            else:
                await client.send_document(
                    chat_id=callback_query.message.chat.id,
                    document=file_path,
                    progress=upload_progress_callback,
                    progress_args=(callback_query.message, task_id)
                )
            await callback_query.message.edit(f"[{task_id}] ✅ Subida completada")
        except Exception as e:
            await callback_query.message.edit(f"[{task_id}] ❌ Error en subida: {str(e)}")
    
    # Limpieza
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    
    # Eliminar tarea de seguimiento
    if task_id in active_tasks:
        del active_tasks[task_id]
    
    # Liberar usuario al finalizar la tarea
    if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
        del user_active_tasks[user_id]

    # Limpiar selección pendiente
    del pending_quality_selection[user_id]

# Filtro para manejar enlaces
@app.on_message(filters.text | filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    # Verificar si el mensaje contiene un comando
    if message.text.startswith('/'):
        return
    
    # Verificar si el usuario ya tiene una tarea activa
    user_id = message.from_user.id
    if user_id in user_active_tasks:
        task_id_actual = user_active_tasks[user_id]
        if task_id_actual in active_tasks:
            await message.reply(
                "⚠️ Ya tienes una tarea en curso.\n"
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
    
    # Verificar si es un sitio compatible para selección de calidad
    if is_supported_site(url):
        # Obtener formatos disponibles
        await message.reply("⏳ Obteniendo formatos disponibles...")
        formats_output = await get_available_formats(url)
        
        if not formats_output:
            await message.reply("❌ No se pudieron obtener los formatos. Descargando por defecto...")
        else:
            formats = parse_formats(formats_output)
            
            if not formats:
                await message.reply("❌ No se encontraron formatos. Descargando por defecto...")
            else:
                # Guardar contexto para la selección
                pending_quality_selection[user_id] = {
                    'url': url,
                    'custom_name': custom_name,
                    'message_id': message.id
                }
                
                # Crear teclado con opciones
                buttons = []
                for fmt in formats[:10]:  # Mostrar máximo 10 opciones
                    btn_text = f"{fmt['resolution']} - {fmt['note'][:20]}"
                    buttons.append([InlineKeyboardButton(btn_text, callback_data=f"format_{fmt['id']}")])
                
                keyboard = InlineKeyboardMarkup(buttons)
                
                await message.reply(
                    "🎚️ **Selecciona el formato deseado:**",
                    reply_markup=keyboard
                )
                return
    
    # Generar ID único para la tarea
    task_id = str(uuid.uuid4())[:8].upper()
    task_counters[message.chat.id] += 1
    
    # Registrar tarea para el usuario
    user_active_tasks[user_id] = task_id
    
    msg = await message.reply(f"[{task_id}] ⏬ Iniciando descarga...")
    progress = DownloadProgress(msg, task_id)
    active_tasks[task_id] = progress
    
    # Descargar contenido
    file_path = await download_content(
        url, 
        custom_name,
        progress.update,
        task_id
    )
    
    if not file_path or not os.path.exists(file_path):
        await msg.edit(f"[{task_id}] ❌ Error al descargar el contenido")
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
            # Detectar tipo MIME para enviar como video si es posible
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
                
                await client.send_video(
                    chat_id=message.chat.id,
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
                await client.send_document(
                    chat_id=message.chat.id,
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
    
    # Liberar usuario al finalizar la tarea
    if user_id in user_active_tasks and user_active_tasks[user_id] == task_id:
        del user_active_tasks[user_id]

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

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado con Pyrofork ⚡")
    app.run()
