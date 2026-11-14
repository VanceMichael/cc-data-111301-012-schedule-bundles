"""把一个 GTFS 静态压缩包接收为一次不可变发布。

接收分两步：先完整读取并校验候选发布，全部通过才登记为当前发布；任何一项
问题都会让候选发布被整体拒绝，库里已有的旧发布保持原样。不会出现“挑几张
能解析的表先盖掉旧版”的中间状态。
"""

from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import zipfile

from .archive import archive_file
from .core import (
    FORMAT,
    KNOWN_TABLES,
    REQUIRED_COLUMNS,
    REQUIRED_FILES,
    SERVICE_FILES,
    WEEKDAY_FIELDS,
    PublicationValidationError,
)

TIME_PATTERN = re.compile(r'^(\d{1,3}):([0-5]\d):([0-5]\d)$')
DATE_PATTERN = re.compile(r'^(\d{4})(\d{2})(\d{2})$')


# ---------------------------------------------------------------------------
# 基础解析

def parse_date(text):
    match = DATE_PATTERN.match(text)
    if not match:
        raise ValueError('日期应为 YYYYMMDD，实际为 %r' % text)
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def parse_time(text):
    """GTFS 时间可超过 24:00:00；返回自服务日 00:00 起的秒数。"""
    match = TIME_PATTERN.match(text)
    if not match:
        raise ValueError('时间应为 HH:MM:SS，实际为 %r' % text)
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def time_to_text(seconds):
    return '%02d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def normalize_date(text):
    """把日期输入规范成 YYYYMMDD；也接受 2026-11-13。"""
    text = text.strip()
    if re.match(r'^\d{8}$', text):
        parse_date(text)
        return text
    if re.match(r'^\d{4}-\d{2}-\d{2}$', text):
        parse_date(text.replace('-', ''))
        return text.replace('-', '')
    raise ValueError('日期应写成 YYYYMMDD 或 YYYY-MM-DD，实际为 %r' % text)


class _TableReadError(ValueError):
    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


def read_table(raw, name):
    """读取一张表。空字段保留为 ''，重复列名直接拒绝而不是悄悄覆盖。"""
    text = raw.decode('utf-8-sig')
    reader = csv.reader(io.StringIO(text, newline=''))
    rows = list(reader)
    if not rows:
        return {'name': name, 'columns': [], 'rows': []}
    columns = rows[0]
    if len(set(columns)) != len(columns):
        raise _TableReadError('%s 的列名重复：%s' % (name, columns), line=1)
    if any(not column for column in columns):
        raise _TableReadError('%s 存在空列名' % name, line=1)
    records = []
    for index, fields in enumerate(rows[1:], start=2):
        if not fields:
            continue  # 跳过完全空行
        if len(fields) > len(columns):
            raise _TableReadError(
                '第 %d 行有 %d 个字段，多于表头的 %d 列；拒绝丢弃多余字段'
                % (index, len(fields), len(columns)), line=index)
        record = {column: (fields[i] if i < len(fields) else '') for i, column in enumerate(columns)}
        record['_line'] = index
        record['_file'] = name
        records.append(record)
    return {'name': name, 'columns': columns, 'rows': records}


