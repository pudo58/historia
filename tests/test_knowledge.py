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
    from types import SimpleNamespace
    me = SimpleNamespace(service=SimpleNamespace(root=tmp_path))
    text = StudioJobs.knowledge_instruction(me, {'knowledge': {'id': 'tran'}, 'topic': 'Trần Hưng Đạo'})
    assert 'Quan gia' in text and 'visual_bible' in text and 'Forbidden City' in text
    assert StudioJobs.knowledge_instruction(me, {'knowledge': None}) == ''


def test_rich_pack_is_slim_in_projects_and_facts_are_retrieved_by_relevance(tmp_path):
    full = knowledge.load(tmp_path, 'tran')
    slim = knowledge.load(tmp_path, 'tran', slim=True)
    assert 'facts' not in slim and slim['version'] == full['version']
    assert sum(slim['fact_counts'].values()) == sum(len(v) for v in full['facts'].values()) > 500
    synthetic = {'name': 'X', 'facts': {
        'events': [{'title': 'Hội nghị Diên Hồng', 'text': 'Vua hỏi các bô lão nên hòa hay đánh', 'ref': 'q.V', 'year': 1284},
                   {'title': 'Lập Quốc tử viện', 'text': 'Dạy con quan học', 'ref': 'q.V', 'year': 1253}],
        'dress_appearance': [{'title': 'Áo', 'text': 'Áo bào đỏ', 'ref': 'q.VI', 'year': 1300}]}}
    picked = knowledge.relevant_facts(synthetic, 'Hội nghị Diên Hồng năm 1284')
    assert picked['su_kien'][0].startswith('Hội nghị Diên Hồng') and 'trang_phuc_dung_mao' in picked
    brief = knowledge.for_writer(synthetic and {**synthetic, 'name': 'X'}, 'Diên Hồng')
    assert 'tu_lieu_sach' in brief
    assert len(str(knowledge.for_writer(full, 'Trần Hưng Đạo kháng chiến chống Nguyên Mông'))) < 30000
    # saving from the editor (no facts in the body) keeps the fact base
    saved = knowledge.save(tmp_path, {k: full[k] for k in ('id', 'name', 'period', 'script', 'visual', 'roles')})
    assert sum(len(v) for v in knowledge.load(tmp_path, 'tran')['facts'].values()) == sum(len(v) for v in full['facts'].values())
    assert saved['id'] == 'tran'
