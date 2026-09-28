"""Prompt-correlated measurements, never equate polling time with GPU execution."""
import math
from statistics import median


def execution_seconds(history, prompt_id):
    times = {}
    for event in history.get('status', {}).get('messages', []):
        if not isinstance(event, (list, tuple)) or len(event) != 2:
            continue
        name, data = event
        if not isinstance(data, dict) or data.get('prompt_id') != prompt_id:
            continue
        timestamp = data.get('timestamp')
        if isinstance(timestamp, (int, float)) and math.isfinite(timestamp):
            times[name] = timestamp
    if 'execution_start' in times and 'execution_success' in times:
        elapsed = (times['execution_success'] - times['execution_start']) / 1000
        return elapsed if elapsed >= 0 else None
    return None


def estimate(submissions, configs, hourly_usd=None):
    """Only extrapolate compatible successful shots; cold first shots are excluded."""
    from studio.service import canonical_hash
    groups = {}
    completed = 0
    execution = []
    for item in submissions:
        if item.get('state') != 'downloaded':
            continue
        completed += 1
        seconds = item.get('timing', {}).get('comfy_execution_seconds')
        if isinstance(seconds, (int, float)) and seconds >= 0:
            execution.append(seconds)
        seconds = item.get('timing', {}).get('total_seconds')
        if seconds is not None and not item.get('cold_candidate') and item.get('config') and item.get('attempts', 1) == 1:
            key = canonical_hash({'config': item['config'], 'runtime': item.get('runtime', {})})
            groups.setdefault(key, []).append(seconds)
    remaining, eta, gpu_eta = len(configs), 0.0, 0.0
    for config, runtime in configs:
        samples = groups.get(canonical_hash({'config': config, 'runtime': runtime}))
        if not samples:
            eta = None
            break
        seconds = median(samples)
        eta += seconds
        if config.get('motion') not in {'static', 'kenburns'}:
            gpu_eta += seconds
    return {'completed_shots': completed, 'remaining_shots': remaining,
            'median_shot_seconds': median([v for values in groups.values() for v in values]) if groups else None,
            'median_comfy_seconds': median(execution) if execution else None,
            'eta_seconds': eta, 'estimated_remaining_usd': gpu_eta / 3600 * hourly_usd
            if eta is not None and hourly_usd is not None else None,
            'billing_basis': 'processing_estimate_not_pod_rental'}
