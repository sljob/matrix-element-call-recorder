# Portal: the recordings archive. Reads index.json written by the publisher
# and re-authorizes every file request against Synapse; a copied link is not
# a capability. Needs curl+requests only, but keeps ffmpeg out of the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY index.py .

ARG PLUGIN_VERSION=0.1.0
ENV PLUGIN_VERSION=${PLUGIN_VERSION}

EXPOSE 8899
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/healthz', timeout=4).status==200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:8899", "--workers", "2", "--threads", "8", "--timeout", "120", "index:app"]
