import json
import time
from pathlib import Path
from staff_directory import API, MATRIX, enc, atomic
from credentials import credential
from device_origin import last_device

cfg=json.loads(Path('/etc/matrix-staff-directory/config.json').read_text())
admin=API(cfg['homeserver'],credential('admin'),cfg.get('ca_file'),timeout=8)
bot=API(cfg['homeserver'],credential('bot'),cfg.get('ca_file'),timeout=8)
while True:
    start=time.monotonic()
    try:
        members=bot.call('GET',MATRIX+'/rooms/'+enc(cfg['room_id'])+'/joined_members')['joined']
        domain=cfg['bot_user_id'].split(':',1)[1]
        users={}
        for uid in members:
            if uid==cfg['bot_user_id'] or uid in cfg.get('exclude_users',[]) or uid.split(':',1)[-1]!=domain: continue
            stamp,error,kind=None,False,'не определено'
            try:
                data=admin.call('GET','/_synapse/admin/v2/users/'+enc(uid)+'/devices')
                if data.get('total',len(data['devices']))>len(data['devices']): raise ValueError('Incomplete')
                stamp,kind=last_device(data['devices'])
            except Exception: error=True
            users[uid]={'stamp':stamp,'error':error,'kind':kind}
        atomic(Path(cfg['state_dir'])/'devices.json',{'checked_at':time.time(),'users':users})
    except Exception:
        print('Device collector update failed; snapshot will expire.',flush=True)
    time.sleep(max(1,15-(time.monotonic()-start)))
