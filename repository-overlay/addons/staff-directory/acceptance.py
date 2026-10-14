import json, sys, time, subprocess
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, '/opt/matrix-staff-directory')
from staff_directory import API, Directory, MATRIX, enc
from credentials import publisher_config
cfg=publisher_config(json.loads(Path('/etc/matrix-staff-directory/config.json').read_text()))
if cfg.get('server_directory_version') not in ('0.1.5-cloud-candidate','0.1.6-staff'):
    raise SystemExit('STOP: ожидалось установленное encrypted-credentials дополнение.')
target=sys.argv[3]
if not target.startswith('@') or target.split(':',1)[-1]!=cfg['bot_user_id'].split(':',1)[1]:
    raise SystemExit('Use a local full Matrix ID')
mode, seconds=sys.argv[1], int(sys.argv[2])
api=API(cfg['homeserver'],cfg['access_token'],cfg.get('ca_file'),timeout=8)
worker=Directory(cfg)
folder=Path(cfg['state_dir'])
window=int(cfg.get('device_activity_window_seconds',600))
deadline=time.monotonic()+seconds
print('Этап:',mode,'; максимальное ожидание:',seconds,'сек.',flush=True)
while True:
    try:
        if subprocess.run(['systemctl','is-active','--quiet','matrix-staff-directory.service']).returncode:
            raise SystemExit('FAIL: издатель остановлен.')
        members=api.call('GET',worker.prefix+'/joined_members')['joined']
        if target not in members:
            raise SystemExit('FAIL: User3 отсутствует в Сотрудниках.')
        busy,ok,unknown=worker.calls(members)
        if not ok or not cfg.get('livekit',{}).get('url'):
            raise RuntimeError('Проверка LiveKit не подтверждена')
        snapshot=json.loads((folder/'devices.json').read_text())
        if not 0<=time.time()-snapshot['checked_at']<=180:
            raise RuntimeError('Collector snapshot stale')
        entry=snapshot['users'][target]
        stamp,error=entry['stamp'],entry['error']
        if error:
            raise RuntimeError('Collector device query failed')
        state=json.loads((folder/'state.json').read_text())
        eid=state.get('last_edit_event_id')
        if not eid:
            raise RuntimeError('Нет ID опубликованного события')
        event=api.call('GET',worker.prefix+'/event/'+enc(eid))
        if event.get('sender')!=cfg['bot_user_id']:
            raise RuntimeError('Неверный отправитель события')
        content=event['content'].get('m.new_content',event['content'])
        rows=[s for s in content.get('body','').splitlines() if '('+target+')' in s]
        if len(rows)!=1:
            raise RuntimeError('Нет единственной строки User3')
        line=rows[0]
        now=time.time()
        age=None if stamp is None else now-stamp
        publication_age=now-event.get('origin_server_ts',0)/1000
        fresh_publication=0<=publication_age<=1200
        in_call=target in busy
        passed=False
        if mode in ('recent','after_call'):
            passed=(not in_call and age is not None and 0<=age<=window
                    and 'Недавно в сети — устройство' in line)
        elif mode=='call':
            passed=in_call and 'Занят — видеоконференция' in line
        elif mode=='offline':
            passed=(not in_call and age is not None and age>window
                    and 'Не в сети (по Matrix)' in line)
        print('Возраст обращения:',round(age) if age is not None else 'нет данных',
              '| В звонке:',in_call,'|',line,flush=True)
        if passed and fresh_publication:
            print('PASS:',mode,flush=True)
            break
    except Exception as exc:
        # Do not print response bodies, credentials or full URLs.
        print('Проверка пока не завершена:',type(exc).__name__,flush=True)
    if time.monotonic()>=deadline:
        raise SystemExit('FAIL/TIMEOUT: '+mode+'. Настройки не менялись; Cloud не обновлять.')
    time.sleep(30)