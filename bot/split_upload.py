import os
import subprocess
import asyncio
from kunigram import Client
from kunigram.types import Message

async def split_and_upload(client: Client, message: Message, progress_msg: Message, file_path: str):
    """Divide archivos grandes y los sube a Telegram"""
    try:
        # Crear directorio temporal
        split_dir = "/tmp/split_files"
        os.makedirs(split_dir, exist_ok=True)
        
        # Limpiar directorio
        for f in os.listdir(split_dir):
            os.remove(os.path.join(split_dir, f))
        
        # Nombre base para archivos divididos
        base_name = os.path.basename(file_path)
        split_prefix = os.path.join(split_dir, base_name)
        
        # Dividir archivo en partes de 2GB sin compresión
        split_cmd = [
            "split",
            "--bytes=2000m",
            "--numeric-suffixes",
            "--suffix-length=3",
            file_path,
            f"{split_prefix}."
        ]
        
        await progress_msg.edit("🔪 Dividiendo archivo...")
        subprocess.run(split_cmd, check=True)
        
        # Obtener partes generadas
        parts = sorted([f for f in os.listdir(split_dir) if f.startswith(base_name)])
        
        await progress_msg.edit(f"📦 Dividido en {len(parts)} partes. Subiendo...")
        
        # Subir cada parte
        for i, part in enumerate(parts):
            part_path = os.path.join(split_dir, part)
            await progress_msg.edit(f"⬆️ Subiendo parte {i+1}/{len(parts)}...")
            
            await client.send_document(
                chat_id=message.chat.id,
                document=part_path,
                disable_notification=True
            )
            
            os.remove(part_path)
        
        await progress_msg.edit("✅ Todos los fragmentos subidos correctamente")
    
    except Exception as e:
        await progress_msg.edit(f"❌ Error: {str(e)}")