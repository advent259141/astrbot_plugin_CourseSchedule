"""Regression coverage for date-bounded and whole-term ICS expansion."""

import importlib
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "course_schedule_under_test"


def load_parser():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(error=lambda *args, **kwargs: None)
    for module in (astrbot, api):
        sys.modules[module.__name__] = module
    return importlib.import_module(f"{PACKAGE}.ics_parser").ICSParser


ICSParser = load_parser()
SHANGHAI = timezone(timedelta(hours=8))


def calendar(*events):
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "".join(events) + "END:VCALENDAR\n"


def event(summary, start, end, extra=""):
    return (
        "BEGIN:VEVENT\n"
        f"UID:{summary.lower().replace(' ', '-') }\n"
        f"DTSTART:{start}\nDTEND:{end}\nSUMMARY:{summary}\n{extra}"
        "END:VEVENT\n"
    )


class ICSRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "schedule.ics"
        self.parser = ICSParser()

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")
        return str(self.path)

    def test_range_includes_courses_overlapping_both_date_edges(self):
        path = self.write(calendar(
            event("Previous night", "20261012T233000", "20261013T003000"),
            event("Last minute", "20261014T235000", "20261015T001000"),
            event("Outside", "20261015T000000", "20261015T010000"),
        ))
        courses = self.parser.parse_ics_file(path, date(2026, 10, 13), date(2026, 10, 14))
        self.assertEqual([c["summary"] for c in courses], ["Previous night", "Last minute"])

    def test_range_rejects_reversed_or_partial_bounds(self):
        path = self.write(calendar())
        with self.assertRaises(ValueError):
            self.parser.parse_ics_file(path, date(2026, 10, 14), date(2026, 10, 13))
        with self.assertRaises(ValueError):
            self.parser.parse_ics_file(path, start_date=date(2026, 10, 13))

    def test_finite_rrule_expands_occurrences_through_inclusive_end_date(self):
        path = self.write(calendar(event(
            "Lab", "20261012T090000", "20261012T100000",
            "RRULE:FREQ=DAILY;COUNT=4\n",
        )))
        courses = self.parser.parse_ics_file(path, date(2026, 10, 13), date(2026, 10, 14))
        self.assertEqual([c["start_time"].date() for c in courses], [date(2026, 10, 13), date(2026, 10, 14)])

    def test_range_expansion_uses_shanghai_weekday_for_early_morning_course(self):
        path = self.write(calendar(
            "BEGIN:VEVENT\nUID:friday\nDTSTART:20261015T163000Z\nDTEND:20261015T173000Z\n"
            "SUMMARY:Friday\nRRULE:FREQ=WEEKLY;COUNT=2;BYDAY=FR\nEND:VEVENT\n"
        ))
        courses = self.parser.parse_ics_file(path, date(2026, 10, 16), date(2026, 10, 23))
        self.assertEqual([c["start_time"].date() for c in courses], [date(2026, 10, 16), date(2026, 10, 23)])
        self.assertTrue(all(c["start_time"].hour == 0 and c["start_time"].utcoffset() == timedelta(hours=8) for c in courses))

    def test_exdate_removes_and_rdate_adds_occurrences(self):
        path = self.write(calendar(
            "BEGIN:VEVENT\nUID:extras\nDTSTART:20261012T090000\nDTEND:20261012T100000\n"
            "SUMMARY:Seminar\nRRULE:FREQ=DAILY;COUNT=3\nEXDATE:20261013T090000\n"
            "RDATE:20261016T090000\nEND:VEVENT\n"
        ))
        courses = self.parser.parse_ics_file(path, date(2026, 10, 12), date(2026, 10, 16))
        self.assertEqual([c["start_time"].date() for c in courses], [date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 16)])

    def test_all_courses_expands_finite_rule_including_historical_occurrences(self):
        path = self.write(calendar(event(
            "Old lecture", "20250901T090000", "20250901T100000",
            "RRULE:FREQ=WEEKLY;COUNT=3\n",
        )))
        courses = self.parser.parse_ics_file(path, all_courses=True)
        self.assertEqual(len(courses), 3)
        self.assertEqual(courses[0]["start_time"].date(), date(2025, 9, 1))

    def test_all_courses_rejects_unbounded_recurrence(self):
        path = self.write(calendar(event(
            "Infinite", "20261012T090000", "20261012T100000", "RRULE:FREQ=DAILY\n"
        )))
        with self.assertRaisesRegex(ValueError, "结束日期或次数"):
            self.parser.parse_ics_file(path, all_courses=True)

    def test_until_includes_the_final_occurrence_in_local_timezone(self):
        path = self.write(calendar(event(
            "Until", "20261012T090000", "20261012T100000",
            "RRULE:FREQ=WEEKLY;UNTIL=20261026T010000Z\n",
        )))
        courses = self.parser.parse_ics_file(path, all_courses=True)
        self.assertEqual([course["start_time"].day for course in courses], [12, 19, 26])

    def test_moved_occurrence_replaces_original_time_without_double_counting(self):
        path = self.write(calendar(
            "BEGIN:VEVENT\nUID:moved\nDTSTART:20261012T090000\nDTEND:20261012T100000\n"
            "SUMMARY:Original\nRRULE:FREQ=DAILY;COUNT=3\nEND:VEVENT\n",
            "BEGIN:VEVENT\nUID:moved\nRECURRENCE-ID:20261013T090000\n"
            "DTSTART:20261013T110000\nDTEND:20261013T120000\nSUMMARY:Moved\nEND:VEVENT\n",
        ))
        courses = self.parser.parse_ics_file(path, all_courses=True)
        self.assertEqual(len(courses), 3)
        self.assertEqual([(course["start_time"].day, course["start_time"].hour) for course in courses],
                         [(12, 9), (13, 11), (14, 9)])

    def test_cancelled_occurrence_is_removed_even_without_its_own_times(self):
        path = self.write(calendar(
            "BEGIN:VEVENT\nUID:cancelled\nDTSTART:20261012T090000\nDTEND:20261012T100000\n"
            "SUMMARY:Lecture\nRRULE:FREQ=DAILY;COUNT=3\nEND:VEVENT\n",
            "BEGIN:VEVENT\nUID:cancelled\nRECURRENCE-ID:20261013T090000\n"
            "STATUS:CANCELLED\nEND:VEVENT\n",
        ))
        courses = self.parser.parse_ics_file(path, all_courses=True)
        self.assertEqual([course["start_time"].day for course in courses], [12, 14])

    def test_unbounded_rule_still_supports_a_bounded_date_query(self):
        path = self.write(calendar(event(
            "Daily", "20261012T090000", "20261012T100000", "RRULE:FREQ=DAILY\n"
        )))
        courses = self.parser.parse_ics_file(path, date(2026, 10, 13), date(2026, 10, 13))
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["start_time"].day, 13)

    def test_overly_large_repeat_returns_a_resource_limit_explanation(self):
        path = self.write(calendar(event(
            "Too large", "20200101T090000", "20200101T100000", "RRULE:FREQ=DAILY;COUNT=10001\n"
        )))
        with self.assertRaisesRegex(ValueError, "10000"):
            self.parser.parse_ics_file(path, all_courses=True)

    def test_many_different_date_queries_do_not_grow_cache_indefinitely(self):
        path = self.write(calendar(event(
            "Daily", "20261012T090000", "20261012T100000", "RRULE:FREQ=DAILY;COUNT=200\n"
        )))
        start = date(2026, 10, 12)
        for offset in range(140):
            target = start + timedelta(days=offset)
            self.parser.parse_ics_file(path, target, target)
        self.assertLessEqual(len(self.parser.course_cache), 128)
        self.assertEqual(self.parser.parse_ics_file(path, start, start)[0]["start_time"].date(), start)

    def test_cache_keys_include_range_and_clear_cache_removes_every_range(self):
        path = self.write(calendar(event("Cached", "20261012T090000", "20261012T100000")))
        first = self.parser.parse_ics_file(path, date(2026, 10, 12), date(2026, 10, 12))
        self.parser.parse_ics_file(path, date(2026, 10, 13), date(2026, 10, 13))
        first[0]["summary"] = "cache sentinel"
        self.assertEqual(self.parser.parse_ics_file(path, date(2026, 10, 12), date(2026, 10, 12))[0]["summary"], "cache sentinel")
        self.assertEqual(len(self.parser.course_cache), 2)
        self.parser.clear_cache(path)
        self.assertEqual(self.parser.course_cache, {})


if __name__ == "__main__":
    unittest.main()
