#!/usr/bin/env bash
# entrypoint.sh — Télécharge DreamShaper 8 au premier démarrage, puis lance A1111

set -euo pipefail

MODEL_DIR="/app/models/Stable-diffusion"
MODEL_FILE="${MODEL_DIR}/dreamshaper_8.safetensors"
# DreamShaper 8 — SD 1.5, ~2.1 Go, non censuré
MODEL_URL="https://huggingface.co/Lykon/DreamShaper/resolve/main/DreamShaper_8_pruned.safetensors"

mkdir -p "$MODEL_DIR"

if [ ! -f "$MODEL_FILE" ]; then
    echo "[A1111] Téléchargement DreamShaper 8 (~2.1 Go)…"
    wget -q --show-progress --no-check-certificate \
        -O "${MODEL_FILE}.tmp" "$MODEL_URL" \
    && mv "${MODEL_FILE}.tmp" "$MODEL_FILE" \
    || { echo "[A1111] ERREUR : échec du téléchargement du modèle." >&2; rm -f "${MODEL_FILE}.tmp"; exit 1; }
    echo "[A1111] Modèle téléchargé : ${MODEL_FILE}"
else
    echo "[A1111] Modèle déjà présent : ${MODEL_FILE}"
fi

echo "[A1111] Démarrage (mode CPU)…"
exec python launch.py \
    --use-cpu all \
    --no-half \
    --precision full \
    --skip-torch-cuda-test \
    --api \
    --listen \
    --port 7860 \
    --skip-install
