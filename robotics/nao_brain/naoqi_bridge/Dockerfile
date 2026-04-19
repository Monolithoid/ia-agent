# ──────────────────────────────────────────────────────────────────────────────
# naoqi_bridge — Python 2.7 Flask HTTP bridge to NAO via qi SDK
#
# The NAOqi Python SDK (qi module) is Python 2.7 only for NAOqi ≤ 2.1.x.
# This image provides the runtime environment; the SDK itself must be provided:
#
#   Option A (recommended):
#     Download "pynaoqi-python2.7-2.1.4.*-linux64.tar.gz" from the SoftBank
#     developer portal and extract it to /opt/ia/nao/naoqi-sdk/ on the VPS.
#     The docker-compose volume mount will make it available at /opt/naoqi-sdk.
#
#   Option B (mock/test without robot):
#     Leave NAO_IP unset — the bridge runs in mock mode (logs commands, no NAO).
#
# PYTHONPATH is pre-configured to /opt/naoqi-sdk/lib/python2.7/site-packages
# which is where the SDK installs the qi module.
# ──────────────────────────────────────────────────────────────────────────────
FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/opt/naoqi-sdk/lib/python2.7/site-packages \
    BRIDGE_PORT=8090

RUN apt-get update && apt-get install -y --no-install-recommends \
        python2.7 \
        python2-dev \
        curl \
    && curl https://bootstrap.pypa.io/pip/2.7/get-pip.py -o /tmp/get-pip.py \
    && python2.7 /tmp/get-pip.py \
    && rm /tmp/get-pip.py \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip2 install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8090
CMD ["python2.7", "app.py"]
