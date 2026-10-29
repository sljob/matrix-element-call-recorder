# Controller: Flask API that fronts the recorder pool and is called by the
# record button through nginx (`/recording` prefix).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

# Reported by /health; the installer compares it with its own version so a
# stale image is visible instead of silently running old code.
ARG PLUGIN_VERSION=0.1.0
ENV PLUGIN_VERSION=${PLUGIN_VERSION}

RUN mkdir -p /output /public

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4).status==200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:8765", "--workers", "2", "--threads", "8", "--timeout", "120", "app:app"]
