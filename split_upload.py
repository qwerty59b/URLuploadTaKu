import os
import asyncio
import logging
import subprocess
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

async def split_and_upload(client: Client, message: Message, progress_msg: Message, file_path: str, task_id: str):
    """Divide archivos grandes usando 7z (sin compresión) y sube a Telegram"""
    try:
        # Crear directorio temporal
        split_dir = "/tmp/split_files"
        os.makedirs(split_dir, exist_ok=True)
        
        # Limpiar directorio
        for f in os.listdir(split_dir):
            file_path_to_remove = os.path.join(split_dir, f)
            if os.path.isfile(file_path_to_remove):
                os.remove(file_path_to_remove)
        
        # Nombre base para archivos divididos
        base_name = os.path.basename(file_path)
        archive_path = os.path.join(split_dir, f"{base_name}.7z")
        
        # Crear archivo 7z sin compresión (modo almacenamiento)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])
        await progress_msg.edit(f"[{task_id}] 🔪 Dividiendo archivo con 7z (sin compresión)...", reply_markup=keyboard)
        
        # Usar binario 7z directamente
        cmd = [
            "7z",
            "a",
            f"-v{1990}m",  # Volúmenes de 1990MB
            "-mx0",        # Sin compresión
            archive_path,
            file_path
        ]
        
        logger.info(f"Ejecutando comando: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Error al dividir: {result.stderr}")
            await progress_msg.edit(f"[{task_id}] ❌ Error al dividir el archivo")
            return
        
        # Obtener partes generadas
        parts = sorted([f for f in os.listdir(split_dir) if f.startswith(f"{base_name}.7z.")])
        
        if not parts:
            # Si no se generaron partes, usar el archivo único
            if os.path.exists(archive_path):
                parts = [os.path.basename(archive_path)]
        
        if not parts:
            await progress_msg.edit(f"[{task_id}] ❌ No se generaron partes")
            return
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])
        await progress_msg.edit(f"[{task_id}] 📦 Dividido en {len(parts)} partes. Subiendo...", reply_markup=keyboard)
        
        # Subir cada parte
        for i, part in enumerate(parts):
            part_path = os.path.join(split_dir, part)
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]])
            await progress_msg.edit(f"[{task_id}] ⬆️ Subiendo parte {i+1}/{len(parts)} ({part})...", reply_markup=keyboard)
            
            await client.send_document(
                chat_id=message.chat.id,
                document=part_path,
                disable_notification=True
            )
            
            os.remove(part_path)
        
        await progress_msg.edit(f"[{task_id}] ✅ Todos los fragmentos subidos correctamente")
    
    except asyncio.CancelledError:
        logger.info(f"Tarea {task_id} cancelada durante división/subida")
        await progress_msg.edit(f"[{task_id}] ❌ Operación cancelada")
    except Exception as e:
        logger.error(f"Error en split_and_upload: {str(e)}", exc_info=True)
        await progress_msg.edit(f"[{task_id}] ❌ Error: {str(e)}")
    finally:
        # Limpiar archivo original
        if os.path.exists(file_path):
            os.remove(file_path)
