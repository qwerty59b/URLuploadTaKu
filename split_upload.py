import os
import asyncio
import logging
import subprocess
import time
from pyrogram import Client
from pyrogram.types import Message

logger = logging.getLogger(__name__)

# Acceso a active_tasks desde bot.py (necesario para cancelaciones)
try:
    from bot import active_tasks
except ImportError:
    active_tasks = {}

async def split_upload_progress(current, total, progress_msg, task_id, part_index, total_parts):
    """Callback para mostrar progreso de subida de partes divididas"""
    current_time = time.time()
    if not hasattr(split_upload_progress, 'last_update'):
        split_upload_progress.last_update = {}
    
    if task_id not in split_upload_progress.last_update:
        split_upload_progress.last_update[task_id] = {}
    
    # Inicializar para esta parte si es necesario
    if part_index not in split_upload_progress.last_update[task_id]:
        split_upload_progress.last_update[task_id][part_index] = 0
    
    # Actualizar cada 15 segundos
    if current_time - split_upload_progress.last_update[task_id][part_index] > 15:
        split_upload_progress.last_update[task_id][part_index] = current_time
        percent = current * 100 / total
        
        # Calcular tamaño en MB
        current_mb = current // (1024 * 1024)
        total_mb = total // (1024 * 1024)
        
        message = (
            f"[{task_id}] ⬆️ Subiendo parte {part_index}/{total_parts}\n"
            f"📊 {percent:.1f}% - {current_mb}MB/{total_mb}MB"
        )
        try:
            await progress_msg.edit(message)
        except Exception as e:
            logger.error(f"Error actualizando progreso: {str(e)}")

async def split_and_upload(client: Client, message: Message, progress_msg: Message, file_path: str, task_id: str):
    try:
        # Mensaje actualizado
        await progress_msg.edit(f"[{task_id}] 🔪 Dividiendo archivo...")
            return
        
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
        await progress_msg.edit(f"[{task_id}] 🔪 Dividiendo archivo con 7z (sin compresión)...")
        
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
        
        await progress_msg.edit(f"[{task_id}] 📦 Dividido en {len(parts)} partes. Subiendo...")
        
        # Subir cada parte
        for i, part in enumerate(parts):
            # Verificar si la tarea fue cancelada
            if task_id in active_tasks and active_tasks[task_id].cancelled:
                break
                
            part_path = os.path.join(split_dir, part)
            
            # Crear callback de progreso para esta parte
            async def progress_callback(current, total):
                await split_upload_progress(
                    current, total, 
                    progress_msg, task_id, 
                    i+1, len(parts)
                )
            
            await progress_msg.edit(f"[{task_id}] ⬆️ Subiendo parte {i+1}/{len(parts)} ({part})...")
            
            await client.send_document(
                chat_id=message.chat.id,
                document=part_path,
                disable_notification=True,
                progress=progress_callback
            )
            
            os.remove(part_path)
        
        if task_id in active_tasks and not active_tasks[task_id].cancelled:
            await progress_msg.edit(f"[{task_id}] ✅ Todos los fragmentos subidos correctamente")
    
    except Exception as e:
        logger.error(f"Error en split_and_upload: {str(e)}", exc_info=True)
        await progress_msg.edit(f"[{task_id}] ❌ Error: {str(e)}")
    finally:
        # Limpiar archivo original
        if os.path.exists(file_path):
            os.remove(file_path)
