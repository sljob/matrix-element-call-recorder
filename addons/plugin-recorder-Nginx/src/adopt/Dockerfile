# Adopt proxy: no dependencies beyond the standard library, so the image is
# tiny and cannot drift from a package index.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY proxy.py .

ARG PLUGIN_VERSION=0.1.0
ENV PLUGIN_VERSION=${PLUGIN_VERSION}

EXPOSE 8765 8899
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4).status==200 else 1)"

CMD ["python", "-u", "proxy.py"]
