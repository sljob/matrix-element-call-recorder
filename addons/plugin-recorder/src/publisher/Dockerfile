# Publisher: needs ffmpeg/ffprobe to produce the MP3 track and the poster
# frame, so this is the one image that is not slim-slim.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY publish.py .

ARG PLUGIN_VERSION=0.1.0
ENV PLUGIN_VERSION=${PLUGIN_VERSION}

# No HTTP surface: this container only writes files, so no EXPOSE and no
# port mapping. It never touches the network.
CMD ["python", "-u", "publish.py"]