def read_publication_tables(path):
    """读取压缩包内的全部 .txt，返回已解析的表和成员清单。"""
    path = Path(path)
    tables = {}
    membership = []
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise PublicationValidationError([{
            'file': str(path), 'line': None,
            'message': '不是可读取的压缩包：%s' % error,
        }], source=str(path))
    with archive:
        for name in archive.namelist():
            relative = posixpath.normpath(name)
            if relative.startswith('../') or relative == '..' or posixpath.isabs(relative):
                raise PublicationValidationError([{
                    'file': name, 'line': None,
                    'message': '压缩包成员路径越界，拒绝接收',
                }], source=str(path))
            if name.endswith('/'):
                continue
            if not name.endswith('.txt'):
                continue
            basename = posixpath.basename(name)
            if basename in tables:
                raise PublicationValidationError([{
                    'file': name, 'line': None,
                    'message': '发布包含重复的 %s' % basename,
                }], source=str(path))
            raw = archive.read(name)
            try:
                table = read_table(raw, basename)
            except _TableReadError as error:
                raise PublicationValidationError([{
                    'file': name, 'line': error.line,
                    'message': '无法解析：%s' % error,
                }], source=str(path))
            except (UnicodeDecodeError, csv.Error) as error:
                raise PublicationValidationError([{
                    'file': name, 'line': None,
                    'message': '无法解析：%s' % error,
                }], source=str(path))
            table['member'] = name
            table['sha256'] = hashlib.sha256(raw).hexdigest()
            tables[basename] = table
            membership.append({
                'file': name,
                'sha256': table['sha256'],
                'bytes': len(raw),
                'rows': len(table['rows']),
                'columns': table['columns'],
            })
    membership.sort(key=lambda entry: entry['file'])
    return tables, membership


# ---------------------------------------------------------------------------
# 发布内校验

