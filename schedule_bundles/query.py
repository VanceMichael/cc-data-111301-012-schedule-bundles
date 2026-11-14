"""按服务日查询一次 GTFS 静态发布。

查询口径固定为“这一个发布 + 这个服务日”：

* calendar.txt 与 calendar_dates.txt 共同决定服务是否生效；例外日期优先。
* 服务日从 00:00 开始，超过 24:00:00 的到站时间仍属于原服务日。
* frequencies.txt 的行是频率规则，展开时保留为频率规则，不伪装成精确逐班时刻。
* 只回答来源里写了什么；引用不上就指出发布内的文件和行，不跨发布补齐。
"""

from __future__ import annotations

from .publication import normalize_date, parse_date, parse_time, time_to_text
from .core import WEEKDAY_FIELDS


def _active_services(tables, service_date):
    """返回 (生效的服务 ID 集合, 被例外停开的记录)。"""
    active = {}
    removed = []
    holiday = parse_date(service_date)
    if 'calendar.txt' in tables:
        for record in tables['calendar.txt']['rows']:
            service_id = record.get('service_id', '').strip()
            if not service_id:
                continue
            try:
                start = parse_date(record.get('start_date', ''))
                end = parse_date(record.get('end_date', ''))
            except ValueError:
                continue
            if not (start <= holiday <= end):
                continue
            weekday = WEEKDAY_FIELDS[holiday.weekday()]
            if record.get(weekday, '') == '1':
                active.setdefault(service_id, []).append({
                    'source': '%s:%s' % (record['_file'], record['_line']),
                    'basis': 'calendar.txt 的 %s=1 且日期在 %s 至 %s 之间'
                             % (weekday, record.get('start_date'), record.get('end_date')),
                })
    if 'calendar_dates.txt' in tables:
        for record in tables['calendar_dates.txt']['rows']:
            service_id = record.get('service_id', '').strip()
            if not service_id:
                continue
            try:
                exception_date = parse_date(record.get('date', ''))
            except ValueError:
                continue  # 校验阶段已把坏日期报给资料员
            if exception_date != holiday:
                continue
            exception_type = record.get('exception_type', '')
            source = '%s:%s' % (record['_file'], record['_line'])
            if exception_type == '1':
                active.setdefault(service_id, []).append({
                    'source': source, 'basis': 'calendar_dates.txt 的 exception_type=1 加开',
                })
            elif exception_type == '2':
                active.pop(service_id, None)
                removed.append({
                    'service_id': service_id,
                    'source': source,
                    'basis': 'calendar_dates.txt 的 exception_type=2 停开',
                })
    return active, removed


def _trip_rows(tables):
    for record in tables.get('trips.txt', {'rows': []})['rows']:
        trip_id = record.get('trip_id', '').strip()
        if not trip_id:
            continue
        yield trip_id, record


def _stop_time_rows(tables, trip_id):
    rows = []
    for record in tables.get('stop_times.txt', {'rows': []})['rows']:
        if record.get('trip_id', '').strip() == trip_id:
            rows.append(record)

    def order(record):
        try:
            return int(record.get('stop_sequence') or 0)
        except ValueError:
            return 0

    rows.sort(key=order)
    return rows


def _frequency_rows(tables, trip_id):
    return [record for record in tables.get('frequencies.txt', {'rows': []})['rows']
            if record.get('trip_id', '').strip() == trip_id]


def _stop_summary(stops, stop_id, include_parent=True):
    record = stops.get(stop_id)
    if record is None:
        return {'stop_id': stop_id, 'resolved': False,
                'note': '本发布的 stops.txt 中没有这个站台/站点'}
    summary = {
        'stop_id': stop_id,
        'stop_name': record.get('stop_name', ''),
        'location_type': record.get('location_type', ''),
        'resolved': True,
        'source': '%s:%s' % (record['_file'], record['_line']),
    }
    if include_parent and 'parent_station' in record:
        summary['parent_station'] = record.get('parent_station', '')
        if record.get('parent_station', ''):
            summary['parent_resolved'] = record['parent_station'] in stops
        else:
            summary['parent_resolved'] = True
    return summary


def _stop_time_view(record, stops):
    """保留来源给的原值；时间为空时明确写空，不补 0。"""
    view = {
        'stop_sequence': record.get('stop_sequence', ''),
        'stop_id': record.get('stop_id', ''),
        'arrival_time': record.get('arrival_time', ''),
        'departure_time': record.get('departure_time', ''),
        'pickup_type': record.get('pickup_type', ''),
        'drop_off_type': record.get('drop_off_type', ''),
        'source': '%s:%s' % (record['_file'], record['_line']),
        'stop': _stop_summary(stops, record.get('stop_id', '')),
    }
    if record.get('arrival_time'):
        seconds = parse_time(record['arrival_time'])
        view['arrival_seconds'] = seconds
        view['arrival_time_text'] = time_to_text(seconds)
        view['arrival_time_next_day'] = seconds >= 24 * 3600
    if record.get('departure_time'):
        seconds = parse_time(record['departure_time'])
        view['departure_seconds'] = seconds
        view['departure_time_text'] = time_to_text(seconds)
        view['departure_time_next_day'] = seconds >= 24 * 3600
    # 空字段照样出现在结果里，并显式标注为空，提示资料员来源未给值。
    view['empty_fields'] = [name for name in ('arrival_time', 'departure_time',
                                              'pickup_type', 'drop_off_type')
                            if record.get(name, '') == '']
    return view


