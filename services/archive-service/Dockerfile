# Build from the repo root:  docker build -f services/archive-service/Dockerfile -t cleandocs-archive .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY shared /app/shared
RUN pip install /app/shared
COPY services/archive-service/requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
COPY services/archive-service/archive_service /app/archive_service
USER nobody
CMD ["sh", "-c", "uvicorn archive_service.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
