"""Read-only inventory shared by preview and execution. No shell output as readiness."""
import json
import shlex

from ghm.manifests import ModelAsset
from ghm.model_download import CACHE_HELPERS, VERIFIED_CACHE

INVENTORY = r'''
import hashlib,json,os,pathlib,shutil,socket,subprocess,sys
c=json.load(sys.stdin)
def inside(path,base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
def version_of(exe):
    try:
        out=subprocess.run([exe,'-c','import sys;print("%d.%d.%d"%sys.version_info[:3])'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,universal_newlines=True,timeout=20).stdout.strip()
        return out if out.count('.')==2 else None
    except Exception:
        return None
__CACHE__
root=pathlib.Path(c['root'])
cache={} if c.get('recheck') else cache_load(root/c['cache'])
comfy=pathlib.Path(c['comfy']).resolve()
modelroot=(comfy/'models').resolve()
if str(root.resolve()) != str(root):
    raise RuntimeError('Installation root must not contain symlinks')
parent=root
while not parent.exists(): parent=parent.parent
disk=shutil.disk_usage(parent)
for name in ['ComfyUI','venv','llm-venv','tts-venv','service-models']:
    if not inside((root/name).resolve(),root):
        raise RuntimeError('Environment symlink escapes installation root')
items=[]
for item in c['files']:
    allowed=modelroot if item['area']=='models' else root
    path=allowed/item['relative']
    if not inside(path.resolve(),allowed):
        raise RuntimeError('Model symlink escapes installation root')
    state='missing'
    if path.exists():
        state='conflict'
        if path.is_file() and path.stat().st_size==item['size_bytes'] and cache_ok(cache,path.resolve(),item['digest']):
            state='valid'
        elif path.is_file() and path.stat().st_size==item['size_bytes']:
            h=hashlib.sha256() if item['algorithm']=='sha256' else hashlib.sha1()
            if item['algorithm']=='git-sha1': h.update(('blob '+str(path.stat().st_size)+'\0').encode())
            with path.open('rb') as f:
                for block in iter(lambda:f.read(8*1024*1024),b''): h.update(block)
            if h.hexdigest()==item['digest']: state='valid'
    items.append({**item,'state':state})
volumes={}
for area,folder in [('services',root),('models',modelroot)]:
    parent_path=folder
    while not parent_path.exists(): parent_path=parent_path.parent
    dev=str(parent_path.stat().st_dev)
    v=volumes.setdefault(dev,{'path':str(parent_path),'free_bytes':shutil.disk_usage(parent_path).free,'missing_bytes':0,'reserve_bytes':0,'writable':True})
    v['missing_bytes']+=sum(f['size_bytes'] for f in items if f['area']==area and f['state']!='valid')
    v['reserve_bytes']+=35*1024**3 if area=='services' else 512*1024**2
    v['writable']=v['writable'] and os.access(parent_path,os.W_OK)
pythons={}
for name in ['python3.12','python3.11']:
    exe=shutil.which(name)
    version=version_of(exe) if exe else None
    if version: pythons[name]=version
own=root/'python'/'.historia-python'
try:
    own_exe=own.read_text().strip() if own.is_file() else ''
except OSError:
    own_exe=''
if own_exe and os.path.isabs(own_exe) and os.access(own_exe,os.X_OK):
    version=version_of(own_exe)
    if version: pythons['historia']=version
s=socket.socket(); port_busy=s.connect_ex(('127.0.0.1',c['port']))==0; s.close()
print(json.dumps({'files':items,'free_bytes':disk.free,'filesystem_path':str(parent),
 'writable':os.access(parent,os.W_OK),'comfy_exists':(comfy/'main.py').is_file(),'comfy_path':str(comfy),'volumes':list(volumes.values()),
 'managed':(root/'.studio-owned').is_file(),'root_nonempty':root.exists() and any(root.iterdir()),
 'port_busy':port_busy,'python':list(sys.version_info[:3]),'pythons':pythons,'architecture':os.uname().machine,
 'tools':{k:bool(shutil.which(k)) for k in ['git','ffmpeg','espeak-ng','flock']}}))
'''


def file_inventory(lock):
    files = []
    for raw in lock['models']:
        a = ModelAsset.model_validate(raw)
        files.append({'name': a.name, 'area': 'models', 'relative': a.target('ComfyUI').removeprefix('ComfyUI/models/'),
                      'size_bytes': a.size_bytes, 'digest': a.sha256, 'algorithm': 'sha256'})
    for snapshot in lock['snapshots']:
        for f in snapshot['files']:
            files.append({'name': snapshot['name'], 'area': 'services', 'relative': 'service-models/' + snapshot['repo'] + '/' + f['filename'],
                          **{k: f[k] for k in ('size_bytes', 'digest', 'algorithm')}})
    return files


