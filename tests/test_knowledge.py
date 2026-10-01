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
    assert 'topknot' in field and '殺韃' not in field and 'yellow robe' not in field
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


def test_auto_storyboard_gives_varied_shots_to_scenes_with_measured_audio(tmp_path, monkeypatch):
    from studio.models import Scene
    from studio.schemas import SceneInput
    from studio.storyboard import auto_shots
    client, service = app(tmp_path)
    project = service.create_project(ProjectInput(title='P', topic='t'))
    long_scene = service.add_scene(project['id'], SceneInput(title='Long', narration='Câu một. Câu hai dài hơn một chút. Câu ba. Câu bốn.', visual_prompt='A river'))
    short_scene = service.add_scene(project['id'], SceneInput(title='Short', narration='Một câu.', visual_prompt='A hall'))
    service.add_scene(project['id'], SceneInput(title='No audio', narration='Chưa có tiếng.', visual_prompt='A gate'))
    for scene, seconds in ((long_scene, 14.0), (short_scene, 3.0)):
        wav = service.root / f"{scene['id']}.wav"
        wav.write_bytes(b'x')
        artifact = service.artifact(wav, project['id'], wav.name, None)['id']
        with service.sessions() as session:
            row = session.get(Scene, scene['id'])
            row.data = {**row.data, 'speech_id': artifact, 'duration': seconds}
            session.commit()
    sizes = {long_scene['id']: 14.0, short_scene['id']: 3.0}
    monkeypatch.setattr('studio.media.probe', lambda path: {'duration': next(v for k, v in sizes.items() if k in str(path))})
    result = client.post(f"/api/studio/projects/{project['id']}/auto-storyboard")
    assert result.status_code == 200, result.text
    assert result.json()['updated'] == ['Long'] and set(result.json()['skipped']) == {'Short', 'No audio'}
    long_data = next(s for s in service.project(project['id'])['scenes'] if s['title'] == 'Long')
    shots = long_data['shot_list']
    assert len(shots) == 3 and long_data['image_strategy'] == 'per_shot'
    assert [s['shot_size'] for s in shots] == ['wide', 'medium', 'close']
    assert {s['direction'] for s in shots} == {'left-to-right'}
    assert client.post(f"/api/studio/projects/{project['id']}/auto-storyboard").json()['updated'] == []   # not redone
    assert len(auto_shots({'title': 'T', 'narration': 'A. B.'}, 5)) == 5


def test_pod_groups_lists_gpu_lanes_of_one_pod_in_order(tmp_path):
    import json
    from ghm.models import Host
    made = create_app(Settings(database_url=f'sqlite:///{tmp_path / "g.db"}', studio_root=tmp_path / 'data'),
                      SecretStore('k'), lambda h, s: FakeExecutor())
    client, hosts = TestClient(made), made.state.studio_jobs.hosts
    ids = []
    for label in ('Pod · GPU 0', 'Pod · GPU 1', 'Solo'):
        with made.state.studio.sessions() as session:
            row = Host(label=label, address='localhost', username='u', auth_kind='password', encrypted_secret='x',
                       pinned_fingerprint='f')
            session.add(row)
            session.commit()
            ids.append(row.id)
    for index, host_id in enumerate(ids[:2]):
        hosts.save_setting(f'host_lane:{host_id}', json.dumps({'pod_id': 'pod1', 'index': index, 'first': ids[0]}))
    groups = client.get('/api/studio/pod-groups').json()
    assert len(groups) == 1 and groups[0]['pod_id'] == 'pod1'
    assert [h['id'] for h in groups[0]['hosts']] == ids[:2]
    assert {h['status'] for h in groups[0]['hosts']} == {'not_installed'}


def test_comfy_failure_detail_reports_node_and_exception_without_paths():
    from studio.backend import comfy_failure_detail
    status = {'status_str': 'error', 'messages': [
        ['execution_start', {}],
        ['execution_error', {'node_type': 'KSampler', 'exception_type': 'torch.OutOfMemoryError',
                             'exception_message': 'CUDA out of memory.\nTried to allocate 2.00 GiB at /workspace/historia/ComfyUI/x.py'}]]}
    text = comfy_failure_detail(status)
    assert 'KSampler' in text and 'OutOfMemoryError' in text and 'CUDA out of memory' in text
    assert '/workspace' not in text and '\n' not in text
    assert comfy_failure_detail({'status_str': 'error', 'messages': []}) == ''
    assert comfy_failure_detail({'messages': 'junk'}) == ''


def test_comfy_failure_detail_names_an_external_interrupt():
    from studio.backend import comfy_failure_detail
    text = comfy_failure_detail({'status_str': 'error', 'messages': [['execution_interrupted', {'node_id': '3'}]]})
    assert 'ngắt từ bên ngoài' in text and 'không phải do thiếu VRAM' in text


