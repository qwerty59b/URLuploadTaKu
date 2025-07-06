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
from pyrogram.types import Message
from split_upload import split_and_upload

# Configuración
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
MAX_DIRECT_SIZE = 1990 * 1024 * 1024  # 1990 MB
CONCURRENT_CONNECTIONS = 16  # Conexiones concurrentes para descargas

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
                    await self.message.edit(f"[{self.task_id}] {text}")
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

async def download_content(url, custom_filename=None, progress_callback=None, task_id=None):
    """Descarga contenido usando aria2c o yt-dlp según el tipo de enlace"""
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
        # Descargar con aria2c (más rápido y con soporte para múltiples conexiones)
        return await download_with_aria2c(url, download_path, custom_filename, progress_callback, task_id)
    else:
        # Descargar con yt-dlp
        return await download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id)

async def download_with_aria2c(url, download_path, custom_filename, progress_callback, task_id):
    """Descarga contenido usando aria2c para enlaces directos (más rápido)"""
    try:
        filename = custom_filename if custom_filename else os.path.basename(url)
        output_path = os.path.join(download_path, filename)
        
        cmd = [
            "aria2c",
            "-x", str(CONCURRENT_CONNECTIONS),  # Número máximo de conexiones
            "-s", str(CONCURRENT_CONNECTIONS),  # Número de conexiones por servidor
            "-j", "5",  # Número máximo de descargas paralelas
            "-c",  # Continuar descarga interrumpida
            "--file-allocation=none",  # Sin pre-asignación de espacio (más rápido)
            "-d", download_path,
            "-o", filename,
            url
        ]
        
        logger.info(f"Iniciando descarga aria2c: {' '.join(cmd)}")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        # Registrar proceso para posible cancelación
        current_downloads[task_id] = process
        
        # Procesar salida para obtener progreso
        total_size = 0
        downloaded = 0
        
        for line in process.stdout:
            if progress_callback and task_id in active_tasks and not active_tasks[task_id].cancelled:
                # Buscar tamaño total
                if "Length:" in line:
                    size_match = re.search(r'Length: (\d+)', line)
                    if size_match:
                        total_size = int(size_match.group(1))
                
                # Buscar progreso actual
                match = re.search(r'(\d+)%\)', line)
                if match:
                    percent = match.group(1)
                    await progress_callback(f"⏬ Descargando... {percent}%")
                elif "download completed" in line:
                    await progress_callback("✅ Descarga completada")
        
        process.wait()
        if process.returncode != 0:
            logger.error(f"Error en descarga aria2c: {process.returncode}")
            return None
        
        return output_path
        
    except Exception as e:
        logger.error(f"Error en descarga aria2c: {e}")
        return None
    finally:
        if task_id in current_downloads:
            del current_downloads[task_id]

async def download_with_ytdlp(url, download_path, custom_filename, progress_callback, task_id):
    """Descarga contenido usando yt-dlp con concurrencia mejorada"""
    try:
        cmd = [
            "yt-dlp",
            "-o", f"{download_path}/%(title)s.%(ext)s",
            "--no-playlist",
            "--concurrent-fragments", "5",  # Fragmentos concurrentes
            url
        ]
        
        logger.info(f"Iniciando descarga yt-dlp: {' '.join(cmd)}")
        
        # Ejecutar yt-dlp capturando salida
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
                    # Extraer porcentaje de progreso
                    match = re.search(r'(\d+\.\d+)%', line)
                    if match:
                        percent = match.group(1)
                        await progress_callback(f"⏬ Descargando... {percent}%")
        
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
        "/cancel [ID] - Cancela una tarea en progreso\n"
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
        # Actualizar yt-dlp y wget
        update_cmd = [
            "pip", "install", "--upgrade", 
            "yt-dlp[default,curl-cffi]", 
            "aria2c"
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
        
        aria_cmd = ["aria2c", "--version"]
        aria_result = subprocess.run(aria_cmd, stdout=subprocess.PIPE, text=True)
        aria_version = aria_result.stdout.split('\n')[0]
        
        await msg.edit(
            f"✅ Herramientas actualizadas:\n"
            f"- yt-dlp: {ytdlp_version}\n"
            f"- aria2c: {aria_version}\n\n"
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

@app.on_message(filters.command("cancel"))
async def cancel_command(client: Client, message: Message):
    """Cancela una tarea por ID"""
    args = message.text.split()
    if len(args) < 2:
        active_list = "\n".join([f"- {task_id}" for task_id in active_tasks.keys()])
        await message.reply(
            f"❌ Uso: /cancel <ID>\n\n"
            f"Tareas activas:\n{active_list if active_list else 'No hay tareas activas'}"
        )
        return
    
    task_id = args[1].strip()
    if cancel_task(task_id):
        await message.reply(f"✅ Tarea {task_id} cancelada correctamente")
    else:
        await message.reply("⚠️ ID de tarea no encontrada o ya completada")

# Filtro para manejar enlaces
@app.on_message(filters.text | filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de archivos/videos"""
    # Verificar si el mensaje contiene un comando
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
    task_counters[message.chat.id] += 1
    
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
    
    # Verificar si la tarea fue cancelada durante la descarga
    if task_id not in active_tasks or active_tasks[task_id].cancelled:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        return
    
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
                    chat_id=message.chat.id,
                    video=file_path,
                    caption=caption,
                    progress=upload_progress_callback,
                    progress_args=(msg, task_id)
                )
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

async def upload_progress_callback(current, total, msg, task_id):
    """Muestra progreso de subida cada 20 segundos"""
    current_time = time.time()
    if not hasattr(upload_progress_callback, 'last_update'):
        upload_progress_callback.last_update = {}
    
    if task_id not in upload_progress_callback.last_update:
        upload_progress_callback.last_update[task_id] = 0
    
    # Verificar si la tarea fue cancelada
    if task_id in active_tasks and active_tasks[task_id].cancelled:
        return
    
    # Actualizar solo si han pasado más de 20 segundos
    if current_time - upload_progress_callback.last_update[task_id] > 20:
        upload_progress_callback.last_update[task_id] = current_time
        percent = current * 100 / total
        try:
            await msg.edit(f"[{task_id}] ⬆️ Subiendo... {percent:.1f}%")
        except Exception:
            pass  # Ignorar errores de actualización

if __name__ == "__main__":
    logger.info("⚡ Bot iniciado con Pyrofork ⚡")
    app.run()
