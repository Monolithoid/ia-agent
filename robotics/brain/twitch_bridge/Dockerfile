# Contexte de build : ./robotics (pour inclure shared/speech_services.py)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY brain/twitch_bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/speech_services.py ./
COPY brain/twitch_bridge/twitch_bridge.py brain/twitch_bridge/twitch_chat.py brain/twitch_bridge/overlay.html ./

EXPOSE 8096
CMD ["python3", "/app/twitch_bridge.py"]
