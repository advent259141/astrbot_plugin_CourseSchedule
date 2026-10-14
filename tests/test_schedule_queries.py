"""Regression coverage for command parsing and date based schedule queries."""

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "course_schedule_under_test"
SHANGHAI = timezone(timedelta(hours=8))


def load_plugin():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = Mock()
    event_module = types.ModuleType("astrbot.api.event")
    event_module.filter = types.SimpleNamespace(command=lambda name: lambda func: func)
    event_module.AstrMessageEvent = object
    event_filter = types.ModuleType("astrbot.api.event.filter")
    event_filter.event_message_type = lambda kind: lambda func: func
    event_filter.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    core = types.ModuleType("astrbot.core")
    star = types.ModuleType("astrbot.core.star")
    star.Star = type("Star", (), {})
    star.Context = object
    star.StarMetadata = object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: ROOT)
    star.star_map = {}
    utils = types.ModuleType("astrbot.core.utils")
    io = types.ModuleType("astrbot.core.utils.io")
    io.download_file = Mock()
    for module in (astrbot, api, event_module, event_filter, core, star, utils, io):
        sys.modules[module.__name__] = module
    return importlib.import_module(f"{PACKAGE}.main"), importlib.import_module(f"{PACKAGE}.query_utils")


main, query_utils = load_plugin()


class Event:
    def __init__(self, user_id="alice", group_id="group", text="", mentions=()):
        self.user_id, self.group_id = user_id, group_id
        self.messages = [types.SimpleNamespace(type="Plain", text=text)]
        self.messages.extend(types.SimpleNamespace(type="At", qq=value) for value in mentions)
        self.message_str = text

    def get_sender_id(self): return self.user_id
    def get_group_id(self): return self.group_id
    def get_self_id(self): return "bot-id"
    def get_messages(self): return self.messages
    def get_sender_name(self): return self.user_id
    def plain_result(self, message): return message
    def image_result(self, image): return image


def ics_for(*rows):
    body = "BEGIN:VCALENDAR\nVERSION:2.0\n"
    for summary, start, end, extra in rows:
        body += (
            "BEGIN:VEVENT\nUID:" + summary + "\nDTSTART:" + start + "\nDTEND:" + end
            + "\nSUMMARY:" + summary + "\n" + extra + "END:VEVENT\n"
        )
    return body + "END:VCALENDAR\n"


class ScheduleQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_manager = Mock()
        self.data_manager.get_ics_file_path.side_effect = lambda user, group: self.root / f"{user}.ics"
        self.parser = main.ICSParser()
        self.users = {"group": {"users": {"alice": {"nickname": "阿丽"}, "bob": {"nickname": "小博"}}}}
        self.helper = main.ScheduleHelper(self.data_manager, self.parser, None, self.users)

    def write(self, user, content):
        (self.root / f"{user}.ics").write_text(content, encoding="utf-8")

    async def test_personal_default_today_hides_started_courses_but_explicit_date_includes_them(self):
        today = date(2026, 10, 13)
        self.write("alice", ics_for(
            ("Past", "20261013T090000", "20261013T100000", ""),
            ("Future", "20261013T150000", "20261013T160000", ""),
        ))
        fixed_now = datetime(2026, 10, 13, 12, tzinfo=SHANGHAI)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)
        with unittest.mock.patch(f"{PACKAGE}.schedule_helper.datetime", FixedDateTime):
            future, err = await self.helper.get_schedule_for_date(Event(), today, "的今日课程")
            full, full_err = await self.helper.get_schedule_for_date(Event(), today, "在2026-10-13的课程", include_all=True)
        self.assertIsNone(err)
        self.assertEqual([c["summary"] for c in future], ["Future"])
        self.assertIsNone(full_err)
        self.assertEqual([c["summary"] for c in full], ["Past", "Future"])
        self.assertTrue(all(c["nickname"] == "阿丽" for c in full))

    async def test_personal_query_requires_target_user_binding_in_current_group(self):
        courses, error = await self.helper.get_schedule_for_date(Event(), date(2026, 10, 13), "today", user_id="outsider")
        self.assertIsNone(courses)
        self.assertIn("还没有在这个群绑定", error)

    async def test_personal_nickname_copy_does_not_mutate_parser_cached_courses(self):
        self.write("alice", ics_for(("Course", "20261013T150000", "20261013T160000", "")))
        with unittest.mock.patch(f"{PACKAGE}.schedule_helper.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 10, 13, 12, tzinfo=SHANGHAI)
            courses, error = await self.helper.get_schedule_for_date(Event(), date(2026, 10, 13), "today")
        cached = next(iter(self.parser.course_cache.values()))
        self.assertIsNone(error)
        self.assertEqual(courses[0]["nickname"], "阿丽")
        self.assertNotIn("nickname", cached[0])

    async def test_group_reference_time_selects_current_course_and_respects_end_boundary(self):
        self.write("alice", ics_for(("Course", "20261013T150000", "20261013T160000", "")))
        moment = datetime(2026, 10, 13, 16, tzinfo=SHANGHAI)
        courses, error = await self.helper.get_group_schedule_for_date(Event(), moment.date(), reference_time=moment)
        self.assertIsNone(error)
        self.assertEqual(courses[0]["summary"], "当前及后续无课")

    async def test_group_query_keeps_course_crossing_midnight_in_progress(self):
        self.write("alice", ics_for(("Night", "20261012T233000", "20261013T003000", "")))
        moment = datetime(2026, 10, 13, 0, 5, tzinfo=SHANGHAI)
        courses, error = await self.helper.get_group_schedule_for_date(Event(), moment.date(), reference_time=moment)
        self.assertIsNone(error)
        self.assertEqual(courses[0]["summary"], "Night")
        self.assertEqual(courses[0]["nickname"], "阿丽")

    async def test_group_query_isolates_invalid_member_calendar(self):
        self.write("alice", "not an ics")
        self.write("bob", ics_for(("Good", "20261013T150000", "20261013T160000", "")))
        courses, error = await self.helper.get_group_schedule_for_date(Event(), date(2026, 10, 13), reference_time=datetime(2026, 10, 13, 12, tzinfo=SHANGHAI))
        self.assertIsNone(error)
        self.assertEqual([course["user_id"] for course in courses], ["bob"])

    async def test_mention_parser_ignores_bot_and_rejects_multiple_humans(self):
        self.assertEqual(query_utils.mentioned_user(Event(mentions=("bot-id", "alice"))), "alice")
        with self.assertRaisesRegex(ValueError, "只 @一位"):
            query_utils.mentioned_user(Event(mentions=("alice", "bob")))

    async def test_group_moment_accepts_time_only_date_only_and_date_time(self):
        now = datetime(2026, 10, 13, 12, tzinfo=SHANGHAI)
        self.assertEqual(query_utils.parse_group_moment("", now), (now.date(), now, "today"))
        self.assertEqual(query_utils.parse_group_moment("09:30", now), (
            now.date(), datetime(2026, 10, 13, 9, 30, tzinfo=SHANGHAI), "2026-10-13 09:30"
        ))
        self.assertEqual(query_utils.parse_group_moment("2026-10-14", now), (
            date(2026, 10, 14), now, "2026-10-14"
        ))
        self.assertEqual(query_utils.parse_group_moment("2026-10-14 09:30", now), (
            date(2026, 10, 14), datetime(2026, 10, 14, 9, 30, tzinfo=SHANGHAI), "2026-10-14 09:30"
        ))

    async def test_date_only_keeps_target_day_but_counts_wait_from_real_now(self):
        self.write("alice", ics_for(("Tomorrow", "20261015T090000", "20261015T100000", "")))
        plugin = main.Main.__new__(main.Main)
        plugin.schedule_helper = self.helper
        plugin.image_generator = Mock()
        plugin.image_generator.generate_schedule_image = Mock(
            side_effect=lambda *args, **kwargs: asyncio.sleep(0, result="image.png")
        )
        now = datetime(2026, 10, 14, 10, 15, tzinfo=SHANGHAI)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return now
        with unittest.mock.patch(f"{PACKAGE}.main.datetime", FixedDateTime):
            results = [item async for item in plugin.show_group_now_schedule(
                Event(text="/群友在上什么课 2026-10-15")
            )]
        call = plugin.image_generator.generate_schedule_image.call_args
        course = call.args[0][0]
        self.assertEqual(results, ["image.png"])
        self.assertEqual(course["start_time"].date(), date(2026, 10, 15))
        self.assertEqual(call.kwargs["reference_time"], now)
        generator = main.ImageGenerator.__new__(main.ImageGenerator)
        status, detail = generator._calculate_time_delta(
            course["start_time"], course["end_time"], call.kwargs["reference_time"], "2026-10-15"
        )
        self.assertEqual(status, "下一节")
        self.assertEqual(detail, "22 小时 45 分钟后")

    async def test_date_only_today_skips_courses_that_have_already_ended(self):
        self.write("alice", ics_for(
            ("Finished", "20261014T090000", "20261014T100000", ""),
            ("Next", "20261014T170000", "20261014T180000", ""),
        ))
        plugin = main.Main.__new__(main.Main)
        plugin.schedule_helper = self.helper
        plugin.image_generator = Mock()
        plugin.image_generator.generate_schedule_image = Mock(
            side_effect=lambda *args, **kwargs: asyncio.sleep(0, result="image.png")
        )
        now = datetime(2026, 10, 14, 15, tzinfo=SHANGHAI)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return now
        with unittest.mock.patch(f"{PACKAGE}.main.datetime", FixedDateTime):
            results = [item async for item in plugin.show_group_now_schedule(
                Event(text="/群友在上什么课 今天")
            )]
        call = plugin.image_generator.generate_schedule_image.call_args
        self.assertEqual(results, ["image.png"])
        self.assertEqual(call.args[0][0]["summary"], "Next")
        self.assertEqual(call.kwargs["reference_time"], now)

    async def test_show_group_command_reuses_same_reference_moment_for_query_and_image(self):
        plugin = main.Main.__new__(main.Main)
        plugin.schedule_helper = Mock()
        plugin.schedule_helper.get_group_schedule_for_date = Mock(side_effect=lambda *args, **kwargs: asyncio.sleep(0, result=([{"summary": "Course"}], None)))
        plugin.image_generator = Mock()
        plugin.image_generator.generate_schedule_image = Mock(side_effect=lambda *args, **kwargs: asyncio.sleep(0, result="image.png"))
        now = datetime(2026, 10, 13, 12, tzinfo=SHANGHAI)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return now
        with unittest.mock.patch(f"{PACKAGE}.main.datetime", FixedDateTime):
            for arguments, expected in (
                ("14:30", datetime(2026, 10, 13, 14, 30, tzinfo=SHANGHAI)),
                ("2026-10-16 09:30", datetime(2026, 10, 16, 9, 30, tzinfo=SHANGHAI)),
            ):
                with self.subTest(arguments=arguments):
                    result = [item async for item in plugin.show_group_now_schedule(
                        Event(text=f"/群友在上什么课 {arguments}")
                    )]
                    call = plugin.schedule_helper.get_group_schedule_for_date.call_args
                    query_moment = call.kwargs["reference_time"]
                    image_moment = plugin.image_generator.generate_schedule_image.call_args.kwargs["reference_time"]
                    self.assertEqual(call.args[1], expected.date())
                    self.assertEqual(query_moment, image_moment)
                    self.assertEqual(query_moment, expected)
                    self.assertEqual(result, ["image.png"])


if __name__ == "__main__":
    unittest.main()
