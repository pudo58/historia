"""Validate packaged API graphs against the connected Comfy schema before POST."""
import copy
import math


def normalize_graph(graph, schema):
    graph = copy.deepcopy(graph)
    for node in graph.values():
        if node['class_type'] != 'SaveVideo':
            continue
        inputs = schema.get('SaveVideo', {}).get('input', {})
        definitions = {**inputs.get('required', {}), **inputs.get('optional', {})}
        format_type = definitions.get('format', [None])[0]
        node['inputs'].pop('codec', None)
        node['inputs'].pop('format.codec', None)
        if format_type == 'COMFY_DYNAMICCOMBO_V3':
            node['inputs']['format.codec'] = 'h264'
        else:
            node['inputs']['codec'] = 'h264'
    validate_graph(graph, schema)
    return graph


def validate_graph(graph, schema):
    def fail(node_id, field, message):
        raise ValueError(f'Workflow node {node_id}, input {field}: {message}')

    def validate(node_id, definitions, values, prefix=''):
        for category in ('required', 'optional'):
            for field, definition in definitions.get(category, {}).items():
                name = prefix + field
                if name not in values:
                    if category == 'required':
                        fail(node_id, name, 'thiếu input bắt buộc')
                    continue
                value = values[name]
                typ = definition[0]
                rules = definition[1] if len(definition) > 1 else {}
                if isinstance(value, list):
                    if len(value) != 2 or value[0] not in graph or type(value[1]) is not int:
                        fail(node_id, name, 'liên kết không hợp lệ')
                    source = schema[graph[value[0]]['class_type']].get('output', [])
                    if value[1] < 0 or value[1] >= len(source):
                        fail(node_id, name, 'output không tồn tại')
                    if isinstance(typ, str) and typ != '*' and source[value[1]] not in (typ, '*'):
                        fail(node_id, name, 'sai kiểu liên kết')
                    continue
                if typ == 'COMFY_DYNAMICCOMBO_V3':
                    option = next((o for o in rules.get('options', []) if o['key'] == value), None)
                    if option is None:
                        fail(node_id, name, 'lựa chọn không hợp lệ')
                    validate(node_id, option.get('inputs', {}), values, name + '.')
                elif isinstance(typ, list):
                    if value not in typ:
                        fail(node_id, name, 'giá trị/model không có trong schema')
                elif typ == 'COMBO' and rules.get('options') is not None:
                    if value not in rules['options']:
                        fail(node_id, name, 'lựa chọn không có trong schema')
                elif typ in ('INT', 'FLOAT'):
                    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                            not math.isfinite(value) or (typ == 'INT' and type(value) is not int)):
                        fail(node_id, name, 'sai kiểu số')
                    if value < rules.get('min', -math.inf) or value > rules.get('max', math.inf):
                        fail(node_id, name, 'ngoài giới hạn')
                elif typ == 'STRING' and not isinstance(value, str):
                    fail(node_id, name, 'phải là chuỗi')
                elif typ == 'BOOLEAN' and not isinstance(value, bool):
                    fail(node_id, name, 'phải là boolean')

    for node_id, node in graph.items():
        if node['class_type'] not in schema:
            fail(node_id, 'class_type', 'thiếu node ' + node['class_type'])
    for node_id, node in graph.items():
        validate(node_id, schema[node['class_type']].get('input', {}), node['inputs'])
