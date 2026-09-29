"""Read-only RunPod Pod monitoring. Never stops, starts or terminates a Pod."""
from datetime import datetime, timezone

import httpx

PODS_URL = 'https://rest.runpod.io/v1/pods'


class RunPodError(Exception):
    """Message is safe to show the user (never contains the API key)."""


async def fetch_pods(api_key: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(PODS_URL, headers={'Authorization': f'Bearer {api_key}'})
    except httpx.HTTPError:
        raise RunPodError('Không kết nối được RunPod. Kiểm tra mạng rồi thử lại.') from None
    if response.status_code in (401, 403):
        raise RunPodError('RunPod từ chối API key. Tạo key mới (quyền đọc) tại Settings → API Keys.')
    if response.status_code >= 400:
        raise RunPodError(f'RunPod trả lỗi {response.status_code}.')
    data = response.json()
    return data if isinstance(data, list) else []


def _uptime(started: str | None, now: datetime) -> int | None:
    if not started:
        return None
    try:
        when = datetime.fromisoformat(started.replace('Z', '+00:00').replace(' +0000 UTC', '+00:00'))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((now - when).total_seconds()))


def summarize(pods: list[dict], hosts: list[dict], now: datetime | None = None) -> dict:
    """hosts: [{'id','label','address','port'}]. Matches a Pod to a Historia host by SSH ip:port."""
    now = now or datetime.now(timezone.utc)
    result = []
    for pod in pods:
        ports = pod.get('portMappings') or {}
        ssh_port = ports.get('22')
        ip = pod.get('publicIp')
        host = next((h for h in hosts if ip and ssh_port and h['address'] == ip and int(h['port']) == int(ssh_port)), None)
        status = pod.get('desiredStatus') or 'UNKNOWN'
        running = status == 'RUNNING'
        gpu = pod.get('gpu') or {}
        cost = pod.get('costPerHr') if isinstance(pod.get('costPerHr'), (int, float)) else None
        seconds = _uptime(pod.get('lastStartedAt'), now) if running else None
        result.append({
            'id': pod.get('id'), 'name': pod.get('name'), 'status': status,
            'gpu': gpu.get('displayName'), 'gpu_count': gpu.get('count'),
            'vcpu': pod.get('vcpuCount'), 'memory_gb': pod.get('memoryInGb'),
            'container_disk_gb': pod.get('containerDiskInGb'), 'volume_gb': pod.get('volumeInGb'),
            'cost_per_hr': cost, 'uptime_seconds': seconds,
            'session_cost': cost * seconds / 3600 if cost is not None and seconds is not None else None,
            'public_ip': ip, 'ssh_port': ssh_port,
            'host_id': host['id'] if host else None, 'host_label': host['label'] if host else None,
            'ssh_ready': bool(running and ip and ssh_port),
        })
    running = [p for p in result if p['status'] == 'RUNNING']
    return {'pods': result, 'running_count': len(running),
            'running_cost_per_hr': sum(p['cost_per_hr'] or 0 for p in running),
            'stopped_disk_note': 'Pod đã dừng vẫn tính phí lưu trữ ổ đĩa nhỏ; xóa Pod nếu không dùng nữa.'}
