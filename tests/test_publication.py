import json
import shutil
from pathlib import Path
import tempfile
import unittest
import zipfile
from schedule_bundles.change import compare
from schedule_bundles.core import PublicationValidationError
from schedule_bundles.publication import (
    PublicationStore,
    read_publication_tables,
    validate_publication,
)
from schedule_bundles.query import service_day, stop_detail, trip_detail


ROOT = Path(__file__).resolve().parent.parent
P1 = ROOT / 'fixtures' / 'publish-1.zip'
P2 = ROOT / 'fixtures' / 'publish-2.zip'


def build(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)


BASE = {
    'agency.txt': 'agency_id,agency_name,agency_url,agency_timezone\na,虚构公交,https://example.invalid,Asia/Shanghai\n',
    'stops.txt': 'stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n',
    'routes.txt': 'route_id,agency_id,route_short_name,route_long_name,route_type\nr1,a,1,演练线,3\n',
    'trips.txt': 'route_id,service_id,trip_id,direction_id\n',
    'stop_times.txt': 'trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n',
    'calendar.txt': 'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n',
}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def publish(self, name, files):
        path = self.root / (name + '.zip')
        build(path, files)
        return path

    def test_reference_must_resolve_within_publication(self):
        errors, _ = validate_publication(*_tables(self.publish('bad', {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'platform,一号站台,30,120,0,station\n',
            'trips.txt': BASE['trips.txt'] + 'r1,holiday,t1,0\n',
            'stop_times.txt': BASE['stop_times.txt'] + 't1,25:10:00,25:10:00,missing,1,0,0\n',
            'calendar_dates.txt': 'service_id,date,exception_type\nholiday,20261113,1\n',
        })))
        self.assertTrue(any(error['file'] == 'stop_times.txt'
                            and error['line'] == 2
                            and 'missing' in error['message'] for error in errors))

    def test_bad_reference_points_to_original_file_and_line(self):
        errors, _ = validate_publication(*_tables(P2))
        self.assertEqual([(error['file'], error['line']) for error in errors],
                         [('stop_times.txt', 2)])
        self.assertTrue(errors[0]['message'])

    def test_optional_file_is_not_missing_reference(self):
        # 没有 calendar.txt，但 calendar_dates 提供了 service_id；这不是引用丢失。
        errors, _ = validate_publication(*_tables(P1))
        self.assertEqual(errors, [])

    def test_missing_service_calendar_is_an_error(self):
        files = {name: text for name, text in BASE.items() if name != 'calendar.txt'}
        errors, _ = validate_publication(*_tables(self.publish('noservice', files)))
        self.assertTrue(any('服务日历' in error['message'] for error in errors))

    def test_parent_station_must_resolve_in_publication(self):
        errors, _ = validate_publication(*_tables(self.publish('badparent', {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'platform,一号站台,30,120,0,nowhere\n',
        })))
        self.assertTrue(any(error['file'] == 'stops.txt'
                            and 'parent_station' in error['message'] for error in errors))

    def test_empty_field_is_preserved_not_invented(self):
        files = {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'station,总站,30,120,1,\n',
            'trips.txt': BASE['trips.txt'] + 'r1,holiday,t1,0\n',
            'stop_times.txt': BASE['stop_times.txt'] + 't1,25:10:00,25:10:00,station,1,,\n',
            'calendar_dates.txt': 'service_id,date,exception_type\nholiday,20261113,1\n',
        }
        tables, _ = read_publication_tables(self.publish('empty', files))
        errors, _ = validate_publication(tables)
        self.assertEqual(errors, [])
        row = tables['stop_times.txt']['rows'][0]
        self.assertEqual(row['pickup_type'], '')
        self.assertEqual(row['drop_off_type'], '')

    def test_ragged_row_is_rejected_instead_of_truncated(self):
        path = self.publish('ragged', {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'station,总站,30,120,1,,\n',
            'trips.txt': BASE['trips.txt'] + 'r1,holiday,t1,0\n',
            'stop_times.txt': BASE['stop_times.txt'] + 't1,25:10:00,25:10:00,station,1,0,0,extra\n',
            'calendar_dates.txt': 'service_id,date,exception_type\nholiday,20261113,1\n',
        })
        with self.assertRaises(PublicationValidationError) as caught:
            read_publication_tables(path)
        self.assertEqual(caught.exception.errors[0]['line'], 2)

    def test_publish_2_error_does_not_touch_current(self):
        store = PublicationStore(self.root / 'store')
        first = store.capture(P1)
        with self.assertRaises(PublicationValidationError):
            store.capture(P2)
        self.assertEqual(store.current_id(), first['publication_id'])
        self.assertEqual([item['publication_id'] for item in store.publications()],
                         [first['publication_id']])

    def test_candidate_can_be_checked_without_changing_library(self):
        store = PublicationStore(self.root / 'store')
        first = store.capture(P1)
        tables, _ = read_publication_tables(P2)
        errors, _ = validate_publication(tables)
        self.assertTrue(errors)
        self.assertEqual(store.current_id(), first['publication_id'])

    def test_receive_no_publish_keeps_previous_current(self):
        store = PublicationStore(self.root / 'store')
        first = store.capture(P1)
        second = store.capture(self.publish('good', {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'station,总站,30,120,1,\n',
            'trips.txt': BASE['trips.txt'] + 'r1,holiday,t1,0\n',
            'stop_times.txt': BASE['stop_times.txt'] + 't1,25:10:00,25:10:00,station,1,0,0\n',
            'calendar_dates.txt': 'service_id,date,exception_type\nholiday,20261113,1\n',
        }), publish=False)
        self.assertNotEqual(first['publication_id'], second['publication_id'])
        self.assertEqual(store.current_id(), first['publication_id'])
        store.set_current(second['publication_id'])
        self.assertEqual(store.current_id(), second['publication_id'])

    def test_removing_route_changes_only_membership_of_new_publication(self):
        store = PublicationStore(self.root / 'store')
        first = store.capture(P1)
        files = {
            name: text for name, text in _read(P1).items()
        }
        files['routes.txt'] = 'route_id,agency_id,route_short_name,route_long_name,route_type\n'
        files['trips.txt'] = 'route_id,service_id,trip_id,direction_id\n'
        files['stop_times.txt'] = 'trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n'
        files['frequencies.txt'] = 'trip_id,start_time,end_time,headway_secs,exact_times\n'
        second = store.capture(self.publish('noroute', files))
        self.assertEqual(second['facts']['route_ids'], [])
        # 旧发布仍完整可用，ID 与内容都没被改写。
        self.assertTrue(store.is_registered(first['publication_id']))
        old_tables = store.load_tables(first['publication_id'])
        self.assertEqual(old_tables['routes.txt']['rows'][0]['route_id'], 'r1')


