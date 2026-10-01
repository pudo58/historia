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


_CACHE: dict[str, tuple[int, dict]] = {}


def _cached(path: Path) -> dict | None:
    """Read a pack file once per modification (a rich pack is hundreds of KB; projects poll often)."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    pack = _read(path)
    if pack is None:
        return None
    pack = {**pack, 'version': version(pack)}
    _CACHE[key] = (stamp, pack)
    return pack


def load(root: Path, pack_id: str, slim: bool = False) -> dict | None:
    """The pack with its version. ``slim`` drops the fact base (kept as per-category counts) for UI/job snapshots."""
    if not ID.match(pack_id or ''):
        return None
    mine = _cached(_user_dir(root) / f'{pack_id}.json')
    pack, builtin = (mine, False) if mine else (_cached(BUILTIN / f'{pack_id}.json'), True)
    if not pack:
        return None
    pack = {**pack, 'builtin': builtin}
    if slim:
        facts = pack.pop('facts', None) or {}
        pack['fact_counts'] = {k: len(v) for k, v in facts.items()}
    return pack


def listing(root: Path) -> list[dict]:
    ids = {p.stem for p in BUILTIN.glob('*.json')} | {p.stem for p in _user_dir(root).glob('*.json')}
    found = [load(root, i, slim=True) for i in sorted(ids)]
    return [{'id': p['id'], 'name': p['name'], 'period': p.get('period', ''), 'builtin': p['builtin'],
             'status': p.get('status', ''), 'version': p['version']} for p in found if p]


def save(root: Path, pack: dict) -> dict:
    clean = {k: v for k, v in pack.items() if k not in {'builtin', 'version', 'fact_counts'}}
    if not ID.match(clean.get('id', '')):
        raise ValueError('Mã gói chỉ gồm chữ thường, số và dấu gạch ngang.')
    if 'facts' not in clean:  # the editor does not carry the fact base; keep the one already there
        existing = load(root, clean['id'])
        if existing and existing.get('facts'):
            clean['facts'] = existing['facts']
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


_CAPS = {'events': 14, 'people': 10, 'titles_offices': 8, 'places': 6, 'architecture_objects': 6, 'customs_institutions': 6}
_LABELS = {'events': 'su_kien', 'people': 'nhan_vat', 'titles_offices': 'chuc_quan_danh_hieu', 'places': 'dia_danh',
           'architecture_objects': 'kien_truc_vat_dung', 'customs_institutions': 'phong_tuc_che_do',
           'dress_appearance': 'trang_phuc_dung_mao'}
_DRESS_BUDGET = 6000
_TOTAL_BUDGET = 12000


def _grams(text: str) -> set[str]:
    words = re.findall(r'\w+', _norm(text))
    return {f'{a} {b}' for a, b in zip(words, words[1:])}


def _line(fact: dict) -> str:
    year = f" [{fact['year']}]" if fact.get('year') else ''
    ref = f" ({fact['ref']})" if fact.get('ref') else ''
    return f"{fact.get('title', '')}{year}: {fact.get('text', '')}{ref}"


def relevant_facts(pack: dict, query: str) -> dict[str, list[str]]:
    """Fact lines of the pack ranked against ``query`` (topic, outline or chapter text), within a size budget."""
    facts = pack.get('facts') or {}
    wanted = _grams(query)
    years = set(re.findall(r'\b1[2-4]\d\d\b', query))
    out: dict[str, list[str]] = {}
    used = 0

    def rank(items):
        scored = []
        for index, fact in enumerate(items):
            score = len(wanted & _grams(f"{fact.get('title', '')} {fact.get('text', '')}"))
            if fact.get('year') and str(fact['year']) in years:
                score += 3
            scored.append((-score, index, fact))
        return [f for _, _, f in sorted(scored, key=lambda t: (t[0], t[1]))]

    dress = rank(facts.get('dress_appearance', []))
    lines, size = [], 0
    for fact in dress:
        line = _line(fact)
        if size + len(line) > _DRESS_BUDGET:
            break
        lines.append(line)
        size += len(line)
    if lines:
        out[_LABELS['dress_appearance']] = lines
        used += size
    for key, cap in _CAPS.items():
        picked = []
        for fact in rank(facts.get(key, [])):
            if len(picked) >= cap:
                break
            hit = wanted & _grams(f"{fact.get('title', '')} {fact.get('text', '')}")
            if wanted and not hit and picked:
                break
            line = _line(fact)
            if used + len(line) > _TOTAL_BUDGET:
                break
            picked.append(line)
            used += len(line)
        if picked:
            out[_LABELS[key]] = picked
    return out


def for_writer(pack: dict | None, query: str = '') -> dict | None:
    """What the script/outline model is told (Vietnamese facts, visual bible, things to avoid, relevant book facts)."""
    if not pack:
        return None
    brief = {'ten': pack['name'], 'giai_doan': pack.get('period', ''), 'kien_thuc': pack.get('script', ''),
             'visual_bible': [pack.get('visual', '')] + [f"{r['name']}: {r['text']}" for r in pack.get('roles', [])],
             'tranh': pack.get('avoid', '')}
    facts = relevant_facts(pack, query) if pack.get('facts') else {}
    if facts:
        brief['tu_lieu_sach'] = facts
    return brief