def validate_publication(tables, source=None):
    """在“一个发布”的边界内校验结构与引用。

    返回 (errors, facts)。errors 非空时调用方必须整体拒绝这份候选发布。
    """
    errors = []

    def problem(file, line, message, record=None):
        entry = {'file': file, 'line': line, 'message': message}
        if record:
            entry['record'] = record
        errors.append(entry)

    for filename in REQUIRED_FILES:
        if filename not in tables:
            problem(filename, None, '发布缺少必需文件')
    if not any(name in tables for name in SERVICE_FILES):
        problem('calendar/calendar_dates', None, '发布缺少服务日历：calendar.txt 与 calendar_dates.txt 至少需要一份')

    parsed = {name: table for name, table in tables.items() if name[:-4] in KNOWN_TABLES}

    for filename, table in sorted(parsed.items()):
        table_name = filename[:-4]
        columns = set(table['columns'])
        for required in REQUIRED_COLUMNS.get(table_name, ()):
            if required not in columns:
                problem(filename, 1, '缺少必需列 %s' % required)

    def rows(filename):
        table = tables.get(filename)
        return table['rows'] if table else []

    def location(record):
        return '%s:%s' % (record['_file'], record['_line'])

    # 收集各表的 ID，索引只在本发布内有效。
    agency_ids = set()
    if 'agency.txt' in tables:
        for record in rows('agency.txt'):
            value = record.get('agency_id', '').strip()
            if not value:
                problem('agency.txt', record['_line'], 'agency_id 为空')
            elif value in agency_ids:
                problem('agency.txt', record['_line'], 'agency_id %s 重复' % value)
            else:
                agency_ids.add(value)

    stop_by_id = {}
    stops_by_parent = {}
    for record in rows('stops.txt'):
        stop_id = record['stop_id']
        if not stop_id:
            problem('stops.txt', record['_line'], 'stop_id 为空')
            continue
        if stop_id in stop_by_id:
            problem('stops.txt', record['_line'], 'stop_id %s 重复' % stop_id)
            continue
        stop_by_id[stop_id] = record
        parent = record.get('parent_station', '')
        if parent:
            stops_by_parent.setdefault(parent, set()).add(stop_id)
    for stop_id, record in stop_by_id.items():
        parent = record.get('parent_station', '')
        if not parent:
            continue
        if parent not in stop_by_id:
            problem('stops.txt', record['_line'], 'parent_station %s 不在本发布的 stops.txt 中' % parent)
        elif parent == stop_id:
            problem('stops.txt', record['_line'], 'stop 不能以自身作为 parent_station')
        else:
            parent_type = stop_by_id[parent].get('location_type', '').strip()
            if parent_type and parent_type not in {'0', '1'}:
                problem('stops.txt', record['_line'],
                        'parent_station %s 的 location_type=%s，不能作为父站' % (parent, parent_type))

    route_ids = set()
    for record in rows('routes.txt'):
        route_id = record.get('route_id', '').strip()
        if not route_id:
            problem('routes.txt', record['_line'], 'route_id 为空')
            continue
        if route_id in route_ids:
            problem('routes.txt', record['_line'], 'route_id %s 重复' % route_id)
        route_ids.add(route_id)
        agency_id = record.get('agency_id', '').strip()
        if 'agency_id' in record and len(agency_ids) > 1 and not agency_id:
            problem('routes.txt', record['_line'], '多 agency 时 agency_id 不能为空')
        elif agency_id and agency_ids and agency_id not in agency_ids:
            problem('routes.txt', record['_line'],
                    'agency_id %s 不在本发布的 agency.txt 中' % agency_id)

    trip_ids = set()
    trip_service = {}
    trip_line = {}
    for record in rows('trips.txt'):
        trip_id = record.get('trip_id', '').strip()
        if not trip_id:
            problem('trips.txt', record['_line'], 'trip_id 为空')
            continue
        if trip_id in trip_ids:
            problem('trips.txt', record['_line'], 'trip_id %s 重复' % trip_id)
            continue
        trip_ids.add(trip_id)
        trip_line[trip_id] = record['_line']
        route_id = record.get('route_id', '').strip()
        if route_id not in route_ids:
            problem('trips.txt', record['_line'], 'route_id %s 不在本发布的 routes.txt 中' % route_id)
        service_id = record.get('service_id', '').strip()
        if not service_id:
            problem('trips.txt', record['_line'], 'service_id 为空')
        trip_service[trip_id] = service_id

    service_ids = set()
    for record in rows('calendar.txt'):
        service_id = record.get('service_id', '').strip()
        if not service_id:
            problem('calendar.txt', record['_line'], 'service_id 为空')
            continue
        if service_id in service_ids:
            problem('calendar.txt', record['_line'], 'service_id %s 重复' % service_id)
            continue
        service_ids.add(service_id)
        try:
            start = parse_date(record.get('start_date', ''))
            end = parse_date(record.get('end_date', ''))
            if start > end:
                problem('calendar.txt', record['_line'], 'start_date 晚于 end_date')
        except ValueError as error:
            problem('calendar.txt', record['_line'], str(error))
        for field in WEEKDAY_FIELDS:
            if record.get(field, '') not in {'0', '1'}:
                problem('calendar.txt', record['_line'], '%s 应为 0 或 1' % field)
    exception_keys = set()
    for record in rows('calendar_dates.txt'):
        service_id = record.get('service_id', '').strip()
        if not service_id:
            problem('calendar_dates.txt', record['_line'], 'service_id 为空')
            continue
        service_ids.add(service_id)
        try:
            date_text = parse_date(record.get('date', '')).isoformat()
        except ValueError as error:
            problem('calendar_dates.txt', record['_line'], str(error))
            date_text = record.get('date', '')
        exception_key = (service_id, date_text)
        if exception_key in exception_keys:
            problem('calendar_dates.txt', record['_line'],
                    '服务 %s 在 %s 的例外日期重复' % (service_id, date_text))
        exception_keys.add(exception_key)
        if record.get('exception_type', '') not in {'1', '2'}:
            problem('calendar_dates.txt', record['_line'], 'exception_type 应为 1 或 2')

    for trip_id, service_id in trip_service.items():
        if service_id and service_id not in service_ids:
            problem('trips.txt', trip_line.get(trip_id),
                    'service_id %s 不在本发布的服务日历中' % service_id)

    # stop_times：逐行校验引用和行驶顺序；全程只在本发布内解释。
    sequences = {}
    previous = {}
    for record in rows('stop_times.txt'):
        file = record['_file']
        line = record['_line']
        trip_id = record.get('trip_id', '').strip()
        stop_id = record.get('stop_id', '').strip()
        if not trip_id:
            problem(file, line, 'trip_id 为空')
        elif trip_id not in trip_ids:
            problem(file, line, 'trip_id %s 不在本发布的 trips.txt 中' % trip_id)
        if not stop_id:
            problem(file, line, 'stop_id 为空')
        elif stop_id not in stop_by_id:
            problem(file, line, 'stop_id %s 不在本发布的 stops.txt 中' % stop_id)
        sequence = record.get('stop_sequence', '').strip()
        if not sequence:
            problem(file, line, 'stop_sequence 为空')
        else:
            try:
                sequence_number = int(sequence)
                if sequence_number < 0:
                    raise ValueError
            except ValueError:
                problem(file, line, 'stop_sequence 应为非负整数，实际为 %r' % sequence)
                sequence_number = None
            if sequence_number is not None:
                seen = sequences.setdefault(trip_id, set())
                if sequence_number in seen:
                    problem(file, line, 'trip %s 的 stop_sequence %s 重复' % (trip_id, sequence_number))
                seen.add(sequence_number)
                if trip_id in previous and sequence_number <= previous[trip_id]:
                    problem(file, line, 'stop_sequence 必须按行程递增（上一条为 %s）' % previous[trip_id])
                previous[trip_id] = sequence_number

        arrival = record.get('arrival_time', '')
        departure = record.get('departure_time', '')
        arrival_seconds = departure_seconds = None
        try:
            if arrival:
                arrival_seconds = parse_time(arrival)
        except ValueError as error:
            problem(file, line, 'arrival_time：%s' % error)
        try:
            if departure:
                departure_seconds = parse_time(departure)
        except ValueError as error:
            problem(file, line, 'departure_time：%s' % error)
        if arrival_seconds is not None and departure_seconds is not None and departure_seconds < arrival_seconds:
            problem(file, line, 'departure_time 早于 arrival_time')

        # 空字段不能悄悄丢；类型取值也要能解释。
        pickup = record.get('pickup_type', '')
        drop_off = record.get('drop_off_type', '')
        if pickup not in {'', '0', '1', '2', '3'}:
            problem(file, line, 'pickup_type 取值无法解释：%r' % pickup)
        if drop_off not in {'', '0', '1', '2', '3'}:
            problem(file, line, 'drop_off_type 取值无法解释：%r' % drop_off)

    frequency_trips = set()
    frequency_keys = set()
    for record in rows('frequencies.txt'):
        file = record['_file']
        line = record['_line']
        trip_id = record.get('trip_id', '').strip()
        if not trip_id:
            problem(file, line, 'trip_id 为空')
        elif trip_id not in trip_ids:
            problem(file, line, 'trip_id %s 不在本发布的 trips.txt 中' % trip_id)
        else:
            frequency_trips.add(trip_id)
        frequency_key = (trip_id, record.get('start_time', ''), record.get('end_time', ''))
        if frequency_key in frequency_keys:
            problem(file, line, 'trip %s 的频率规则重复' % trip_id)
        frequency_keys.add(frequency_key)
        start = end = None
        try:
            start = parse_time(record.get('start_time', ''))
        except ValueError as error:
            problem(file, line, 'start_time：%s' % error)
        try:
            end = parse_time(record.get('end_time', ''))
        except ValueError as error:
            problem(file, line, 'end_time：%s' % error)
        if start is not None and end is not None and end <= start:
            problem(file, line, 'end_time 必须晚于 start_time')
        try:
            headway = int(record.get('headway_secs', ''))
            if headway <= 0:
                raise ValueError
        except ValueError:
            problem(file, line, 'headway_secs 应为正整数，实际为 %r' % record.get('headway_secs', ''))
        exact_times = record.get('exact_times', '')
        if exact_times not in {'', '0', '1'}:
            problem(file, line, 'exact_times 应为 0 或 1，实际为 %r' % exact_times)

    facts = {
        'files': sorted(tables),
        'agency_ids': sorted(agency_ids),
        'route_ids': sorted(route_ids),
        'trip_ids': sorted(trip_ids),
        'stop_ids': sorted(stop_by_id),
        'service_ids': sorted(service_ids),
        'frequency_trip_ids': sorted(frequency_trips),
        'parent_stations': {stop_id: record.get('parent_station', '')
                            for stop_id, record in stop_by_id.items()
                            if record.get('parent_station', '')},
        'empty_fields': _empty_fields(tables),
    }
    return errors, facts


