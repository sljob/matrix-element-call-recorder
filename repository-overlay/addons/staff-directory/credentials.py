import json
import os
from pathlib import Path

def credential(name):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise ValueError('Must run via systemd encrypted credentials')
    return (Path(directory) / name).read_text().strip()

def publisher_config(cfg):
    cfg = dict(cfg)
    cfg['access_token'] = credential('bot')
    cfg['livekit'] = dict(cfg['livekit'])
    cfg['livekit'].update(json.loads(credential('livekit')))
    return cfg
