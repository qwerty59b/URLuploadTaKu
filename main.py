import os
import re
import asyncio
import logging
import subprocess
import concurrent.futures
from pathlib import Path
from typing import Tuple, Dict, Optional
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaDocument
from pyrogram.errors import FloodWait, RPCError
from tqdm import tqdm
import yt_dlp
import requests
import py7zr
from ffmpeg import probe, input

# Configuración básica
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables de entorno
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_SIZE = 1990 * 1024 * 1024  # 1990 MB
DOWNLOAD_PATH = "downloads"
THUMB_PATH = "thumbnails"

# Crear directorios
os.makedirs(DOWNLOAD_PATH, exist_ok=True)
os.makedirs(THUMB_PATH, exist_ok=True)

# Estado de usuarios
user_tasks: Dict[int, bool] = defaultdict(bool)
progress_data: Dict[int, Dict[str, float]] = defaultdict(dict)
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Utilidades
def parse_message(text: str) -> Tuple[str, Optional[str]]:
    if " | " in text:
        url, filename = text.split(" | ", 1)
        return url.strip(), filename.strip()
    return text.strip(), None

def is_media_file(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in [
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv"
    ])

def get_video_metadata(file_path: str) -> Dict[str, str]:
    try:
        metadata = probe(file_path)
        video_stream = next(
            (s for s in metadata["streams"] if s["codec_type"] == "video"), None
        )
        return {
            "duration": str(float(metadata["format"]["duration"])),
            "width": video_stream.get("width", "?"),
            "height": video_stream.get("height", "?"),
            "size": str(os.path.getsize(file_path))
        }
    except Exception:
        return {}

def create_thumbnail(video_path: str, output_path: str) -> bool:
    try:
        (
            input(video_path)
            .filter("thumbnail")
            .output(output_path, vframes=1, format="image2")
            .run(overwrite_output=True, capture_stdout=True, capture_stderr=True)
        )
        return True
    except Exception as e:
        logger.error(f"Error creating thumbnail: {e}")
        return False

def split_file(file_path: str, output_dir: str) -> list:
    archive_name = Path(file_path).stem
    output_pattern = os.path.join(output_dir, f"{archive_name}.7z.001")
    
    with py7zr.SevenZipFile(output_pattern, "w") as archive:
        archive.write(file_path, os.path.basename(file_path))
    
    return sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.startswith(archive_name) and f.endswith(".7z.001")
    ])

def download_wget(url: str, filename: str, chat_id: int) -> bool:
    try:
        cmd = [
            "wget", "-O", filename, "-c", "--tries=0", "--timeout=5",
            "--waitretry=0", "--retry-connrefused", "--quiet",
            "--show-progress", "--progress=dot:giga", url
        ]
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        total_size = 0
        downloaded = 0
        
        while True:
            line = process.stderr.readline()
            if not line:
                break
                
            if "%" in line:
                parts = line.split()
                if len(parts) >= 3 and parts[1].endswith("%"):
                    percentage = float(parts[1].replace("%", ""))
                    downloaded = (percentage / 100) * total_size
                elif "Length:" in line:
                    size_part = line.split("Length:")[1].split()[0]
                    if size_part.isdigit():
                        total_size = int(size_part)
            
            progress_data[chat_id]["downloaded"] = downloaded
            progress_data[chat_id]["total"] = total_size
            
        return process.wait() == 0
    except Exception:
        return False

