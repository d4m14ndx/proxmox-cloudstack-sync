FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV SYNC_CONFIG=/app/config.json
ENV PYTHONPATH=/app/backend

EXPOSE 8088

CMD ["python", "backend/main.py"]
