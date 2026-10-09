#!/usr/bin/env python3
"""Публикация записей: out/ -> public/ + index.html. Идемпотентно."""
import os, re, subprocess as sp, datetime, sys

OUT = "/matrix/dev/matrix-e2ee-recorder/out"
PUB = "/matrix/dev/matrix-e2ee-recorder/public"
os.makedirs(PUB, exist_ok=True)
QUIET = os.environ.get("QUIET") == "1"

def log(*a):
    if not QUIET: print(*a, flush=True)

def run(cmd):
    return sp.run(cmd, capture_output=True, text=True, timeout=900)

def probe(f, args):
    r = run(["ffprobe", "-v", "error"] + args + [f])
    return r.stdout.strip() if r.returncode == 0 else ""

def dur(f):
    v = probe(f, ["-show_entries", "format=duration", "-of", "default=nk=1:nw=1"])
    try: return float(v)
    except: return 0.0

# файлы, в которые ffmpeg пишет прямо сейчас
busy = set()
r = run(["docker", "exec", "recorder-p44", "sh", "-c", "ps ax | grep '[f]fmpeg'"])
busy = set(re.findall(r"/out/(rec-\d+\.mp4)", r.stdout))
if busy: log("занято сейчас:", ", ".join(sorted(busy)))

for name in sorted(os.listdir(OUT)):
    if not (name.startswith("rec-") and name.endswith(".mp4")): continue
    src, base = os.path.join(OUT, name), name[:-4]
    web = os.path.join(PUB, base + "_web.mp4")

    if name in busy:
        log(f"· {base} — запись идёт, пропуск"); continue
    if os.path.exists(web) and os.path.getmtime(web) >= os.path.getmtime(src):
        continue
    sz = os.path.getsize(src)
    if sz < 1_000:
        log(f"· {base} — мал ({sz} б), пропуск"); continue
    d = dur(src)
    if d <= 0:
        log(f"✗ {base} — не читается (нужен SIGINT: pkill -2 -f ffmpeg)"); continue

    log(f"→ {base}  {d:.1f} с  {sz/1048576:.1f} МБ")
    if run(["ffmpeg","-y","-v","error","-i",src,"-c","copy",
            "-movflags","+faststart",web]).returncode != 0:
        log("  ремукс не удался"); continue
    mp3 = os.path.join(PUB, base + "_audio.mp3")
    if not os.path.exists(mp3):
        run(["ffmpeg","-y","-v","error","-i",src,"-vn",
             "-c:a","libmp3lame","-q:a","4",mp3])
    jpg = os.path.join(PUB, base + "_frame.jpg")
    if not os.path.exists(jpg):
        run(["ffmpeg","-y","-v","error","-ss",str(max(1.0,d/2)),
             "-i",src,"-frames:v","1",jpg])
    log("  опубликовано")

# ---------- index.html ----------
items = sorted([f for f in os.listdir(PUB) if f.endswith("_web.mp4")], reverse=True)
rows = []
for f in items:
    p, base = os.path.join(PUB, f), f[:-8]
    d = dur(p)
    a = probe(p, ["-select_streams","a","-show_entries","stream=codec_name",
                  "-of","default=nk=1:nw=1"]).split("\n")[0]
    v = probe(p, ["-select_streams","v:0","-show_entries","stream=width,height",
                  "-of","csv=p=0:s=x"]).split("\n")[0]
    mm = f"{int(d//60)}:{int(d%60):02d}"
    sz = f"{os.path.getsize(p)/1048576:.1f} МБ"
    m = re.search(r"(\d{13})", base)
    ts = (datetime.datetime.fromtimestamp(int(m.group(1))/1000)
          .strftime("%d.%m.%Y %H:%M:%S") if m else base)
    ok = bool(a)
    badge = (f'<span class=ok>звук {a}</span>' if ok
             else '<span class=no>ЗВУКА НЕТ</span>')
    mp3 = (f' · <a href="{base}_audio.mp3" download>только звук</a>'
           if os.path.exists(os.path.join(PUB, base+"_audio.mp3")) else "")
    rows.append(f"""<div class=card>
<h3>{ts}</h3>
<p class=meta>{mm} · {v} · {sz} · {badge} · <code>{base}</code></p>
<video controls preload=metadata poster="{base}_frame.jpg">
<source src="{f}" type="video/mp4"></video>
<p><a href="{f}" download>скачать видео</a>{mp3}</p></div>""")

html = f"""<!doctype html><html lang=ru><meta charset=utf-8>
<meta http-equiv=refresh content=30>
<title>Записи E2EE-звонков</title>
<style>
body{{background:#14161a;color:#e8e8e8;
 font:15px/1.7 system-ui,-apple-system,sans-serif;
 max-width:940px;margin:36px auto;padding:0 20px}}
h2{{font-weight:600}} h3{{margin:0 0 6px;font-size:17px}}
video{{width:100%;border-radius:10px;background:#000;margin:8px 0}}
.meta{{color:#9aa0a8;font-size:13px;margin:0 0 8px}}
.ok{{color:#5ec27a}} .no{{color:#e06c6c}}
.card{{border-top:1px solid #2a2d33;padding-top:20px;margin-top:28px}}
a{{color:#7cc4ff;text-decoration:none}} a:hover{{text-decoration:underline}}
code{{background:#22252b;padding:2px 6px;border-radius:4px;font-size:12px}}
.upd{{color:#6b7078;font-size:12px;margin-top:40px}}
</style>
<h2>Записи звонков Matrix Element Call (E2EE)</h2>
<p class=meta>записей: {len(items)}</p>
{"".join(rows) if rows else "<p>Записей пока нет.</p>"}
<p class=upd>обновлено {datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</p>
</html>"""

open(os.path.join(PUB, "index.html"), "w", encoding="utf-8").write(html)
log(f"index.html: {len(items)} записей")
