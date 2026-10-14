"""Safe data-only answers selection. No source/eval; no tokens in answers."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE=Path(__file__).resolve().parent

def read_answers(path):
    values={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if not re.match(r'^[A-Z][A-Z0-9_]*=',line):raise ValueError('Invalid answers syntax')
        key,value=line.split('=',1)
        if key in values:raise ValueError('Duplicate answers key')
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
        values[key]=value
    enabled=values.get('STAFF_DIRECTORY_ENABLED','false')
    if enabled not in ('true','false'):raise ValueError('STAFF_DIRECTORY_ENABLED must be true or false')
    return enabled=='true',values

def check_platform():
    if sys.version_info<(3,10):raise ValueError('Staff directory requires Python >=3.10')
    for tool in ('systemctl','systemd-creds','systemd-run','systemd-analyze'):
        if not shutil.which(tool):raise ValueError('Staff directory missing tool: '+tool)
    p=subprocess.run(['systemctl','--version'],capture_output=True,text=True,check=True)
    if int(p.stdout.split()[1])<250:raise ValueError('Staff directory requires systemd >=250')
    for path in ('/opt/matrix-staff-directory','/etc/matrix-staff-directory','/var/lib/matrix-staff-directory'):
        if Path(path).exists():raise ValueError('Staff addon already exists; main installer is not an upgrade tool')

def main():
    if len(sys.argv)!=3 or sys.argv[1] not in ('--check','--install'):raise ValueError('Usage: --check|--install answers.conf')
    enabled,values=read_answers(sys.argv[2])
    if not enabled:
        print('STAFF_DIRECTORY_ENABLED=false: addon skipped')
        return 0
    check_platform()
    domain=values.get('DOMAIN','');stack=values.get('INSTALL_DIR','/opt/element-stack')
    if not domain or not Path(stack).is_absolute():raise ValueError('Staff addon needs DOMAIN and absolute INSTALL_DIR')
    if sys.argv[1]=='--check':
        print('Staff addon selected; platform requirements OK. Token requested only during installation.')
        return 0
    env=os.environ.copy();env['STAFF_INSTALL_DOMAIN']=domain;env['STAFF_INSTALL_STACK']=stack
    return subprocess.run(['bash',str(HERE/'install.sh')],env=env).returncode

if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('Staff selection stopped:',str(exc),file=sys.stderr);sys.exit(1)
