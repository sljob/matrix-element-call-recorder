import re

def device_kind(device):
    # User-Agent is self-reported; display_name is intentionally ignored.
    ua=str(device.get('last_seen_user_agent') or '').lower()
    if any(x in ua for x in ('android','iphone','ipad','ipados','element x ios','element-x-ios','elementx-ios','element x android','element-x-android','elementx-android')):
        return 'мобильный'
    if any(x in ua for x in ('windows nt','macintosh','x11','cros','element-desktop','element desktop')):
        return 'ПК'
    return 'не определено'

def last_device(devices):
    valid=[d for d in devices if not d.get('dehydrated') and isinstance(d.get('last_seen_ts'),(int,float)) and not isinstance(d.get('last_seen_ts'),bool) and d['last_seen_ts']>0]
    if not valid:return None,'не определено'
    latest=max(d['last_seen_ts'] for d in valid)
    kinds={device_kind(d) for d in valid if d['last_seen_ts']==latest}
    kind=next(iter(kinds)) if len(kinds)==1 else ('несколько типов устройств' if 'не определено' not in kinds else 'не определено')
    return latest/1000,kind

def suffix(path,uid,now):
    import json
    try:
        data=json.loads(path.read_text())
        if not 0<=now-data['checked_at']<=180:return ' · последнее обращение: нет свежих данных'
        entry=data['users'][uid]
        if entry['error']:return ' · последнее обращение: нет данных'
        kind=entry.get('kind','не определено')
        if kind not in ('мобильный','ПК','несколько типов устройств'):kind='не определено'
        return ' · последнее обращение: '+kind
    except (OSError,ValueError,KeyError,TypeError):
        return ' · последнее обращение: нет данных'
