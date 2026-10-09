
import json
import tempfile
os.umask(0o022)

def _publish_metadata(base):
    source = os.path.join(OUT, base + ".meta")
    if not os.path.isfile(source):
        source = os.path.join(OUT, base + ".mp4.meta")
    if not os.path.isfile(source):
        return

    with open(source, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Invalid metadata: " + source)
    room = data.get("room_id")
    if not isinstance(room, str) or not room.startswith("!"):
        raise ValueError("Invalid metadata room: " + source)

    destination = os.path.join(PUB, base + ".meta")
    if os.path.isfile(destination):
        with open(destination, encoding="utf-8") as handle:
            current = json.load(handle)
        if not isinstance(current, dict):
            raise ValueError("Invalid published metadata: " + destination)
        current_room = current.get("room_id")
        if current_room and current_room != room:
            raise ValueError("Conflicting room IDs: " + destination)
        if current_room == room:
            # Сохраняем более полные данные Controller.
            os.chmod(destination, 0o644)
            return

    fd, temporary = tempfile.mkstemp(
        prefix=".recording-meta-", dir=PUB
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

