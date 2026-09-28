"""Read-only inventory shared by preview and execution. No shell output as readiness."""
import json
import shlex

from ghm.manifests import ModelAsset

INVENTORY = r'''
import hashlib,json,os,pathlib,shutil,socket,sys
c=json.load(sys.stdin)
root=pathlib.Path(c['root'])
comfy=pathlib.Path(c['comfy']).resolve()
modelroot=(comfy/'models').resolve()
if str(root.resolve()) != str(root):
    raise RuntimeError('Installation root must not contain symlinks')
parent=root
while not parent.exists(): parent=parent.parent
disk=shutil.disk_usage(parent)
for name in ['ComfyUI','venv','llm-venv','tts-venv','service-models']:
    if not (root/name).resolve().is_relative_to(root):
        raise RuntimeError('Environment symlink escapes installation root')
items=[]
for item in c['files']:
    allowed=modelroot if item['area']=='models' else root
    path=allowed/item['relative']
    if not path.resolve().is_relative_to(allowed):
        raise RuntimeError('Model symlink escapes installation root')
    state='missing'
    if path.exists():
        state='conflict'
        if path.is_file() and path.stat().st_size==item['size_bytes']:
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
s=socket.socket(); port_busy=s.connect_ex(('127.0.0.1',c['port']))==0; s.close()
print(json.dumps({'files':items,'free_bytes':disk.free,'filesystem_path':str(parent),
 'writable':os.access(parent,os.W_OK),'comfy_exists':(comfy/'main.py').is_file(),'comfy_path':str(comfy),'volumes':list(volumes.values()),
 'managed':(root/'.studio-owned').is_file(),'root_nonempty':root.exists() and any(root.iterdir()),
 'port_busy':port_busy,'python':list(sys.version_info[:3]),'architecture':os.uname().machine,
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
    result = await executor.run_input('python3 -c ' + shlex.quote(INVENTORY),
                                      json.dumps({'root': options.root, 'comfy': options.comfy_root, 'port': options.remote_port,
                                                  'files': file_inventory(lock)}), timeout=1800)
    if result.rc:
        raise ValueError('Không đọc được thư mục/volume hoặc gặp symlink ngoài vùng cài. Không thay đổi dữ liệu.')
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('SSH không trả inventory hợp lệ; chưa thay đổi môi trường.') from None


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
    if report['python'][:2] < [3, 11] or report['python'][:2] > [3, 12]:
        blockers.append('Bộ cài hiện yêu cầu Python 3.11 hoặc 3.12.')
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
            'valid_files': sum(f['state'] == 'valid' for f in report['files']), 'blockers': blockers,
            'inventory': report}
