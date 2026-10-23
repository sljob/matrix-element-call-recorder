import json
import time
from pathlib import Path

class DeviceSnapshot:
    def __init__(self,cfg):
        self.path=Path(cfg['state_dir'])/'devices.json'
    def status(self,uid,presence,busy,call_ok,enabled,normal):
        if busy or presence in ('online','unavailable'):
            return normal(presence,busy,call_ok,enabled)
        try:
            data=json.loads(self.path.read_text())
            now=time.time()
            if not 0 <= now-data['checked_at'] <= 180:
                raise ValueError('Stale collector')
            entry=data['users'][uid]
            if entry['error']: raise ValueError('Device API error')
            ts=entry['stamp']
            if ts is None: raise ValueError('No activity')
            if ts>now+30: raise ValueError('Clock')
            if now-ts<=600:
                result=('🔵','Недавно в сети — устройство (до 10 мин)',3)
            elif presence=='offline':
                result=('⚪','Не в сети (по Matrix)',4)
            else:
                result=('❔','Нет данных о присутствии',5)
        except (OSError,ValueError,KeyError,TypeError):
            result=('❔','Статус устройства недоступен',5)
        icon,text,order=result
        if enabled and not call_ok: text+=' · статус звонка недоступен'
        return icon,text,order
