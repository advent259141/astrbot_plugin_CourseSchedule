# -*- coding: utf-8 -*-
"""
本模块负责处理 .ics 文件和 WakeUp 口令的解析、转换和数据获取。
"""
import re
from collections import OrderedDict
from datetime import datetime, timezone, timedelta, date, time as dt_time
from typing import Dict, List, Optional

import aiohttp
from icalendar import Calendar, Event
from dateutil.rrule import rruleset, rrulestr

from astrbot.api import logger

from .wakeup_client import WakeUpClientError, fetch_wakeup_schedule


class ICSParser:
    """ICS 和 WakeUp 数据解析器"""

    def __init__(self):
        self.course_cache: OrderedDict[tuple, List[Dict]] = OrderedDict()

    def parse_ics_file(
        self, file_path: str, start_date=None, end_date=None, all_courses=False
    ) -> List[Dict]:
        """按日期范围展开课程；全课表模式只允许有结束条件的重复课程。"""
        if all_courses:
            if start_date is not None or end_date is not None:
                raise ValueError("全课表查询不能同时指定日期范围。")
        else:
            if start_date is None and end_date is None:
                start_date = datetime.now(timezone(timedelta(hours=8))).date()
                end_date = start_date + timedelta(days=365)
            elif start_date is None or end_date is None:
                raise ValueError("请同时指定查询的起始日期和结束日期。")
            if end_date < start_date:
                raise ValueError("查询结束日期不能早于起始日期。")

        cache_key = (file_path, start_date, end_date, all_courses)
        if cache_key in self.course_cache:
            self.course_cache.move_to_end(cache_key)
            return self.course_cache[cache_key]

        with open(file_path, "r", encoding="utf-8") as file:
            calendar = Calendar.from_ical(file.read())
        shanghai_tz = timezone(timedelta(hours=8))
        range_start = (
            datetime.combine(start_date, dt_time.min, tzinfo=shanghai_tz)
            if start_date is not None else None
        )
        range_end = (
            (datetime.max.replace(tzinfo=shanghai_tz) if end_date == date.max else
             datetime.combine(end_date + timedelta(days=1), dt_time.min, tzinfo=shanghai_tz))
            if end_date is not None else None
        )
        components = calendar.walk("VEVENT")
        replacements = {}
        cancelled_series = set()
        for component in components:
            uid = str(component.get("uid", ""))
            recurrence_id = component.get("recurrence-id")
            if recurrence_id is not None:
                if not uid:
                    raise ValueError("重复课程的例外实例缺少 UID。")
                if recurrence_id.params.get("RANGE") == "THISANDFUTURE":
                    raise ValueError("暂不支持一次修改后续所有重复课程的课表。")
                key = (uid, self._local_datetime(recurrence_id.dt, shanghai_tz))
                previous = replacements.get(key)
                if previous is None or component.get("sequence", 0) >= previous.get("sequence", 0):
                    replacements[key] = component
            elif uid and str(component.get("status", "")).upper() == "CANCELLED":
                cancelled_series.add(uid)

        courses = []
        for component in components:
            uid = str(component.get("uid", ""))
            recurrence_id = component.get("recurrence-id")
            if uid in cancelled_series or str(component.get("status", "")).upper() == "CANCELLED":
                continue
            if recurrence_id is not None:
                key = (uid, self._local_datetime(recurrence_id.dt, shanghai_tz))
                if replacements[key] is not component:
                    continue
            dtstart = self._local_datetime(component.get("dtstart").dt, shanghai_tz)
            dtend = self._local_datetime(component.get("dtend").dt, shanghai_tz)
            duration = dtend - dtstart
            if duration <= timedelta():
                raise ValueError("课程结束时间必须晚于开始时间。")

            occurrences = rruleset()
            occurrences.rdate(dtstart)
            rules = component.get("rrule", [])
            if not isinstance(rules, list):
                rules = [rules]
            for rule_data in rules:
                if all_courses and "UNTIL" not in rule_data and "COUNT" not in rule_data:
                    raise ValueError("重复课程没有结束日期或次数，无法统计全学期。")
                # 使用上海时区展开，避免凌晨课程的 BYDAY 被 UTC 日期偏移。
                normalized_rule = type(rule_data)()
                for name, values in rule_data.items():
                    normalized_rule[name] = list(values)
                if "UNTIL" in normalized_rule:
                    until = normalized_rule["UNTIL"][0]
                    if isinstance(until, date) and not isinstance(until, datetime):
                        until = datetime.combine(until, dt_time.max)
                    normalized_rule["UNTIL"] = [
                        self._local_datetime(until, shanghai_tz).astimezone(timezone.utc)
                    ]
                occurrences.rrule(
                    rrulestr(normalized_rule.to_ical().decode(), dtstart=dtstart)
                )
            for property_name, add_occurrence in (
                ("rdate", occurrences.rdate), ("exdate", occurrences.exdate)
            ):
                values = component.get(property_name, [])
                if not isinstance(values, list):
                    values = [values]
                for value in values:
                    for item in value.dts:
                        add_occurrence(self._local_datetime(item.dt, shanghai_tz))
            if recurrence_id is None:
                for replacement_uid, replacement_date in replacements:
                    if replacement_uid == uid:
                        occurrences.exdate(replacement_date)

            for occurrence_number, occurrence in enumerate(occurrences, start=1):
                if occurrence_number > 10000:
                    raise ValueError("课程重复规则展开超过 10000 次，请缩短课表范围或检查重复规则。")
                if range_end is not None and occurrence >= range_end:
                    break
                occurrence_end = occurrence + duration
                if range_start is not None and occurrence_end <= range_start:
                    continue
                courses.append({
                    "summary": component.get("summary"),
                    "description": component.get("description"),
                    "location": component.get("location"),
                    "start_time": occurrence,
                    "end_time": occurrence_end,
                })
                if len(courses) > 10000:
                    raise ValueError("查询范围内的课程超过 10000 次，请检查课表的重复规则。")

        courses.sort(key=lambda course: course["start_time"])
        self.course_cache[cache_key] = courses
        while len(self.course_cache) > 128:
            self.course_cache.popitem(last=False)
        return courses

    @staticmethod
    def _local_datetime(value, tz):
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime.combine(value, dt_time.min)
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)

    def clear_cache(self, file_path: str):
        """清除指定文件在所有查询范围下的缓存。"""
        for key in list(self.course_cache):
            if key[0] == file_path:
                self.course_cache.pop(key, None)

    def parse_wakeup_token(self, text: str) -> Optional[str]:
        """从文本中解析 WakeUp 分享口令"""
        # 同时兼容完整分享文案、纯口令以及包含 key 的分享链接。
        match = re.search(r"(?<![a-fA-F0-9])([a-fA-F0-9]{32})(?![a-fA-F0-9])", text)
        if match:
            return match.group(1).lower()
        return None

    async def fetch_wakeup_schedule(self, token: str) -> Optional[List]:
        """通过 WakeUp 6.x 的加密 getv2 协议获取课程表数据。"""
        try:
            return await fetch_wakeup_schedule(token)
        except WakeUpClientError as error:
            logger.error(f"获取 WakeUp 课表失败: {error}")
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error(f"连接 WakeUp API 失败: {error}")
        except Exception as error:
            logger.error(f"解析 WakeUp 课表时发生异常: {error}")
        return None

    def convert_wakeup_to_ics(self, data: List) -> Optional[str]:
        """将 WakeUp JSON 数据转换为 ICS 格式"""
        try:
            # Wakeup 的数据是多个 JSON 对象拼接成的字符串，需要分割
            # data 已经是 fetch_wakeup_schedule 解析后的列表了
            time_table_info = data[1]  # 时间表
            schedule_settings = data[2]  # 课表设置
            course_definitions = data[3]  # 课程定义
            course_arrangements = data[4]  # 课程安排

            course_id_to_name = {c["id"]: c["courseName"] for c in course_definitions}

            start_date_str = schedule_settings.get("startDate", "2023-09-04")
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()

            cal = Calendar()
            cal.add("prodid", "-//WakeUp Schedule Converter//")
            cal.add("version", "2.0")

            for arrangement in course_arrangements:
                course_def_id = arrangement.get("id")
                course_name = course_id_to_name.get(course_def_id, "未知课程")

                start_week = arrangement.get("startWeek")
                end_week = arrangement.get("endWeek")
                day_of_week = arrangement.get("day")
                start_node = arrangement.get("startNode")
                step = arrangement.get("step", 1)

                teacher = arrangement.get("teacher", "")
                room = arrangement.get("room", "")

                start_time_str = "00:00"
                end_time_str = "00:00"
                for time_slot in time_table_info:
                    if time_slot.get("node") == start_node:
                        start_time_str = time_slot.get("startTime", "00:00")
                        end_node = start_node + step - 1
                        for end_slot in time_table_info:
                            if end_slot.get("node") == end_node:
                                end_time_str = end_slot.get("endTime", "00:00")
                                break
                        break

                start_time = dt_time.fromisoformat(start_time_str)
                end_time = dt_time.fromisoformat(end_time_str)

                weekday_map = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
                byday = weekday_map[day_of_week - 1]

                first_day_offset = day_of_week - start_date.weekday() - 1
                if first_day_offset < 0:
                    first_day_offset += 7
                first_day = start_date + timedelta(
                    weeks=start_week - 1, days=first_day_offset
                )

                last_day_offset = day_of_week - start_date.weekday() - 1
                if last_day_offset < 0:
                    last_day_offset += 7
                last_day = start_date + timedelta(
                    weeks=end_week - 1, days=last_day_offset
                )
                until_datetime = datetime.combine(last_day, end_time)

                event = Event()
                event.add("summary", course_name)
                event.add("dtstart", datetime.combine(first_day, start_time))
                event.add("dtend", datetime.combine(first_day, end_time))
                event.add(
                    "rrule", {"freq": "weekly", "byday": byday, "until": until_datetime}
                )
                event.add("location", room)
                event.add("description", f"教师: {teacher}")

                cal.add_component(event)

            return cal.to_ical().decode("utf-8")

        except Exception as e:
            logger.error(f"转换 WakeUp 数据到 ICS 失败: {e}")
            return None
