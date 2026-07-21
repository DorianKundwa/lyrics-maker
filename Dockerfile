FROM python:3.11-slim

# System dependencies your app actually needs at runtime/build time:
# - ffmpeg: video/audio rendering (README asks for libass + libx264, both are
#   compiled into Debian's ffmpeg package)
# - espeak + libespeak-dev: aeneas uses espeak for forced alignment
# - build-essential: aeneas ships a C extension that compiles on install
# - libsndfile1: needed by several audio-processing Python libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak \
    espeak-data \
    libespeak-dev \
    build-essential \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# whisperx and demucs both pull in torch. Installed via requirements.txt
# alone, pip grabs PyPI's default CUDA-enabled wheels — several GB of
# CUDA runtime that a GPU-less Coolify VPS will never use. Installing the
# CPU-only build first means the requirements.txt install below just sees
# the version constraint already satisfied and skips the CUDA build.
RUN pip install --no-cache-dir torch~=2.8.0 torchaudio~=2.8.0 torchvision~=0.23.0 \
    --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# app.py creates these on startup too, but pre-creating them means a
# Coolify persistent-storage mount has somewhere to attach
RUN mkdir -p uploads outputs alignments

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