def test_inscriptions_are_chinese_characters_and_tattoos_follow_the_chronicle(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    assert '大越' in pack['visual'] and 'Chinese characters' in pack['visual']
    forearm = knowledge.visual_for_scene(pack, {'visual_prompt': 'Close shot of a soldier forearm tattooed with two ink characters', 'narration': '', 'title': 'Hai chữ Sát Thát'})
    assert '殺韃' in forearm and 'Sat That' not in forearm and 'dragon' not in forearm
    flags = knowledge.visual_for_scene(pack, {'visual_prompt': 'war boats with banners on the river', 'narration': ''})
    assert '陳' in flags and '破彊敵報皇恩' not in flags
    assert '破彊敵報皇恩' in knowledge.visual_for_scene(pack, {'visual_prompt': 'Prince Hoai Van unfurls his flag', 'narration': ''})
    bare = knowledge.visual_for_scene(pack, {'visual_prompt': 'A bare-chested boatman pulls the oar', 'narration': ''})
    assert 'dragon' in bare and 'bare skin' in bare
    titles = [f['title'] for f in knowledge.load(tmp_path, 'tran')['facts']['customs_institutions']]
    assert any('殺韃' in t for t in titles)


def test_pack_reconciles_the_toan_thu_with_viet_nam_su_luoc_and_the_costume_study(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    facts = pack['facts']
    dress = {f['title']: f for f in facts['dress_appearance']}
    assert 'Thượng hoàng Thái Tông' not in {f['title'] for f in facts['dress_appearance'] if f.get('year') == 1285}
    assert 'Nhân Tông' in dress['Vua Nhân Tông']['title'] and 'Thái Tông đã mất từ 1277' in dress['Vua Nhân Tông']['text']
    for title in ('Mũ chữ đinh: cấu tạo', 'Tóc nam giới thời Trần', 'Giày dép', 'Trang phục múa giá chi vũ (phù điêu chùa Hòa Long)'):
        assert title in dress and dress[title]['ref']
    refs = ' '.join(f['ref'] for cat in facts.values() for f in cat)
    assert 'Việt Nam Sử Lược' in refs and 'daivietcophong' in refs
    assert 'ĐỐI CHIẾU SỬ LIỆU' in pack['script'] and 'Sát Đát' in pack['script']
    assert any('Hịch tướng sĩ' in f['title'] for f in facts['events'])
    assert all(f.get('ref') for cat in facts.values() for f in cat)
    # image-bound text stays positive and free of Latin transliterations of Vietnamese objects
    image_text = ' '.join([pack['visual']] + [r['text'] for r in pack['roles']])
    for latin in ('ao trang vat', 'thai long', 'mu co thao', 'Thien Truong', 'Ma Loi', 'non la'):
        assert latin not in image_text
    dance = knowledge.visual_for_scene(pack, {'visual_prompt': 'Court dancers perform for the envoys', 'narration': ''})
    assert 'upturned brim' in dance
    women = knowledge.visual_for_scene(pack, {'visual_prompt': 'The queen walks through the garden', 'narration': ''})
    assert 'white lining' in women and 'bun' in women


def test_image_text_gives_men_a_topknot_and_a_concrete_costume_never_a_modern_cut(tmp_path):
    pack = knowledge.load(tmp_path, 'tran')
    soldiers = knowledge.visual_for_scene(pack, {'visual_prompt': 'Vietnamese soldiers retreat down a road', 'narration': ''})
    for text in (pack['visual'], soldiers):
        assert 'cropped' not in text and 'short modern haircut' not in text.replace('no short modern haircut', '')
    assert 'topknot' in soldiers and 'hemp tunic' in soldiers and 'bare feet' in soldiers
    assert '殺韃' not in soldiers                                   # only when the scene is about the tattoo
    assert '殺韃' in knowledge.visual_for_scene(pack, {'visual_prompt': 'a soldier forearm tattooed', 'narration': ''})
    yuan = knowledge.visual_for_scene(pack, {'visual_prompt': 'Yuan cavalry charge', 'narration': ''})
    assert 'fur' in yuan and 'braids' in yuan and 'foreign' in yuan


def test_shots_of_a_short_narration_do_not_repeat_the_same_words():
    from studio.storyboard import auto_shots
    text = ('Tháng giêng năm 1285, Ô Mã Nhi đánh vào Vạn Kiếp và núi Phả Lại. Ngày mười hai, giặc đánh vào Gia Lâm, '
            'bắt được quân ta, thấy người nào cũng thích hai chữ Sát Thát, chúng tức lắm, giết hại rất nhiều.')
    excerpts = [s['narration_excerpt'] for s in auto_shots({'title': 'T', 'narration': text}, 4)]
    assert len(set(excerpts)) == 4 and all(excerpts)
