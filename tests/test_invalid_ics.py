"""Regression tests for malformed calendar files at query and binding boundaries."""

import asyncio
import importlib
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "course_schedule_under_test"


def load_plugin():
    """Provide the small AstrBot API surface needed to import the real plugin."""
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = Mock()
    event = types.ModuleType("astrbot.api.event")
    event.filter = types.SimpleNamespace(command=lambda name: lambda func: func)
    event.AstrMessageEvent = object
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

    for module in (astrbot, api, event, event_filter, core, star, utils, io):
        sys.modules[module.__name__] = module

    return importlib.import_module(f"{PACKAGE}.main"), api.logger


main, logger = load_plugin()
SHANGHAI = timezone(timedelta(hours=8))
BAD_ICS = "UID:broken\n"


def valid_ics(summary="Good course"):
    start = datetime.now(SHANGHAI).replace(hour=23, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
        f"SUMMARY:{summary}\nEND:VEVENT\nEND:VCALENDAR\n"
    )


class Event:
    def __init__(self, user_id="bad", group_id="group", messages=()):
        self.user_id = user_id
        self.group_id = group_id
        self.messages = messages
        self.unified_msg_origin = "test"
        self.message_obj = types.SimpleNamespace(raw_message="test")
        self.message_str = "a" * 32

    def get_sender_id(self):
        return self.user_id

    def get_group_id(self):
        return self.group_id

    def get_messages(self):
        return self.messages

    def get_sender_name(self):
        return "sender"

    def plain_result(self, message):
        return message

    def image_result(self, image):
        return image


class InvalidICSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_manager = Mock()
        self.data_manager.get_ics_file_path.side_effect = (
            lambda user_id, group_id: self.root / f"{user_id}_{group_id}.ics"
        )
        self.parser = main.ICSParser()
        self.user_data = {"group": {"users": {"bad": {}, "good": {}}}}
        self.helper = main.ScheduleHelper(self.data_manager, self.parser, None, self.user_data)
        (self.root / "bad_group.ics").write_text(BAD_ICS, encoding="utf-8")
        (self.root / "good_group.ics").write_text(valid_ics(), encoding="utf-8")
        logger.reset_mock()

    async def test_personal_query_reports_invalid_calendar(self):
        courses, error = await self.helper.get_schedule_for_date(
            Event(), datetime.now(SHANGHAI).date(), "的今日课程"
        )
        self.assertIsNone(courses)
        self.assertIn("解析失败", error)
        self.assertNotIn("UID", error)

    async def test_group_query_keeps_valid_user(self):
        courses, error = await self.helper.get_group_schedule_for_date(
            Event(), datetime.now(SHANGHAI).date()
        )
        self.assertIsNone(error)
        self.assertEqual([course["user_id"] for course in courses], ["good"])
        self.assertIn('Property "UID"', logger.error.call_args.args[0])

    async def test_group_query_reports_when_every_file_is_invalid(self):
        self.user_data["group"]["users"].pop("good")
        courses, error = await self.helper.get_group_schedule_for_date(
            Event(), datetime.now(SHANGHAI).date()
        )
        self.assertIsNone(courses)
        self.assertIn("解析失败", error)

    async def test_ranking_keeps_valid_user(self):
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.user_data = self.user_data
        plugin.image_generator = Mock()
        plugin.image_generator.generate_ranking_image = Mock(
            side_effect=lambda *args: asyncio.sleep(0, result=args[0])
        )
        results = [item async for item in plugin.weekly_course_ranking(Event())]
        self.assertEqual(len(results), 1)
        self.assertEqual([row["user_id"] for row in results[0]], ["good"])

    async def test_ranking_reports_when_every_file_is_invalid(self):
        self.user_data["group"]["users"].pop("good")
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.user_data = self.user_data
        results = [item async for item in plugin.weekly_course_ranking(Event())]
        self.assertIn("解析失败", results[0])

    async def test_invalid_upload_preserves_existing_binding_and_file(self):
        original = valid_ics()
        target = self.root / "bad_group.ics"
        target.write_text(original, encoding="utf-8")
        self.user_data["group"]["users"]["bad"] = {"nickname": "old"}
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.user_data = self.user_data
        plugin.binding_requests = {"group-bad": {"timestamp": time.time(), "nickname": "new"}}
        file_component = types.SimpleNamespace(type="File", get_file=Mock(
            side_effect=lambda **kwargs: asyncio.sleep(0, result="https://example.invalid/bad.ics")
        ))

        async def download_bad(url, destination):
            Path(destination).write_text(BAD_ICS, encoding="utf-8")

        with patch.object(main, "download_file", side_effect=download_bad):
            results = [item async for item in plugin.handle_file_message(Event(messages=[file_component]))]

        self.assertIn("失败", results[0])
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(self.user_data["group"]["users"]["bad"]["nickname"], "old")
        self.data_manager.save_user_data.assert_not_called()
        self.assertEqual(len(list(self.root.glob("*.ics"))), 2)

    async def test_valid_upload_replaces_file_and_clears_cached_courses(self):
        target = self.root / "bad_group.ics"
        target.write_text(valid_ics("Old course"), encoding="utf-8")
        self.parser.parse_ics_file(str(target))
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.user_data = self.user_data
        plugin.binding_requests = {"group-bad": {"timestamp": time.time(), "nickname": "new"}}
        file_component = types.SimpleNamespace(type="File", get_file=Mock(
            side_effect=lambda **kwargs: asyncio.sleep(0, result="https://example.invalid/good.ics")
        ))

        async def download_good(url, destination):
            Path(destination).write_text(valid_ics("New course"), encoding="utf-8")

        with patch.object(main, "download_file", side_effect=download_good):
            results = [item async for item in plugin.handle_file_message(Event(messages=[file_component]))]

        self.assertIn("成功", results[0])
        self.assertIn("New course", target.read_text(encoding="utf-8"))
        self.assertEqual(str(self.parser.parse_ics_file(str(target))[0]["summary"]), "New course")
        self.data_manager.save_user_data.assert_called_once()

    async def test_failed_download_preserves_existing_file_and_removes_temporary_file(self):
        original = valid_ics()
        target = self.root / "bad_group.ics"
        target.write_text(original, encoding="utf-8")
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.user_data = self.user_data
        plugin.binding_requests = {"group-bad": {"timestamp": time.time()}}
        file_component = types.SimpleNamespace(type="File", get_file=Mock(
            side_effect=lambda **kwargs: asyncio.sleep(0, result="https://example.invalid/bad.ics")
        ))

        async def fail_download(url, destination):
            Path(destination).write_text("partial", encoding="utf-8")
            raise IOError("connection lost")

        with patch.object(main, "download_file", side_effect=fail_download):
            results = [item async for item in plugin.handle_file_message(Event(messages=[file_component]))]

        self.assertIn("失败", results[0])
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(len(list(self.root.glob("*.ics"))), 2)
        self.data_manager.save_user_data.assert_not_called()

    async def test_invalid_wakeup_calendar_preserves_existing_file(self):
        original = valid_ics()
        target = self.root / "bad_group.ics"
        target.write_text(original, encoding="utf-8")
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.ics_parser.fetch_wakeup_schedule = Mock(
            side_effect=lambda token: asyncio.sleep(0, result=[{}])
        )
        plugin.ics_parser.convert_wakeup_to_ics = Mock(return_value=BAD_ICS)
        plugin.user_data = self.user_data
        plugin.binding_requests = {"group-bad": {"timestamp": time.time(), "nickname": "new"}}

        results = [item async for item in plugin.handle_wakeup_token(Event())]

        self.assertIn("失败", results[0])
        self.assertNotIn("UID", results[0])
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.data_manager.save_user_data.assert_not_called()

    async def test_valid_wakeup_calendar_replaces_file(self):
        target = self.root / "bad_group.ics"
        target.write_text(valid_ics("Old course"), encoding="utf-8")
        plugin = main.Main.__new__(main.Main)
        plugin.data_manager = self.data_manager
        plugin.ics_parser = self.parser
        plugin.ics_parser.fetch_wakeup_schedule = Mock(
            side_effect=lambda token: asyncio.sleep(0, result=[{}])
        )
        plugin.ics_parser.convert_wakeup_to_ics = Mock(
            return_value=valid_ics("New course")
        )
        plugin.user_data = self.user_data
        plugin.binding_requests = {"group-bad": {"timestamp": time.time(), "nickname": "new"}}

        results = [item async for item in plugin.handle_wakeup_token(Event())]

        self.assertIn("成功", results[0])
        self.assertIn("New course", target.read_text(encoding="utf-8"))
        self.data_manager.save_user_data.assert_called_once()


if __name__ == "__main__":
    unittest.main()
