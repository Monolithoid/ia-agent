FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget curl libgl1 libglib2.0-0 libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# PyTorch CPU uniquement — évite les ~4 Go de dépendances CUDA
RUN pip install --no-cache-dir \
    torch==2.1.2 torchvision==0.16.2 \
    --index-url https://download.pytorch.org/whl/cpu

ARG A1111_VERSION=v1.10.1
RUN git clone --depth 1 --branch ${A1111_VERSION} \
    https://github.com/AUTOMATIC1111/stable-diffusion-webui.git /app

WORKDIR /app

# Pré-installer les dépendances Python pour accélérer le démarrage
RUN pip install --no-cache-dir -r requirements_versions.txt

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
