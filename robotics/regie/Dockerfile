FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY regie.py regie.html ./

EXPOSE 8097
CMD ["python3", "/app/regie.py"]