def _tables(path):
    tables, _ = read_publication_tables(path)
    return tables, None


def _read(path):
    tables, _ = read_publication_tables(path)
    return {name: _raw_text(path, name) for name in tables}


def _raw_text(path, name):
    with zipfile.ZipFile(path) as archive:
        return archive.read(name).decode('utf-8-sig')


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tables, _ = read_publication_tables(P1)

    def test_calendar_dates_opens_service_on_exact_date(self):
        result = service_day(self.tables, '20261113')
        self.assertEqual([service['service_id'] for service in result['services']], ['holiday'])
        self.assertEqual(len(result['trips']), 1)
        self.assertEqual(result['trips'][0]['trip_id'], 't1')
        self.assertIn('calendar_dates.txt:2', result['trips'][0]['service_basis'][0]['source'])

    def test_other_dates_have_no_service(self):
        result = service_day(self.tables, '20261114')
        self.assertEqual(result['trips'], [])
        self.assertEqual(result['services'], [])

    def test_after_midnight_stays_on_original_service_day(self):
        result = service_day(self.tables, '20261113')
        stop_time = result['trips'][0]['stop_times'][0]
        self.assertEqual(stop_time['arrival_time'], '25:10:00')
        self.assertEqual(stop_time['arrival_seconds'], 25 * 3600 + 10 * 60)
        self.assertTrue(stop_time['arrival_time_next_day'])

    def test_frequency_row_stays_a_rule(self):
        result = service_day(self.tables, '20261113')
        frequencies = result['trips'][0]['frequencies']
        self.assertEqual(len(frequencies), 1)
        self.assertEqual(frequencies[0]['kind'], 'frequency_rule')
        self.assertEqual(frequencies[0]['exact_times'], '0')
        # 不展开成逐班时刻：stop_times 仍只有来源写的那一条。
        self.assertEqual(len(result['trips'][0]['stop_times']), 1)

    def test_trip_detail_reports_parent_station(self):
        detail = trip_detail(self.tables, 't1')
        stop = detail['stop_times'][0]['stop']
        self.assertEqual(stop['parent_station'], 'station')
        self.assertTrue(stop['parent_resolved'])

    def test_stop_detail_reports_hierarchy_and_raw_fields(self):
        detail = stop_detail(self.tables, 'platform')
        self.assertEqual(detail['fields']['parent_station'], 'station')
        self.assertEqual(detail['parent']['stop_id'], 'station')
        parent = stop_detail(self.tables, 'station')
        self.assertEqual([child['stop_id'] for child in parent['children']], ['platform'])
        self.assertEqual(parent['fields']['parent_station'], '')

    def test_calendar_and_exceptions_combine(self):
        files = {
            **BASE,
            'stops.txt': BASE['stops.txt'] + 'station,总站,30,120,1,\n',
            'trips.txt': BASE['trips.txt'] + 'r1,weekday,t1,0\nr1,weekend,t2,0\n',
            'stop_times.txt': (BASE['stop_times.txt']
                               + 't1,08:00:00,08:00:00,station,1,0,0\n'
                               + 't2,09:00:00,09:00:00,station,1,0,0\n'),
            'calendar.txt': BASE['calendar.txt']
            + 'weekday,1,1,1,1,1,0,0,20261101,20261130\n',
            'calendar_dates.txt': ('service_id,date,exception_type\n'
                                   'weekend,20261114,1\n'
                                   'weekday,20261113,2\n'),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.zip'
            build(path, files)
            tables, _ = read_publication_tables(path)
            errors, _ = validate_publication(tables)
            self.assertEqual(errors, [])

        # 2026-11-13 是周五：calendar 本应生效，但例外日期停开。
        friday = service_day(tables, '20261113')
        self.assertEqual(friday['trips'], [])
        self.assertEqual(friday['services'], [])
        self.assertEqual([item['service_id'] for item in friday['removed_by_exception']],
                         ['weekday'])
        self.assertEqual(friday['removed_by_exception'][0]['source'], 'calendar_dates.txt:3')

        # 2026-11-14 是周六：calendar 不生效，例外日期加开 weekend。
        saturday = service_day(tables, '20261114')
        self.assertEqual([service['service_id'] for service in saturday['services']], ['weekend'])
        self.assertEqual([trip['trip_id'] for trip in saturday['trips']], ['t2'])
        self.assertEqual(saturday['trips'][0]['service_basis'][0]['source'],
                         'calendar_dates.txt:2')

        # 2026-11-12 是周四：只有 calendar 生效，说明两者共同决定。
        thursday = service_day(tables, '20261112')
        self.assertEqual([trip['trip_id'] for trip in thursday['trips']], ['t1'])
        self.assertIn('calendar.txt:2', thursday['trips'][0]['service_basis'][0]['source'])


class ComparisonTests(unittest.TestCase):
    def test_only_reference_changes_are_reported(self):
        old, _ = read_publication_tables(P1)
        new, _ = read_publication_tables(P2)
        result = compare(old, new)
        self.assertEqual(result['tables']['changed'], ['stop_times'])
        removed = result['reference_changes']['removed']
        added = result['reference_changes']['added']
        self.assertEqual([(edge['target_id'], edge['source']) for edge in removed],
                         [('platform', 'stop_times.txt:2')])
        self.assertEqual([(edge['target_id'], edge['source']) for edge in added],
                         [('missing-platform', 'stop_times.txt:2')])
        self.assertEqual(result['records']['stop_times']['changed'][0]['changes']['stop_id'],
                         {'from': 'platform', 'to': 'missing-platform'})

    def test_identical_publication_has_no_changes(self):
        tables, _ = read_publication_tables(P1)
        result = compare(tables, tables)
        self.assertEqual(result['reference_changes'], {'removed': [], 'added': []})
        self.assertEqual(result['records'], {})


if __name__ == '__main__':
    unittest.main()
