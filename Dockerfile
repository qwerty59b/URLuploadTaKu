FROM python:3.9-slim

RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    wget \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-u", "main.py"]
