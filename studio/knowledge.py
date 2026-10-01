"""Dynasty knowledge packs: what a period looked like and how it spoke, injected into scripts and image prompts.

Built-in packs ship in ``studio/knowledge/*.json`` (read-only); a pack saved by the user lives in
``<studio_root>/knowledge/<id>.json`` and overrides the built-in one with the same id.
"""
import hashlib
import json
import re
from pathlib import Path

BUILTIN = Path(__file__).parent / 'knowledge'
ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')


def _canonical(pack: dict) -> str:
    return json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def version(pack: dict) -> str:
    return hashlib.sha256(_canonical(pack).encode()).hexdigest()[:12]


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and ID.match(str(data.get('id', ''))) else None


def _user_dir(root: Path) -> Path:
    return Path(root) / 'knowledge'


def load(root: Path, pack_id: str) -> dict | None:
    if not ID.match(pack_id or ''):
        return None
    mine = _read(_user_dir(root) / f'{pack_id}.json')
    if mine:
        return {**mine, 'builtin': False, 'version': version(mine)}
    built = _read(BUILTIN / f'{pack_id}.json')
    return {**built, 'builtin': True, 'version': version(built)} if built else None


def listing(root: Path) -> list[dict]:
    ids = {p.stem for p in BUILTIN.glob('*.json')} | {p.stem for p in _user_dir(root).glob('*.json')}
    found = [load(root, i) for i in sorted(ids)]
    return [{'id': p['id'], 'name': p['name'], 'period': p.get('period', ''), 'builtin': p['builtin'],
             'status': p.get('status', ''), 'version': p['version']} for p in found if p]


def save(root: Path, pack: dict) -> dict:
    clean = {k: v for k, v in pack.items() if k not in {'builtin', 'version'}}
    if not ID.match(clean.get('id', '')):
        raise ValueError('Mã gói chỉ gồm chữ thường, số và dấu gạch ngang.')
    folder = _user_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{clean['id']}.json"
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    return load(root, clean['id'])


def _norm(text: str) -> str:
    return (text or '').lower()


def visual_for_scene(pack: dict | None, scene: dict) -> str:
    """The pack's always-on visual bible plus the role lines whose keywords occur in the scene."""
    if not pack:
        return ''
    haystack = _norm(f"{scene.get('visual_prompt', '')} {scene.get('narration', '')} {scene.get('title', '')}")
    parts = [pack.get('visual', '').strip()]
    for role in pack.get('roles', []):
        if any(_norm(k) in haystack for k in role.get('keywords', []) if k):
            parts.append(role.get('text', '').strip())
    return ' '.join(p for p in parts if p)


def for_writer(pack: dict | None) -> dict | None:
    """What the script/outline model is told (Vietnamese facts, visual bible, things to avoid)."""
    if not pack:
        return None
    return {'ten': pack['name'], 'giai_doan': pack.get('period', ''), 'kien_thuc': pack.get('script', ''),
            'visual_bible': [pack.get('visual', '')] + [f"{r['name']}: {r['text']}" for r in pack.get('roles', [])],
            'tranh': pack.get('avoid', '')}
