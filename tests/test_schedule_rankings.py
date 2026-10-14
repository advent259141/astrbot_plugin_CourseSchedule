"""Date queries and rankings exercised through the real command handlers."""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_schedule_queries import Event, SHANGHAI, ics_for, main


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 12, tzinfo=SHANGHAI)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = main.Main.__new__(main.Main)
        self.plugin.data_manager = Mock()
        self.plugin.data_manager.get_ics_file_path.side_effect = (
            lambda user, group: self.root / f"{user}.ics"
        )
        self.plugin.ics_parser = main.ICSParser()
        self.plugin.user_data = {"group": {"users": {"alice": {"nickname": "Alice"}}}}
        self.plugin.schedule_helper = main.ScheduleHelper(
            self.plugin.data_manager, self.plugin.ics_parser, None, self.plugin.user_data
        )
        self.plugin.image_generator = SimpleNamespace(
            generate_ranking_image=AsyncMock(return_value="ranking.png"),
            generate_user_schedule_image=AsyncMock(return_value="personal.png"),
        )
        self.write("alice", ics_for(
            ("Weekly", "20261005T090000", "20261005T100000", "RRULE:FREQ=WEEKLY;COUNT=4\n"),
            ("Last week", "20261006T100000", "20261006T110000", ""),
            ("This week", "20261013T140000", "20261013T153000", ""),
        ))
        self.clock = patch.object(main, "datetime", FixedDateTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write(self, user, content):
        (self.root / f"{user}.ics").write_text(content, encoding="utf-8")

    async def collect(self, handler, event=None):
        return [result async for result in handler(event or Event())]

    async def test_previous_week_includes_historical_courses_and_monday_sunday_bounds(self):
        self.assertEqual(await self.collect(self.plugin.previous_week_course_ranking), ["ranking.png"])
        call = self.plugin.image_generator.generate_ranking_image.call_args
        rows, start, end = call.args
        self.assertEqual((start, end), (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertEqual(rows[0]["course_count"], 2)
        self.assertEqual(rows[0]["total_duration"], timedelta(hours=2))
        self.assertEqual(call.kwargs["title"], "上周上课排行榜")

    async def test_next_week_uses_its_own_cache_range(self):
        await self.collect(self.plugin.previous_week_course_ranking)
        await self.collect(self.plugin.next_week_course_ranking)
        rows, start, end = self.plugin.image_generator.generate_ranking_image.call_args.args
        self.assertEqual((start, end), (date(2026, 10, 19), date(2026, 10, 25)))
        self.assertEqual(rows[0]["course_count"], 1)
        self.assertEqual(rows[0]["total_duration"], timedelta(hours=1))

    async def test_current_week_also_counts_earlier_days_of_the_week(self):
        await self.collect(self.plugin.weekly_course_ranking)
        rows = self.plugin.image_generator.generate_ranking_image.call_args.args[0]
        self.assertEqual(rows[0]["course_count"], 2)
        self.assertEqual(rows[0]["total_duration"], timedelta(hours=2, minutes=30))

    async def test_semester_counts_all_historical_and_future_occurrences(self):
        self.assertEqual(await self.collect(self.plugin.semester_course_ranking), ["ranking.png"])
        call = self.plugin.image_generator.generate_ranking_image.call_args
        rows, start, end = call.args
        self.assertEqual((start, end), (None, None))
        self.assertEqual(rows[0]["course_count"], 6)
        self.assertEqual(rows[0]["total_duration"], timedelta(hours=6, minutes=30))
        self.assertEqual(call.kwargs["title"], "全学期上课排行榜")

    async def test_semester_skips_unbounded_calendar_and_reports_partial_statistics(self):
        self.plugin.user_data["group"]["users"]["bob"] = {"nickname": "Bob"}
        self.write("bob", ics_for((
            "Infinite", "20261001T080000", "20261001T090000", "RRULE:FREQ=DAILY\n"
        )))
        results = await self.collect(self.plugin.semester_course_ranking)
        self.assertIn("排行可能不完整", results[0])
        self.assertEqual(results[-1], "ranking.png")
        rows = self.plugin.image_generator.generate_ranking_image.call_args.args[0]
        self.assertEqual([row["user_id"] for row in rows], ["alice"])

    async def test_semester_with_only_unbounded_calendar_returns_explanation(self):
        self.write("alice", ics_for((
            "Infinite", "20261001T080000", "20261001T090000", "RRULE:FREQ=DAILY\n"
        )))
        results = await self.collect(self.plugin.semester_course_ranking)
        self.assertIn("结束日期或次数", results[0])
        self.plugin.image_generator.generate_ranking_image.assert_not_called()

    async def test_ranking_rejects_query_outside_bound_group(self):
        results = await self.collect(self.plugin.next_week_course_ranking, Event(group_id="another"))
        self.assertIn("还没有人绑定", results[0])
        self.plugin.image_generator.generate_ranking_image.assert_not_called()

    async def test_personal_command_reads_date_from_plain_text_around_mention(self):
        self.plugin.user_data["group"]["users"]["bob"] = {"nickname": "Bob"}
        self.write("bob", ics_for(("Morning", "20261013T090000", "20261013T100000", "")))
        event = Event(text="/查看课表 @Bob 2026-10-13")
        event.messages = [
            SimpleNamespace(type="At", qq="bot-id"),
            SimpleNamespace(type="Plain", text="/查看课表 "),
            SimpleNamespace(type="At", qq="bob"),
            SimpleNamespace(type="Plain", text=" 2026-10-13"),
        ]
        self.assertEqual(await self.collect(self.plugin.show_today_schedule, event), ["personal.png"])
        courses, nickname, title = self.plugin.image_generator.generate_user_schedule_image.call_args.args
        self.assertEqual([course["summary"] for course in courses], ["Morning"])
        self.assertEqual(nickname, "Bob")
        self.assertIn("2026-10-13", title)

    async def test_personal_command_reports_invalid_date_without_rendering(self):
        results = await self.collect(self.plugin.show_today_schedule, Event(text="/查看课表 2026-02-30"))
        self.assertIn("日期无效", results[0])
        self.plugin.image_generator.generate_user_schedule_image.assert_not_called()

    async def test_group_command_reports_invalid_time_without_querying(self):
        self.plugin.schedule_helper = Mock()
        results = await self.collect(self.plugin.show_group_now_schedule, Event(text="/群友在上什么课 25:00"))
        self.assertIn("时间无效", results[0])
        self.plugin.schedule_helper.get_group_schedule_for_date.assert_not_called()


class ImageStatusTests(unittest.TestCase):
    def test_state_respects_subsecond_start_and_end_boundaries(self):
        image = main.ImageGenerator.__new__(main.ImageGenerator)
        start = datetime(2026, 10, 13, 14, tzinfo=SHANGHAI)
        end = start + timedelta(hours=1)
        before_start = start - timedelta(milliseconds=500)
        before_end = end - timedelta(milliseconds=500)
        self.assertEqual(image._calculate_time_delta(start, end, before_start, "today")[0], "下一节")
        self.assertEqual(image._calculate_time_delta(start, end, before_end, "today")[0], "进行中")

    def test_exact_start_time_is_in_progress(self):
        image = main.ImageGenerator.__new__(main.ImageGenerator)
        start = datetime(2026, 10, 13, 14, tzinfo=SHANGHAI)
        status, detail = image._calculate_time_delta(start, start + timedelta(hours=1), start, "today")
        self.assertEqual(status, "进行中")
        self.assertIn("剩余", detail)

    def test_exact_end_time_is_finished(self):
        image = main.ImageGenerator.__new__(main.ImageGenerator)
        start = datetime(2026, 10, 13, 14, tzinfo=SHANGHAI)
        end = start + timedelta(hours=1)
        status, detail = image._calculate_time_delta(start, end, end, "today")
        self.assertEqual(status, "已结束")


if __name__ == "__main__":
    unittest.main()