def _trip_view(tables, trip_id, trip_record, stops):
    stop_times = _stop_time_rows(tables, trip_id)
    frequencies = []
    for record in _frequency_rows(tables, trip_id):
        rule = {
            'start_time': record.get('start_time', ''),
            'end_time': record.get('end_time', ''),
            'headway_secs': record.get('headway_secs', ''),
            'exact_times': record.get('exact_times', ''),
            'source': '%s:%s' % (record['_file'], record['_line']),
            'kind': 'frequency_rule',
            'note': '频率规则本身不是逐班精确时刻',
        }
        if record.get('start_time'):
            rule['start_seconds'] = parse_time(record['start_time'])
        if record.get('end_time'):
            rule['end_seconds'] = parse_time(record['end_time'])
        frequencies.append(rule)
    return {
        'trip_id': trip_id,
        'route_id': trip_record.get('route_id', ''),
        'service_id': trip_record.get('service_id', ''),
        'direction_id': trip_record.get('direction_id', ''),
        'source': '%s:%s' % (trip_record['_file'], trip_record['_line']),
        'stop_times': [_stop_time_view(record, stops) for record in stop_times],
        'frequencies': frequencies,
    }


def service_day(tables, service_date):
    """列出某个服务日在这个发布里实际生效的行程与依据。"""
    service_date = normalize_date(service_date)
    active, removed = _active_services(tables, service_date)
    stops = {}
    if 'stops.txt' in tables:
        stops = {record.get('stop_id', '').strip(): record
                 for record in tables['stops.txt']['rows']}
    trips = []
    for trip_id, trip_record in _trip_rows(tables):
        service_id = trip_record.get('service_id', '').strip()
        if service_id not in active:
            continue
        view = _trip_view(tables, trip_id, trip_record, stops)
        view['service_basis'] = active[service_id]
        trips.append(view)

    # 服务日历里提到、但没有 trip 的服务也要如实说明，而不是静默为空。
    unused = sorted(set(active) - {view['service_id'] for view in trips})
    return {
        'service_date': service_date,
        'services': [
            {'service_id': service_id, 'basis': active[service_id]}
            for service_id in sorted(active)
        ],
        'services_without_trips': unused,
        'removed_by_exception': removed,
        'trips': trips,
    }


def trip_detail(tables, trip_id):
    """原始班次资料：一个 trip 的全部 stop_times 与频率规则，按来源原样返回。"""
    stops = {}
    if 'stops.txt' in tables:
        stops = {record.get('stop_id', '').strip(): record
                 for record in tables['stops.txt']['rows']}
    for existing_id, record in _trip_rows(tables):
        if existing_id == trip_id:
            return _trip_view(tables, existing_id, record, stops)
    raise KeyError('本发布中没有 trip_id %r' % trip_id)


def stop_detail(tables, stop_id):
    """站台/站点层级与原始字段；parent_station 只在发布内解释。"""
    stops = {}
    if 'stops.txt' in tables:
        stops = {record.get('stop_id', '').strip(): record
                 for record in tables['stops.txt']['rows']}
    if stop_id not in stops:
        raise KeyError('本发布中没有 stop_id %r' % stop_id)
    record = stops[stop_id]
    columns = tables['stops.txt']['columns']
    parent = record.get('parent_station', '')
    children = sorted(child_id for child_id, child in stops.items()
                      if child.get('parent_station', '') == stop_id and child_id != stop_id)
    return {
        'stop_id': stop_id,
        'fields': {column: record.get(column, '') for column in columns},
        'source': '%s:%s' % (record['_file'], record['_line']),
        'parent_station': parent,
        'parent_resolved': (parent == '' or parent in stops),
        'parent': None if not parent or parent not in stops else _stop_summary(stops, parent, False),
        'children': [_stop_summary(stops, child_id, False) for child_id in children],
    }


def service_summary(tables):
    """发布里声明的服务日历本身，及各自覆盖的日期范围。"""
    calendar = tables.get('calendar.txt')
    dates = tables.get('calendar_dates.txt')
    services = {}
    if calendar:
        for record in calendar['rows']:
            service_id = record.get('service_id', '').strip()
            if not service_id:
                continue
            services.setdefault(service_id, {'service_id': service_id,
                                             'calendar': [], 'exceptions': []})
            services[service_id]['calendar'].append({
                'source': '%s:%s' % (record['_file'], record['_line']),
                'start_date': record.get('start_date', ''),
                'end_date': record.get('end_date', ''),
                'weekdays': {field: record.get(field, '') for field in WEEKDAY_FIELDS},
            })
    if dates:
        for record in dates['rows']:
            service_id = record.get('service_id', '').strip()
            if not service_id:
                continue
            services.setdefault(service_id, {'service_id': service_id,
                                             'calendar': [], 'exceptions': []})
            services[service_id]['exceptions'].append({
                'source': '%s:%s' % (record['_file'], record['_line']),
                'date': record.get('date', ''),
                'exception_type': record.get('exception_type', ''),
            })
    return {'services': [services[key] for key in sorted(services)]}
