# ──────────────────────────────────────────────────────────────────────────────
# naoqi_bridge — Flask HTTP bridge between the VPS and the NAO robot.
#
# Operates in robot-initiated WebSocket mode (rosbridge) — the qi SDK is not
# used. Mock mode (no NAO_IP) logs commands without connecting.
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.12-alpine

ENV BRIDGE_PORT=8090

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8090
CMD ["python", "app.py"]
