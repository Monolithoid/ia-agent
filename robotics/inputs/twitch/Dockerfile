FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY twitch_input.py twitch_chat.py ./

CMD ["python3", "/app/twitch_input.py"]
