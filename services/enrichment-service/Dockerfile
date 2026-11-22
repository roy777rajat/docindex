# Build from the repo root:  docker build -f services/enrichment-service/Dockerfile -t cleandocs-enrichment .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY shared /app/shared
RUN pip install /app/shared
COPY services/enrichment-service/requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
COPY services/enrichment-service/enrichment_service /app/enrichment_service
USER nobody
CMD ["sh", "-c", "uvicorn enrichment_service.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
