DESCRIPTION = '公交静态发布包的服务日与引用闭包采集'
SAMPLE = 'fixtures/publish-1.zip'
DEFAULT_STORE = '.work/publications'

import argparse
import json
from pathlib import Path
import sys
import tempfile
import zipfile

from .archive import archive_file
from .change import compare
from .core import PublicationValidationError, inspect_file
from .publication import PublicationStore, read_publication_tables, validate_publication
from .query import service_day, service_summary, stop_detail, trip_detail


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _fail(message, **extra):
    _emit({'error': message, **extra})
    raise SystemExit(1)


def _publication_header(manifest):
    return {
        'publication_id': manifest['publication_id'],
        'source': manifest.get('source'),
        'current': manifest.get('current', False),
        'path': manifest.get('path'),
    }


def _load_publication(store, reference):
    if reference in (None, '', 'current'):
        manifest = store.current()
        if manifest is None:
            _fail('发布库中还没有当前发布；请先运行 receive')
        return manifest
    if not store.is_registered(reference):
        _fail('发布库中没有这个发布：%s' % reference)
    return store.load(reference)


def _resolve_tables(store, reference):
    """接受发布库里的 ID / current，也接受尚未接收的压缩包路径（供候选比较）。"""
    if reference in (None, '', 'current'):
        manifest = _load_publication(store, reference)
        return store.load_tables(manifest['publication_id']), manifest
    path = Path(reference)
    if path.is_file():
        try:
            tables, membership = read_publication_tables(path)
        except PublicationValidationError as error:
            _fail('候选资料无法读取', errors=error.errors, source=error.source)
        manifest = {
            'publication_id': None,
            'source': path.name,
            'files': membership,
        }
        return tables, manifest
    if not store.is_registered(reference):
        _fail('既不是压缩包，发布库中也没有这个发布：%s' % reference)
    return store.load_tables(reference), store.load(reference)


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command', choices=[
        'inspect', 'capture', 'demo',
        'check', 'receive', 'publish', 'current', 'list',
        'service-day', 'trip', 'stop', 'services', 'compare',
    ])
    parser.add_argument('argument', nargs='?', default=None,
                        help='资料路径、发布 ID、服务日或 trip_id')
    parser.add_argument('second', nargs='?', default=None,
                        help='compare 的第二个发布')
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--store', default=DEFAULT_STORE)
    parser.add_argument('--publication', default='current',
                        help='服务日 / 班次 / 站点查询使用的发布 ID')
    parser.add_argument('--no-publish', action='store_true',
                        help='只接收候选发布，不把它设为当前发布')
    args = parser.parse_args()
    store = PublicationStore(args.store)

    if args.command == 'inspect':
        result = inspect_file(Path(args.argument or SAMPLE))
    elif args.command == 'capture':
        result = archive_file(Path(args.argument or SAMPLE), Path(args.archive))
    elif args.command == 'demo':
        with tempfile.TemporaryDirectory() as directory:
            result = {'原始资料': archive_file(Path(args.argument or SAMPLE), Path(directory)),
                      '资料预览': inspect_file(Path(args.argument or SAMPLE))}
    elif args.command == 'check':
        # 只校验候选资料，不写发布库、不碰当前发布。
        if not args.argument:
            _fail('check 需要候选资料路径')
        try:
            tables, membership = read_publication_tables(Path(args.argument))
        except PublicationValidationError as error:
            _fail('候选资料无法读取', errors=error.errors, source=error.source)
        except (FileNotFoundError, OSError) as error:
            _fail('无法读取候选资料：%s' % error, source=args.argument)
        errors, facts = validate_publication(tables, source=args.argument)
        result = {
            'accepted': not errors,
            'source': args.argument,
            'files': membership,
            'facts': facts,
            'errors': errors,
        }
        _emit(result)
        raise SystemExit(0 if not errors else 1)
    elif args.command == 'receive':
        if not args.argument:
            _fail('receive 需要资料路径')
        try:
            result = store.capture(Path(args.argument), publish=not args.no_publish)
        except PublicationValidationError as error:
            _fail('候选发布被拒绝，当前发布未改变', errors=error.errors,
                  source=error.source)
        except (FileNotFoundError, OSError, zipfile.BadZipFile) as error:
            _fail('无法读取候选资料：%s' % error, source=args.argument)
        result = {
            'publication_id': result['publication_id'],
            'source': result['source'],
            'bytes': result['bytes'],
            'already_present': result.get('already_present', False),
            'current': result['current'],
            'files': result['files'],
        }
    elif args.command == 'publish':
        identifier = args.argument or args.publication
        try:
            store.set_current(identifier)
        except FileNotFoundError:
            _fail('发布库中没有这个发布：%s' % identifier)
        result = _publication_header(store.load(identifier))
    elif args.command == 'current':
        manifest = store.current()
        result = {} if manifest is None else _publication_header(manifest)
    elif args.command == 'list':
        result = {'publications': [_publication_header(manifest)
                                   for manifest in store.publications()]}
    elif args.command in ('service-day', 'trip', 'stop', 'services'):
        manifest = _load_publication(store, args.publication)
        tables = store.load_tables(manifest['publication_id'])
        if args.command == 'service-day':
            if not args.argument:
                _fail('service-day 需要服务日，例如 20261113')
            try:
                body = service_day(tables, args.argument)
            except ValueError as error:
                _fail('服务日无法解释：%s' % error)
        elif args.command == 'trip':
            if not args.argument:
                _fail('trip 需要 trip_id')
            try:
                body = {'trip': trip_detail(tables, args.argument)}
            except KeyError:
                _fail('本发布中没有 trip_id %r' % args.argument,
                      publication_id=manifest['publication_id'])
        elif args.command == 'stop':
            if not args.argument:
                _fail('stop 需要 stop_id')
            try:
                body = {'stop': stop_detail(tables, args.argument)}
            except KeyError:
                _fail('本发布中没有 stop_id %r' % args.argument,
                      publication_id=manifest['publication_id'])
        else:
            body = service_summary(tables)
        result = {'publication': _publication_header(manifest), **body}
    elif args.command == 'compare':
        if not args.argument or not args.second:
            _fail('compare 需要两个发布：ID、current 或压缩包路径')
        tables_from, manifest_from = _resolve_tables(store, args.argument)
        tables_to, manifest_to = _resolve_tables(store, args.second)
        result = compare(tables_from, tables_to, manifest_from, manifest_to)
    else:  # pragma: no cover - argparse 已限定取值
        _fail('未知命令')
    _emit(result)


if __name__ == '__main__':
    main()
