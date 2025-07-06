import os
import subprocess
import asyncio
import time
import re
from kunigram import Client, filters
from kunigram.types import Message
from split_upload import split_and_upload

# Configuración
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH']
BOT_TOKEN = os.environ['BOT_TOKEN']
OWNER_ID = int(os.environ['OWNER_ID'])
MAX_DIRECT_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

app = Client(
    "ytdlp_bot",
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

    async def update(self, text):
        current_time = time.time()
        # Actualizar solo si han pasado más de 20 segundos
        if current_time - self.last_update > 20:
            self.last_update = current_time
            if text != self.progress_text:
                self.progress_text = text
                await self.message.edit(text)

async def download_with_ytdlp(url, custom_filename=None, progress_callback=None):
    """Descarga contenido usando yt-dlp"""
    download_path = "/tmp/downloads"
    os.makedirs(download_path, exist_ok=True)
    
    cmd = [
        "yt-dlp",
        "-o", f"{download_path}/%(title)s.%(ext)s",
        "--no-playlist",
        url
    ]
    
    try:
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
        print(f"Error en descarga: {e}")
        return None

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply(
        "🤖 **Bot de Descargas Avanzado**\n\n"
        "Envía un enlace de video para subirlo a Telegram\n\n"
        "Formatos soportados: MP4, M3U8, YouTube, etc.\n\n"
        "Para renombrar: `http://ejemplo.com/video.mp4 | mi_video.mp4`\n\n"
        "Archivos >2GB se dividirán automáticamente"
    )

@app.on_message(filters.command("update") & filters.private)
async def update_bot(client: Client, message: Message):
    """Actualiza yt-dlp y reinicia el bot (solo owner)"""
    if not is_owner(message.from_user.id):
        await message.reply("❌ Solo el propietario puede usar este comando")
        return
        
    msg = await message.reply("🔄 Actualizando yt-dlp...")
    update_cmd = ["pip", "install", "--upgrade", "yt-dlp[default,curl-cffi]"]
    subprocess.run(update_cmd, check=True)
    
    await msg.edit("✅ yt-dlp actualizado. Reiniciando bot...")
    os._exit(0)  # Reinicio controlado

@app.on_message(filters.text & ~filters.command)
async def handle_links(client: Client, message: Message):
    """Procesa enlaces de video"""
    user_input = message.text
    parts = user_input.split(" | ", 1)
    url = parts[0].strip()
    custom_name = parts[1].strip() if len(parts) > 1 else None
    
    if not url.startswith(("http://", "https://")):
        return
    
    msg = await message.reply("⏬ Iniciando descarga...")
    progress = DownloadProgress(msg)
    
    # Descargar con yt-dlp
    file_path = await download_with_ytdlp(
        url, 
        custom_name,
        progress.update
    )
    
    if not file_path or not os.path.exists(file_path):
        await msg.edit("❌ Error al descargar el contenido")
        return
    
    file_size = os.path.getsize(file_path)
    await msg.edit(f"✅ Descarga completa ({file_size/1024/1024:.2f} MB)\n⚡ Procesando...")
    
    try:
        # Manejar archivos grandes (>2GB)
        if file_size > MAX_DIRECT_SIZE:
            await msg.edit("📦 Archivo muy grande, dividiendo...")
            await split_and_upload(client, message, msg, file_path)
        else:
            await msg.edit("⬆️ Subiendo a Telegram...")
            await client.send_document(
                chat_id=message.chat.id,
                document=file_path,
                progress=upload_progress_callback,
                progress_args=(msg,)
            )
            await msg.edit("✅ Subida completada")
    
    except Exception as e:
        await msg.edit(f"❌ Error: {str(e)}")
    finally:
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
        await msg.edit(f"⬆️ Subiendo... {percent:.1f}%")

if __name__ == "__main__":
    print("⚡ Bot iniciado ⚡")
    app.run()