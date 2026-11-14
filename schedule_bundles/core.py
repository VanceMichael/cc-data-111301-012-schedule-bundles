"""GTFS 静态发布包的清点、读取与发布内引用校验。

边界始终是“一个压缩包就是一次发布”：所有引用只在同一个包内解释，不会从
别的发布、别的服务日或别的文件里借行补全。ID 只是该发布内的成员标识，
不是跨发布、跨日期的全局业务身份。
"""

from __future__ import annotations

import csv
import hashlib
import io
import posixpath
from pathlib import Path
import zipfile

FORMAT = 'schedule-bundles-publication-v1'

# 一个发布必须具备的表。calendar / calendar_dates 至少要有一份，稍后单独检查。
REQUIRED_FILES = ('agency.txt', 'stops.txt', 'routes.txt', 'trips.txt', 'stop_times.txt')
SERVICE_FILES = ('calendar.txt', 'calendar_dates.txt')

# 会被解释引用的表。其他 .txt 仍计入发布成员，但不参与 GTFS 引用校验。
KNOWN_TABLES = (
    'agency',
    'stops',
    'routes',
    'trips',
    'stop_times',
    'calendar',
    'calendar_dates',
    'frequencies',
)

REQUIRED_COLUMNS = {
    # agency_id 在只有一个 agency 时可以省略；多 agency 时由校验阶段另行检查。
    'agency': ('agency_timezone',),
    'stops': ('stop_id',),
    'routes': ('route_id',),
    'trips': ('route_id', 'service_id', 'trip_id'),
    'stop_times': ('trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'),
    'calendar': ('service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'start_date', 'end_date'),
    'calendar_dates': ('service_id', 'date', 'exception_type'),
    'frequencies': ('trip_id', 'start_time', 'end_time', 'headway_secs'),
}

WEEKDAY_FIELDS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


class PublicationValidationError(ValueError):
    """候选发布未通过校验；异常携带可定位到原始文件与行的问题清单。"""

    def __init__(self, errors, source=None, publication=None):
        self.errors = list(errors)
        self.source = source
        self.publication = publication
        first = self.errors[0] if self.errors else None
        if first:
            where = first.get('file') or '发布包'
            if first.get('line') is not None:
                where += ':' + str(first['line'])
            if first.get('record'):
                where += ' ' + str(first['record'])
            message = where + '：' + str(first.get('message'))
        else:
            message = '候选发布未通过校验'
        if len(self.errors) > 1:
            message += '（另有 %d 项问题）' % (len(self.errors) - 1)
        super().__init__(message)


def inspect_file(path):
    """保留原有的单份来源结构预览；只报告成员和行数，不做业务解释。"""
    files = []
    with zipfile.ZipFile(Path(path)) as archive:
        for name in archive.namelist():
            if name.endswith('.txt'):
                with archive.open(name) as stream, io.TextIOWrapper(stream, encoding='utf-8-sig', newline='') as reader:
                    rows = csv.reader(reader)
                    header = next(rows, [])
                    files.append({'name': name, 'columns': header, 'rows': sum(1 for row in rows)})
    return {'files': files}
