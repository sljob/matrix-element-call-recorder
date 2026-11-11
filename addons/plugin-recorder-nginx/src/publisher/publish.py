#!/usr/bin/env python3
"""
Publisher: turns a finished recording into what the portal can list.

The worker leaves ``<OUTPUT_DIR>/<room>/rec-*.mp4`` behind. Raw MP4 is not
publishable as-is: the archive needs a stable name, a poster frame, a
downloadable audio track and an index that the portal can read without
scanning the disk on every request.

Responsibilities, in order:

  1. wait until the file stops growing AND ffprobe can read a duration
     (a file that just got its ``moov`` atom is still unwritable for a moment);
  2. probe streams -- a recording without an audio or a video stream is
     reported, not silently published as a broken player;
  3. extract MP3 and a JPG poster with FFmpeg;
  4. move everything into ``<PUBLIC_DIR>/<room>/`` atomically;
  5. rewrite ``<PUBLIC_DIR>/index.json`` for the portal.

Nothing here deletes source files: the worker directory is the fallback if
publication is interrupted.
"""

import json
import os
import shutil
import subprocess
import sys
import time

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", "/public")
DOMAIN = os.environ.get("DOMAIN", "")
LANG = os.environ.get("INSTALL_LANGUAGE", "ru")
POLL = float(os.environ.get("POLL_INTERVAL", "5"))
STABLE_FOR = float(os.environ.get("STABLE_SECONDS", "10"))
INDEX_LOCK = os.path.join(PUBLIC_DIR, ".index.lock")


def log(msg):
    sys.stdout.write("%s [publisher] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def ffprobe(path):
    """Return (duration_seconds, [codec_types]) or (None, []) if unreadable."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            return None, []
        data = json.loads(p.stdout or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0)
        types = [s.get("codec_type") for s in data.get("streams") or []]
        return (duration or None), types
    except (subprocess.SubprocessError, ValueError, OSError):
        return None, []


def wait_until_final(path):
    """
    A file is publishable when its size is unchanged for STABLE_FOR seconds
    and ffprobe reports a duration. The size check alone is not enough:
    FFmpeg writes the moov atom at the very end, and a copy taken between
    the last frame and that atom produces an unplayable file.
    """
    last = -1
    stable_since = None
    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, []
        if size != last:
            last, stable_since = size, time.time()
        elif stable_since and time.time() - stable_since >= STABLE_FOR:
            duration, types = ffprobe(path)
            if duration:
                return duration, types
        time.sleep(POLL)


def safe(name):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def extract(mp4, target_dir, base):
    """MP3 + JPG. Failures are logged, not fatal: a video without a poster is
    still a usable recording."""
    mp3 = os.path.join(target_dir, base + ".mp3")
    jpg = os.path.join(target_dir, base + ".jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4,
                    "-vn", "-acodec", "libmp3lame", "-q:a", "4", mp3],
                   capture_output=True, timeout=1800)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1",
                    "-i", mp4, "-frames:v", "1", "-q:v", "3", jpg],
                   capture_output=True, timeout=300)
    return (mp3 if os.path.isfile(mp3) else None,
            jpg if os.path.isfile(jpg) else None)


def read_index():
    path = os.path.join(PUBLIC_DIR, "index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def write_index(entries):
    """Write through a temp file: the portal may be reading while we write."""
    path = os.path.join(PUBLIC_DIR, "index.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def publish(mp4):
    room = os.path.basename(os.path.dirname(mp4))
    duration, types = wait_until_final(mp4)
    if not duration:
        log("SKIP %s: ffprobe cannot read a duration (no moov yet or broken file)" % mp4)
        return False

    base = os.path.splitext(os.path.basename(mp4))[0]
    target = os.path.join(PUBLIC_DIR, safe(room))
    os.makedirs(target, exist_ok=True)

    final_mp4 = os.path.join(target, base + ".mp4")
    shutil.move(mp4, final_mp4)
    os.chmod(final_mp4, 0o644)

    mp3, jpg = extract(final_mp4, target, base)
    has_audio = "audio" in types
    has_video = "video" in types
    if not has_audio:
        log("WARN %s has no audio stream -- check the pulse sink in the worker" % base)
    if not has_video:
        log("WARN %s has no video stream -- check x11grab / Xvfb" % base)

    entry = {
        "room_id": room,
        "file": "%s/%s.mp4" % (safe(room), base),
        "mp3": ("%s/%s.mp3" % (safe(room), base)) if mp3 else None,
        "jpg": ("%s/%s.jpg" % (safe(room), base)) if jpg else None,
        "bytes": os.path.getsize(final_mp4),
        "duration": round(duration, 1),
        "has_audio": has_audio,
        "has_video": has_video,
        "published_at": int(time.time()),
    }

    entries = [e for e in read_index() if e.get("file") != entry["file"]]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("published_at", 0), reverse=True)
    write_index(entries)
    log("published %s (%ds, %s bytes, audio=%s video=%s)"
        % (entry["file"], entry["duration"], entry["bytes"], has_audio, has_video))
    return True


def already_published(mp4):
    room = safe(os.path.basename(os.path.dirname(mp4)))
    base = os.path.splitext(os.path.basename(mp4))[0]
    return os.path.isfile(os.path.join(PUBLIC_DIR, room, base + ".mp4"))


def main():
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    log("watching %s (public %s)" % (OUTPUT_DIR, PUBLIC_DIR))
    seen = set()
    while True:
        try:
            for room in sorted(os.listdir(OUTPUT_DIR)):
                room_dir = os.path.join(OUTPUT_DIR, room)
                if not os.path.isdir(room_dir):
                    continue
                for name in sorted(os.listdir(room_dir)):
                    if not name.endswith(".mp4"):
                        continue
                    mp4 = os.path.join(room_dir, name)
                    if mp4 in seen or already_published(mp4):
                        continue
                    # a file still being written must not enter `seen`
                    if publish(mp4):
                        seen.add(mp4)
        except OSError as exc:
            log("scan error: %s" % exc)
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