async def inventory(executor, options, lock):
    result = await executor.run_input('python3 -c ' + shlex.quote(INVENTORY.replace('__CACHE__', CACHE_HELPERS)),
                                      json.dumps({'root': options.root, 'cache': VERIFIED_CACHE,
                                                  'recheck': getattr(options, 'recheck_models', False), 'comfy': options.comfy_root, 'port': options.remote_port,
                                                  'files': file_inventory(lock)}), timeout=1800)
    if result.rc:
        raise ValueError('Không đọc được thư mục/volume hoặc gặp symlink ngoài vùng cài. Không thay đổi dữ liệu.')
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('SSH không trả inventory hợp lệ; chưa thay đổi môi trường.') from None


SUPPORTED_PYTHON = ([3, 11], [3, 12])


def python_notes(report, options):
    """Not a blocker: an unsuitable system Python is handled at install time (studio.python_env)."""
    if list(report['python'][:2]) in SUPPORTED_PYTHON:
        return []
    have = '.'.join(str(part) for part in report['python'])
    usable = [(name, version) for name, version in (report.get('pythons') or {}).items()
              if [int(x) for x in version.split('.')[:2]] in SUPPORTED_PYTHON]
    if usable:
        name, version = usable[0]
        where = 'đã cài trước đó bởi Historia' if name == 'historia' else name
        return [f'Python mặc định của Pod là {have} (cần 3.11/3.12). Bộ cài sẽ dùng Python {version} có sẵn ({where}); '
                'python3 của Pod không bị thay đổi.']
    return [f'Python mặc định của Pod là {have} (cần 3.11 hoặc 3.12). Khi cài, Historia tự tải Python 3.12 (~40 MB, bằng uv) '
            f'vào {options.root.rstrip("/")}/python, cạnh Python hệ thống; python3 của Pod không bị thay đổi. '
            'Cần Pod truy cập được pypi.org và github.com.']


def summarize(report, options):
    missing = sum(f['size_bytes'] for f in report['files'] if f['state'] != 'valid')
    # Explicit conservative allowance, not an exact claim about dependency sizes.
    reserve = sum(v['reserve_bytes'] for v in report['volumes']) if report.get('volumes') else 35 * 1024**3
    blockers = []
    if any(f['state'] == 'conflict' for f in report['files']):
        blockers.append('Có file trùng tên nhưng sai checksum. Không ghi đè; chọn thư mục riêng hoặc xử lý file trước.')
    if not report.get('volumes') and report['free_bytes'] < missing + reserve:
        blockers.append('Không đủ dung lượng trên filesystem đã chọn.')
    for volume in report.get('volumes', []):
        if volume['free_bytes'] < volume['missing_bytes'] + volume['reserve_bytes']:
            blockers.append('Không đủ dung lượng tại ' + volume['path'])
        if not volume['writable']:
            blockers.append('Không có quyền ghi tại ' + volume['path'])
    if not report['writable']:
        blockers.append('Thư mục không có quyền ghi.')
    notes = python_notes(report, options)
    if report['architecture'] != 'x86_64':
        blockers.append('Bộ cài này chỉ dành cho Linux x86_64.')
    if not report['tools'].get('flock'):
        blockers.append('Thiếu flock (util-linux) để khóa cài đặt an toàn.')
    if options.adopt_existing:
        if not report['comfy_exists'] or not report['port_busy']:
            blockers.append('Không tìm thấy ComfyUI ở đường dẫn/port đã chọn. Bấm kiểm tra tự động hoặc xem cấu hình nâng cao.')
    elif report['root_nonempty'] and not report['managed']:
        blockers.append('Thư mục đã có dữ liệu không do Studio quản lý. Chọn thư mục mới hoặc chế độ tái sử dụng.')
    elif report['port_busy'] and not report['managed']:
        blockers.append('Port đang được dịch vụ khác sử dụng. Chọn port riêng, ví dụ 8190.')
    service_required = sum(f['size_bytes'] for f in report['files'] if f['state'] != 'valid' and f.get('area') == 'services') + 35 * 1024**3
    return {'missing_bytes': missing, 'reserve_bytes': reserve, 'required_bytes': missing + reserve,
            'service_required_bytes': service_required if report.get('volumes') else missing + reserve,
            'valid_files': sum(f['state'] == 'valid' for f in report['files']), 'blockers': blockers, 'notes': notes,
            'inventory': report}
