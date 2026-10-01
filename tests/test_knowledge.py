from fastapi.testclient import TestClient

from studio import knowledge
from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.jobs import StudioJobs
from studio.schemas import ProjectInput


def app(tmp_path):
    made = create_app(Settings(database_url=f'sqlite:///{tmp_path / "k.db"}', studio_root=tmp_path / 'data'),
                      SecretStore('k'), lambda h, s: FakeExecutor())
    return TestClient(made), made.state.studio


def test_builtin_tran_pack_loads_and_every_claim_has_a_source(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    assert pack['builtin'] and pack['name'] == 'Nhà Trần' and len(pack['version']) == 12
    assert pack['sources'] and all(s['claim'] and s['ref'] for s in pack['sources'])
    assert pack['visual'] and pack['avoid'] and pack['roles']


def test_role_lines_are_added_only_when_the_scene_mentions_that_role(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    court = knowledge.visual_for_scene(pack, {'visual_prompt': 'A king hands a seal to a child.', 'narration': ''})
    assert 'yellow robe' in court and 'ding' not in court and 'tattoo' not in court
    field = knowledge.visual_for_scene(pack, {'visual_prompt': 'Soldiers march at dawn.', 'narration': 'Quân ta xuất trận.'})
    assert 'tattoo' in field and 'yellow robe' not in field
    assert knowledge.visual_for_scene(None, {'visual_prompt': 'x'}) == ''


def test_saving_makes_a_user_copy_that_overrides_the_builtin_and_changes_the_version(tmp_path):
    original = knowledge.load(tmp_path, 'tran')
    saved = knowledge.save(tmp_path, {**original, 'visual': 'Edited bible.'})
    assert not saved['builtin'] and saved['visual'] == 'Edited bible.' and saved['version'] != original['version']
    assert knowledge.load(tmp_path, 'tran')['visual'] == 'Edited bible.'
    assert knowledge.load(tmp_path, '../etc') is None


def test_api_lists_edits_and_a_project_carries_its_pack(tmp_path):
    client, service = app(tmp_path)
    assert [p['id'] for p in client.get('/api/studio/knowledge').json()] == ['tran']
    pack = client.get('/api/studio/knowledge/tran').json()
    body = {k: pack[k] for k in ('id', 'name', 'period', 'status', 'script', 'visual', 'roles', 'avoid', 'sources')}
    assert client.put('/api/studio/knowledge/ly', json=body).status_code == 409          # id must match the path
    assert client.put('/api/studio/knowledge/tran', json={**body, 'visual': 'New look.'}).json()['visual'] == 'New look.'
    project = service.create_project(ProjectInput(title='P', topic='t', knowledge_id='tran'))
    assert service.project(project['id'])['knowledge']['visual'] == 'New look.'
    bare = service.create_project(ProjectInput(title='B', topic='t'))
    assert service.project(bare['id'])['knowledge'] is None


def test_writer_instruction_names_the_rules_and_is_empty_without_a_pack(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    text = StudioJobs.knowledge_instruction({'knowledge': pack})
    assert 'Quan gia' in text and 'visual_bible' in text and 'Forbidden City' in text
    assert StudioJobs.knowledge_instruction({'knowledge': None}) == ''
