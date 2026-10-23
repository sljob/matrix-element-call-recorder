"""Adjust only the installed directory room kick threshold; never remove users."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0,'/opt/matrix-staff-directory')
from staff_directory import API,MATRIX,enc,atomic
from credentials import credential

def updated(old,bot):
    if old.get('users',{}).get(bot)!=100:
        raise ValueError('Unexpected bot level; not modified')
    if old.get('events',{}).get('m.room.message',old.get('events_default',0))!=100:
        raise ValueError('Expected bot-only messages; not modified')
    if old.get('kick',50) not in (99,100):
        raise ValueError('Nonstandard kick threshold; not modified')
    new=copy.deepcopy(old);new['kick']=99
    return new

def main():
    mode=sys.argv[1];backup=Path(sys.argv[2])
    cfg=json.loads(Path('/etc/matrix-staff-directory/config.json').read_text())
    api=API(cfg['homeserver'],credential('bot'),cfg.get('ca_file'),timeout=10)
    path=MATRIX+'/rooms/'+enc(cfg['room_id'])+'/state/m.room.power_levels/'
    old=api.call('GET',path)
    if mode=='apply':
        new=updated(old,cfg['bot_user_id'])
        if new==old:print('OK: kick=99 already set; unchanged');return
        atomic(backup/'before.json',{'room_id':cfg['room_id'],'content':old})
        atomic(backup/'after.json',new)
        if api.call('GET',path)!=old:raise ValueError('Concurrent permission change')
        api.call('PUT',path,new)
        if api.call('GET',path)!=new:raise ValueError('Readback mismatch; backup saved')
        print('OK: kick100 -> 99; messages remain bot-only. Nobody removed.')
    elif mode=='restore':
        before=json.loads((backup/'before.json').read_text())
        after=json.loads((backup/'after.json').read_text())
        if before['room_id']!=cfg['room_id']:raise ValueError('Wrong room backup')
        if old==before['content']:print('Original rights already set');return
        if old!=after:raise ValueError('Rights changed externally; restore refused')
        api.call('PUT',path,before['content'])
        if api.call('GET',path)!=before['content']:raise ValueError('Restore readback mismatch')
        print('Original rights restored')
    else:raise ValueError('Unknown operation')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Never emit response bodies or secrets.
        print('STOP:',str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
        sys.exit(1)