def download_ytdlp(url: str, filename: str, chat_id: int) -> bool:
    class ProgressHook:
        def __init__(self):
            self.last_update = 0
            
        def __call__(self, d):
            if d["status"] == "downloading":
                progress_data[chat_id]["downloaded"] = d.get("downloaded_bytes", 0)
                progress_data[chat_id]["total"] = d.get("total_bytes", 0)
    
    ydl_opts = {
        "outtmpl": filename,
        "progress_hooks": [ProgressHook()],
        "noprogress": False,
        "concurrent_fragment_downloads": 10,
        "http_chunk_size": 1048576,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "retries": 10,
        "fragment_retries": 10,
        "extractor_args": {
            "youtube": {"player_client": ["android"]},
            "youtubetab": {"skip": ["dash", "hls"]}
        },
        "hls_prefer_native": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.google.com/"
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except yt_dlp.DownloadError:
        return False

async def download_file(url: str, filename: str, chat_id: int) -> bool:
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        # Primero intentamos con wget
        success = await loop.run_in_executor(
            pool, download_wget, url, filename, chat_id
        )
        
        # Si falla, intentamos con yt-dlp
        if not success:
            success = await loop.run_in_executor(
                pool, download_ytdlp, url, filename, chat_id
            )
    
    return success

async def upload_file(client: Client, file_path: str, chat_id: int, message: Message) -> bool:
    try:
        file_size = os.path.getsize(file_path)
        progress_data[chat_id]["uploaded"] = 0
        progress_data[chat_id]["upload_total"] = file_size
        
        # Crear thumbnail para videos
        thumb_path = ""
        if is_media_file(file_path):
            thumb_name = f"{Path(file_path).stem}.jpg"
            thumb_path = os.path.join(THUMB_PATH, thumb_name)
            if not create_thumbnail(file_path, thumb_path):
                thumb_path = ""
        
        # Obtener metadatos
        metadata = {}
        if is_media_file(file_path):
            metadata = get_video_metadata(file_path)
        
        caption = f"**Archivo subido:** `{Path(file_path).name}`"
        if metadata:
            duration = int(float(metadata["duration"]))
            mins, secs = divmod(duration, 60)
            hours, mins = divmod(mins, 60)
            size_mb = int(metadata["size"]) / (1024 * 1024)
            
            caption += (
                f"\n**Duración:** `{hours:02d}:{mins:02d}:{secs:02d}`"
                f"\n**Tamaño:** `{size_mb:.2f} MB`"
                f"\n**Resolución:** `{metadata['width']}x{metadata['height']}`"
            )
        
        await client.send_document(
            chat_id=chat_id,
            document=file_path,
            thumb=thumb_path,
            caption=caption,
            progress=upload_progress,
            progress_args=(chat_id,)
        )
        return True
    except RPCError as e:
        logger.error(f"Upload error: {e}")
        return False

async def upload_progress(current: int, total: int, chat_id: int):
    progress_data[chat_id]["uploaded"] = current
    progress_data[chat_id]["upload_total"] = total

async def progress_reporter(chat_id: int, message: Message):
    while user_tasks[chat_id]:
        try:
            data = progress_data.get(chat_id, {})
            
            if "total" in data and data["total"] > 0:
                down_percent = (data.get("downloaded", 0) / data["total"]) * 100
                down_text = (
                    f"⬇️ **Descarga:** "
                    f"{data.get('downloaded', 0)/(1024*1024):.1f}MB/"
                    f"{data['total']/(1024*1024):.1f}MB "
                    f"({down_percent:.1f}%)"
                )
            else:
                down_text = "⬇️ **Descarga:** Iniciando..."
            
            if "upload_total" in data and data["upload_total"] > 0:
                up_percent = (data.get("uploaded", 0) / data["upload_total"]) * 100
                up_text = (
                    f"⬆️ **Subida:** "
                    f"{data.get('uploaded', 0)/(1024*1024):.1f}MB/"
                    f"{data['upload_total']/(1024*1024):.1f}MB "
                    f"({up_percent:.1f}%)"
                )
            else:
                up_text = "⬆️ **Subida:** Esperando..."
            
            text = f"{down_text}\n{up_text}"
            
            try:
                await message.edit_text(text)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except RPCError:
                pass
            
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"Progress reporter error: {e}")
            break

@app.on_message(filters.text & filters.private)
async def handle_message(client: Client, message: Message):
    user_id = message.from_user.id
    if user_tasks[user_id]:
        await message.reply("⚠️ Ya tienes una tarea en curso. Espera a que termine.")
        return

    url, custom_name = parse_message(message.text)
    if not url:
        await message.reply("❌ Por favor envía un enlace válido")
        return

    user_tasks[user_id] = True
    progress_msg = await message.reply("🔄 Iniciando descarga...")
    os.makedirs(f"{DOWNLOAD_PATH}/{user_id}", exist_ok=True)
    
    # Iniciar reportero de progreso
    asyncio.create_task(progress_reporter(user_id, progress_msg))
    
    try:
        # Descargar archivo
        filename = custom_name or os.path.basename(url)
        file_path = os.path.join(DOWNLOAD_PATH, str(user_id), filename)
        
        success = await download_file(url, file_path, user_id)
        if not success or not os.path.exists(file_path):
            await progress_msg.edit_text("❌ Error en la descarga")
            return
        
        # Procesar archivos grandes
        files_to_upload = [file_path]
        if os.path.getsize(file_path) > MAX_SIZE:
            await progress_msg.edit_text("✂️ Partiendo archivo...")
            files_to_upload = split_file(file_path, os.path.dirname(file_path))
        
        # Subir archivos
        await progress_msg.edit_text("⬆️ Iniciando subida...")
        for file in files_to_upload:
            await upload_file(client, file, user_id, message)
            os.remove(file)
        
        await progress_msg.delete()
        await message.reply("✅ ¡Tarea completada con éxito!")
        
    except Exception as e:
        logger.exception("Error en la tarea:")
        await progress_msg.edit_text(f"❌ Error: {str(e)}")
    finally:
        user_tasks[user_id] = False
        progress_data.pop(user_id, None)
        # Limpiar archivos temporales
        for f in os.listdir(f"{DOWNLOAD_PATH}/{user_id}"):
            os.remove(os.path.join(f"{DOWNLOAD_PATH}/{user_id}", f))

if __name__ == "__main__":
    app.run()
