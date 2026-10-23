import json,sys,time
from pathlib import Path
sys.path.insert(0,'/opt/matrix-staff-directory')
from staff_directory import API,MATRIX,enc,atomic
from credentials import credential
cfg=json.loads(Path('/etc/matrix-staff-directory/config.json').read_text())
api=API(cfg['homeserver'],credential('bot'),cfg.get('ca_file'),timeout=10)
base=MATRIX+'/rooms/'+enc(cfg['room_id'])
path=base+'/state/m.room.power_levels/'
backup=Path(sys.argv[2]);mode=sys.argv[1]
if mode=='apply':
    old=api.call('GET',path)
    bot=cfg['bot_user_id']
    if old.get('users',{}).get(bot,old.get('users_default',0))!=100:
        raise SystemExit('STOP: bot must have PL100; room unchanged')
    if any(not isinstance(v,int) or v>100 for v in old.get('users',{}).values()):
        raise SystemExit('STOP: unexpected room levels; unchanged')
    admin=API(cfg['homeserver'],credential('admin'),cfg.get('ca_file'),timeout=10)
    admin_id=admin.call('GET',MATRIX+'/account/whoami')['user_id']
    peers=[u for u,v in old.get('users',{}).items() if v==100 and u!=bot]
    if any(u!=admin_id for u in peers):
        raise SystemExit('STOP: another PL100 user exists; no automatic demotion')
    intermediate=json.loads(json.dumps(old))
    if admin_id in peers:intermediate['users'][admin_id]=99
    new=json.loads(json.dumps(intermediate))
    new['events_default']=100
    for typ in list(new.get('events',{})):new['events'][typ]=100
    new.setdefault('events',{}).update({'m.room.message':100,'m.reaction':100,'m.sticker':100,'m.room.encrypted':100})
    new['users_default']=min(new.get('users_default',0),99)
    atomic(backup/'room-before.json',old)
    atomic(backup/'room-intermediate.json',intermediate)
    atomic(backup/'room-after.json',new)
    if api.call('GET',path)!=old:raise SystemExit('STOP: concurrent room permission change')
    if intermediate!=old:
        admin.call('PUT',path,intermediate)
        if api.call('GET',path)!=intermediate:raise SystemExit('Self-demotion readback failed')
    api.call('PUT',path,new)
    if api.call('GET',path)!=new:raise SystemExit('Room permissions readback failed')
    print('ROOM OK: only bot PL100; messages/reactions require PL100. Administrator PL99 retains invitations.')
elif mode=='rollback':
    if (backup/'room-before.json').exists():
        current=api.call('GET',path);old=json.loads((backup/'room-before.json').read_text());new=json.loads((backup/'room-after.json').read_text())
        intermediate=json.loads((backup/'room-intermediate.json').read_text())
        if current==new or current==intermediate:api.call('PUT',path,old);print('Room permissions restored')
        elif current!=old:raise SystemExit('Concurrent permissions change: automatic room rollback refused')
elif mode=='verify':
    levels=api.call('GET',path)
    if levels!=json.loads((backup/'room-after.json').read_text()):raise SystemExit('Permission mismatch')
    state=json.loads((Path(cfg['state_dir'])/'state.json').read_text())
    ev=api.call('GET',base+'/event/'+enc(state['last_edit_event_id']))
    content=ev['content'].get('m.new_content',ev['content'])
    if ev['sender']!=cfg['bot_user_id'] or 'последнее обращение:' not in content.get('body',''):
        raise SystemExit('Updated publication not confirmed')
    if not 0<=time.time()-ev['origin_server_ts']/1000<180:raise SystemExit('Stale publication')
    snapshot=json.loads((Path(cfg['state_dir'])/'devices.json').read_text())
    if not 0<=time.time()-snapshot['checked_at']<180:raise SystemExit('Stale collector')
    if any('kind' not in v for v in snapshot['users'].values()):raise SystemExit('Old collector format')
    print('VERIFY OK: actual room permissions, fresh collector and bot publication')
    for line in content.get('body','').splitlines():
        if 'последнее обращение:' in line:print(line)