def _empty_fields(tables):
    """记录哪些字段在来源中确实为空，避免导入时把“空”与“缺失”混为一谈。"""
    empty = {}
    for filename, table in tables.items():
        fields = {}
        for record in table['rows']:
            for column in table['columns']:
                if record.get(column, '') == '':
                    fields[column] = fields.get(column, 0) + 1
        if fields:
            empty[filename] = fields
    return empty


# ---------------------------------------------------------------------------
# 发布库

def _json_dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _write_atomic(path, text):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as writer:
        writer.write(text)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(temporary, path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _manifest_path(store, identifier):
    return Path(store) / 'publications' / (identifier + '.json')


def _tables_path(store, identifier):
    return Path(store) / 'tables' / (identifier + '.json')


class PublicationStore:
    """内容寻址的发布库。

    原始压缩包先归档；只有通过全部校验的发布才会写入 publication 索引。
    索引写入是最后一步，所以失败不会留下半份“当前发布”。
    """

    def __init__(self, root):
        self.root = Path(root)

    # -- 写入 ---------------------------------------------------------------

    def capture(self, source, publish=True):
        source = Path(source)
        tables, membership = read_publication_tables(source)
        errors, facts = validate_publication(tables, source=str(source))
        if errors:
            # 校验完全在内存中完成；被拒绝的候选不会在发布库里留下任何记录。
            raise PublicationValidationError(errors, source=str(source))
        archived = archive_file(source, self.root / 'objects')
        identifier = archived['sha256']
        if self.is_registered(identifier):
            if publish:
                self.set_current(identifier)
            manifest = self.load(identifier)
            manifest['already_present'] = True
            return manifest
        manifest = {
            'format': FORMAT,
            'publication_id': identifier,
            'source': archived['source'],
            'sha256': identifier,
            'bytes': archived['bytes'],
            'files': membership,
            'facts': facts,
            'registered_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'current': False,
        }
        (self.root / 'tables').mkdir(parents=True, exist_ok=True)
        (self.root / 'publications').mkdir(parents=True, exist_ok=True)
        _write_atomic(_tables_path(self.root, identifier), _json_dump({
            'publication_id': identifier,
            'tables': tables,
        }))
        _write_atomic(_manifest_path(self.root, identifier), _json_dump(manifest))
        if publish:
            self.set_current(identifier)
        return self.load(identifier)

    def is_registered(self, identifier):
        return _manifest_path(self.root, identifier).exists()

    def load(self, identifier):
        manifest = _read_json(_manifest_path(self.root, identifier))
        manifest['current'] = identifier == self.current_id()
        manifest['path'] = str(self.root / 'objects' / identifier)
        return manifest

    def load_tables(self, identifier):
        path = _tables_path(self.root, identifier)
        if not path.exists():
            raise FileNotFoundError('发布库中没有这个发布：%s' % identifier)
        return _read_json(path)['tables']

    def current_id(self):
        pointer = self.root / 'current.json'
        if not pointer.exists():
            return None
        return _read_json(pointer)['publication_id']

    def current(self):
        identifier = self.current_id()
        return None if identifier is None else self.load(identifier)

    def set_current(self, identifier):
        self.load(identifier)  # 不存在会直接报错，不会写出悬空指针
        _write_atomic(self.root / 'current.json', _json_dump({
            'format': FORMAT,
            'publication_id': identifier,
        }))

    def publications(self):
        active = self.current_id()
        directory = self.root / 'publications'
        if not directory.exists():
            return []
        result = []
        for path in sorted(directory.glob('*.json')):
            manifest = _read_json(path)
            manifest['current'] = manifest['publication_id'] == active
            manifest['path'] = str(self.root / 'objects' / manifest['publication_id'])
            result.append(manifest)
        result.sort(key=lambda item: (item['registered_at'], item['publication_id']))
        return result

    def resolve(self, identifier):
        if identifier in (None, '', 'current'):
            return self.current()
        return self.load(identifier)
