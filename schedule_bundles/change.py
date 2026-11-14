"""比较两次发布之间到底换掉了哪些引用关系。

比较只在两份发布自身携带的资料之间进行：每个发布内部的 ID 各有各的含义，
不因为名字相同就认定是同一个业务实体，也不会为了对齐而改动任何一份发布。
"""

from __future__ import annotations

# 每种表的记录主键。主键同样只在单份发布内有效。
RECORD_KEYS = {
    'agency': ('agency_id',),
    'stops': ('stop_id',),
    'routes': ('route_id',),
    'trips': ('trip_id',),
    'stop_times': ('trip_id', 'stop_sequence'),
    'calendar': ('service_id',),
    'calendar_dates': ('service_id', 'date'),
    'frequencies': ('trip_id', 'start_time', 'end_time'),
}

# 引用关系：(关系名, 起点表, 起点字段, 终点表, 终点字段)。
# 只在同一发布内成立，比较时也不跨发布拼接。
REFERENCE_FIELDS = (
    ('route_agency', 'routes', 'agency_id', 'agency', 'agency_id'),
    ('stop_parent', 'stops', 'parent_station', 'stops', 'stop_id'),
    ('trip_route', 'trips', 'route_id', 'routes', 'route_id'),
    ('trip_service', 'trips', 'service_id', 'service_calendar', 'service_id'),
    ('stop_time_trip', 'stop_times', 'trip_id', 'trips', 'trip_id'),
    ('stop_time_stop', 'stop_times', 'stop_id', 'stops', 'stop_id'),
    ('frequency_trip', 'frequencies', 'trip_id', 'trips', 'trip_id'),
)


def _table_name(filename):
    return filename[:-4] if filename.endswith('.txt') else filename


def _record_rows(table):
    return table['rows'] if table else []


def _key(table_name, record):
    fields = RECORD_KEYS.get(table_name)
    if fields is None:
        # 未知表没有可依赖的主键；用整行内容作键，避免把不同记录折叠成一条。
        return tuple((column, record.get(column, '')) for column in sorted(record)
                     if not column.startswith('_'))
    return tuple(record.get(field, '') for field in fields)


def _fields(record):
    return {key: value for key, value in record.items()
            if not key.startswith('_')}


def _record_location(record):
    return '%s:%s' % (record.get('_file', ''), record.get('_line', ''))


def _edge_key(edge):
    return tuple(edge[field] for field in ('kind', 'owner_type', 'owner_id',
                                           'target_type', 'target_id'))


def _owner_id(table_name, record):
    key = _key(table_name, record)
    return '|'.join(key) if len(key) > 1 else (key[0] if key else '')


def _edges(tables):
    """抽取发布内实际写出的引用关系，并注明原始文件和行。"""
    edges = {}
    for kind, owner_table, owner_field, target_table, target_field in REFERENCE_FIELDS:
        table = tables.get(owner_table + '.txt')
        if not table:
            continue
        for record in table['rows']:
            target_id = record.get(owner_field, '')
            if not target_id:
                continue  # 空引用由发布校验负责报告，这里不伪造关系
            edge = {
                'kind': kind,
                'owner_type': owner_table,
                'owner_id': _owner_id(owner_table, record),
                'owner_field': owner_field,
                'target_type': target_table,
                'target_id': target_id,
                'source': _record_location(record),
            }
            edges[_edge_key(edge)] = edge
    return edges


def _table_changes(tables_from, tables_to, table_name):
    """逐表比较记录，只报告新增、移除和字段变化，不推断业务身份。"""
    old_rows = _record_rows(tables_from.get(table_name + '.txt'))
    new_rows = _record_rows(tables_to.get(table_name + '.txt'))
    old = {_key(table_name, record): record for record in old_rows}
    new = {_key(table_name, record): record for record in new_rows}
    added = []
    removed = []
    changed = []
    for key in sorted(set(new) - set(old), key=str):
        added.append({'key': list(key), 'source': _record_location(new[key]),
                      'fields': _fields(new[key])})
    for key in sorted(set(old) - set(new), key=str):
        removed.append({'key': list(key), 'source': _record_location(old[key]),
                        'fields': _fields(old[key])})
    for key in sorted(set(old) & set(new), key=str):
        before = _fields(old[key])
        after = _fields(new[key])
        differing = sorted(field for field in set(before) | set(after)
                           if before.get(field, '') != after.get(field, ''))
        if differing:
            changed.append({
                'key': list(key),
                'from_source': _record_location(old[key]),
                'to_source': _record_location(new[key]),
                'changes': {field: {'from': before.get(field, ''),
                                    'to': after.get(field, '')}
                            for field in differing},
            })
    return {'added': added, 'removed': removed, 'changed': changed}


def compare(tables_from, tables_to, manifest_from=None, manifest_to=None):
    old_table_names = {_table_name(name) for name in tables_from}
    new_table_names = {_table_name(name) for name in tables_to}

    table_changes = {}
    for table_name in sorted(old_table_names | new_table_names):
        changes = _table_changes(tables_from, tables_to, table_name)
        if changes['added'] or changes['removed'] or changes['changed']:
            table_changes[table_name] = changes

    old_edges = _edges(tables_from)
    new_edges = _edges(tables_to)
    edges_removed = [old_edges[key] for key in sorted(set(old_edges) - set(new_edges), key=str)]
    edges_added = [new_edges[key] for key in sorted(set(new_edges) - set(old_edges), key=str)]

    file_changes = {'added': [], 'removed': [], 'changed': []}
    if manifest_from and manifest_to:
        old_files = {entry['file']: entry for entry in manifest_from['files']}
        new_files = {entry['file']: entry for entry in manifest_to['files']}
        for name in sorted(set(new_files) - set(old_files)):
            file_changes['added'].append(new_files[name])
        for name in sorted(set(old_files) - set(new_files)):
            file_changes['removed'].append(old_files[name])
        for name in sorted(set(old_files) & set(new_files)):
            before = old_files[name]
            after = new_files[name]
            if before['sha256'] != after['sha256']:
                file_changes['changed'].append({
                    'file': name,
                    'from_sha256': before['sha256'],
                    'to_sha256': after['sha256'],
                    'from_rows': before['rows'],
                    'to_rows': after['rows'],
                })

    return {
        'format': 'schedule-bundles-comparison-v1',
        'from': None if manifest_from is None else {
            'publication_id': manifest_from['publication_id'],
            'source': manifest_from.get('source'),
        },
        'to': None if manifest_to is None else {
            'publication_id': manifest_to['publication_id'],
            'source': manifest_to.get('source'),
        },
        'note': '比较只反映两次发布各自写出的资料；相同的 ID 不等于跨发布的同一业务身份。',
        'files': file_changes,
        'tables': {'added': sorted(new_table_names - old_table_names),
                   'removed': sorted(old_table_names - new_table_names),
                   'changed': sorted(table_changes)},
        'reference_changes': {
            'removed': edges_removed,
            'added': edges_added,
        },
        'records': table_changes,
    }
