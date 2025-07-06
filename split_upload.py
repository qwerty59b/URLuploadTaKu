import os
import subprocess
import asyncio
import math
from kunigram import Client
from kunigram.types import Message

async def split_and_upload(client: Client, message: Message, progress_msg: Message, file_path: str):
    """Divide archivos grandes y los sube a Telegram"""
    try:
        # Tamaño de parte (1990 MB)
        PART_SIZE = 1990 * 1024 * 1024
        
        # Crear directorio temporal
        split_dir = "/tmp/split_files"
        os.makedirs(split_dir, exist_ok=True)
        
        # Limpiar directorio
        for f in os.listdir(split_dir):
            os.remove(os.path.join(split_dir, f))
        
        # Obtener información del archivo
        file_size = os.path.getsize(file_path)
        base_name = os.path.basename(file_path)
        num_parts = math.ceil(file_size / PART_SIZE)
        
        await progress_msg.edit(f"🔪 Dividiendo archivo en {num_parts} partes...")
        
        # Dividir archivo
        with open(file_path, 'rb') as f:
            part_num = 1
            while True:
                chunk = f.read(PART_SIZE)
                if not chunk:
                    break
                
                part_name = f"{base_name}.part{part_num:03d}"
                part_path = os.path.join(split_dir, part_name)
                
                with open(part_path, 'wb') as part_file:
                    part_file.write(chunk)
                
                part_num += 1
        
        # Subir partes
        parts = sorted(os.listdir(split_dir))
        total_parts = len(parts)
        
        await progress_msg.edit(f"📦 Subiendo {total_parts} partes...")
        
        for i, part in enumerate(parts):
            part_path = os.path.join(split_dir, part)
            await progress_msg.edit(f"⬆️ Subiendo parte {i+1}/{total_parts}...")
            
            await client.send_document(
                chat_id=message.chat.id,
                document=part_path,
                disable_notification=True
            )
            
            os.remove(part_path)
        
        await progress_msg.edit("✅ Todos los fragmentos subidos correctamente")
    
    except Exception as e:
        await progress_msg.edit(f"❌ Error: {str(e)}")
    finally:
        # Limpiar archivo original
        if os.path.exists(file_path):
            os.remove(file_path)
