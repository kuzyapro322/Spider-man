
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

RUN useradd --create-home appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY domain.py service.py app.py ./

USER appuser

ENV PORT=8000
ENV LOG_LEVEL=info
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn --workers 1 --bind 0.0.0.0:${PORT} --log-level ${LOG_LEVEL} app:app"]
