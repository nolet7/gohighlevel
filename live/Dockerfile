# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HGE_DATABASE_PATH=/data/engine.db
RUN useradd --system --uid 10001 --home /app app && mkdir /data && chown app /data
COPY --from=build /install /usr/local
USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status==200 else 1)"
# Single worker on purpose: SQLite + in-process scheduler. Scale out by moving to Postgres (see docs/adr/0002).
CMD ["uvicorn", "hvac_engine.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
