#!/usr/bin/env python3
"""Publish only a worker-finalized source. Preserve source, recover interrupted commits."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=1800)
    if result.returncode:
        raise RuntimeError('media command failed: ' + args[0])
    return result.stdout


def probe(path):
    data = json.loads(command(['ffprobe', '-v', 'error', '-show_format',
                               '-show_streams', '-of', 'json', str(path)]))
    duration = float(data.get('format', {}).get('duration', 0))
    streams = data.get('streams', [])
    if duration <= 0 or not any(s['codec_type'] == 'video' for s in streams):
        raise ValueError('source has no valid duration/video')
    return duration, streams


def publish(source, meta, public):
    source, public = Path(source), Path(public)
    # room IDs are stored verbatim as metadata; hashing is only a filesystem key.
    room_key = hashlib.sha256(meta['room_id'].encode()).hexdigest()
    target = public / room_key
    target.mkdir(parents=True, exist_ok=True)
    entry_file = target / (source.stem + '.json')
    if entry_file.is_file():
        try:
            old = json.loads(entry_file.read_text())
        except (ValueError, OSError):
            old = {}
        if old.get('file') and all((public / old[k]).is_file() for k in ('file',) + (('mp3',) if old.get('mp3') else ())):
            return old
    duration, streams = probe(source)
    audio = any(s['codec_type'] == 'audio' for s in streams)
    # Remux to regular MP4 + faststart, following the monolith publisher.
    # No re-encoding of already encoded H264/AAC; partial files stay outside public.
    with tempfile.TemporaryDirectory(prefix='.publishing-', dir=target) as tmp:
        tmp = Path(tmp)
        mp4 = tmp / (source.stem + '.mp4')
        command(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-map', '0:v:0',
                 '-map', '0:a:0?', '-c', 'copy', '-movflags', '+faststart', str(mp4)])
        probe(mp4)
        mp3 = tmp / (source.stem + '.mp3') if audio else None
        if mp3:
            command(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vn',
                     '-c:a', 'libmp3lame', '-q:a', '4', str(mp3)])
            if not mp3.stat().st_size:
                raise ValueError('empty audio file')
        jpg = tmp / (source.stem + '.jpg')
        command(['ffmpeg', '-y', '-v', 'error', '-ss', str(min(1, duration / 2)),
                 '-i', str(source), '-frames:v', '1', '-q:v', '3', str(jpg)])
        if not jpg.is_file():
            raise ValueError('poster missing')
        entry = dict(meta, duration=round(duration, 2), bytes=mp4.stat().st_size,
                     file=room_key + '/' + mp4.name,
                     mp3=room_key + '/' + mp3.name if mp3 else None,
                     jpg=room_key + '/' + jpg.name,
                     published_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        for item in (mp4, mp3, jpg):
            if item:
                item.chmod(0o644)
                os.replace(item, target / item.name)
        # Metadata is the commit marker. Readers never list uncommitted files.
        marker = tmp / entry_file.name
        marker.write_text(json.dumps(entry, ensure_ascii=False))
        marker.chmod(0o644)
        os.replace(marker, entry_file)
        return entry


def main():
    p = argparse.ArgumentParser()
    p.add_argument('source')
    p.add_argument('--public', default=os.environ.get('PUBLIC_DIR', '/public'))
    args = p.parse_args()
    marker = Path(args.source + '.done')
    meta = json.loads(marker.read_text())
    if meta.get('source_bytes') != Path(args.source).stat().st_size:
        raise ValueError('source differs from finalization marker')
    publish(args.source, meta, args.public)
    print('published', Path(args.source).name)


if __name__ == '__main__':
    main()
