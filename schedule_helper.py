import asyncio
import os
import re
from datetime import date, datetime, timezone, timedelta

PRIVATE_SCOPE_ID = "private"
SHANGHAI_TZ = timezone(timedelta(hours=8))
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_ALIASES = {
    "周一": 0,
    "星期一": 0,
    "礼拜一": 0,
    "周二": 1,
    "星期二": 1,
    "礼拜二": 1,
    "周三": 2,
    "星期三": 2,
    "礼拜三": 2,
    "周四": 3,
    "星期四": 3,
    "礼拜四": 3,
    "周五": 4,
    "星期五": 4,
    "礼拜五": 4,
    "周六": 5,
    "星期六": 5,
    "礼拜六": 5,
    "周日": 6,
    "星期日": 6,
    "星期天": 6,
    "礼拜日": 6,
    "礼拜天": 6,
    "周天": 6,
}


class ScheduleHelper:
    """课表查询辅助类，包含通用的课表获取和验证逻辑"""

    def __init__(self, data_manager, ics_parser, image_generator, user_data):
        self.data_manager = data_manager
        self.ics_parser = ics_parser
        self.image_generator = image_generator
        self.user_data = user_data

    @staticmethod
    def get_scope_id(event) -> str:
        """获取当前事件的课表作用域。群聊使用群号，私聊统一使用 private。"""
        return event.get_group_id() or PRIVATE_SCOPE_ID

    @staticmethod
    def is_private_scope(scope_id: str) -> bool:
        return scope_id == PRIVATE_SCOPE_ID

    @staticmethod
    def get_group_only_message() -> str:
        return "该功能仅支持群聊使用。"

    def get_bind_hint(self, scope_id: str) -> str:
        if self.is_private_scope(scope_id):
            return (
                "你还没有绑定课表哦，请先发送 /绑定课表，"
                "然后在当前私聊发送 .ics 文件或 WakeUp 分享口令。"
            )
        return (
            "你还没有在这个群绑定课表哦，请在群内发送 /绑定课表 指令，"
            "然后发送 .ics 文件或 WakeUp 分享口令来绑定。"
        )

    @staticmethod
    def get_today() -> date:
        return datetime.now(SHANGHAI_TZ).date()

    @staticmethod
    def _normalize_when(when: str | None) -> str:
        return "".join(str(when or "").strip().lower().split())

    @staticmethod
    def _normalize_date_label(label: str) -> str:
        return {"今日": "今天", "明日": "明天"}.get(label, label)

    def _build_date_context(
        self,
        target_date: date,
        when_key: str,
        date_text: str,
        title_suffix: str,
        group_date_type: str,
        group_title: str,
        ranking_title: str,
    ) -> dict:
        return {
            "target_date": target_date,
            "when_key": when_key,
            "date_text": date_text,
            "title_suffix": title_suffix,
            "group_date_type": group_date_type,
            "group_title": group_title,
            "empty_label": self._normalize_date_label(date_text),
            "ranking_title": ranking_title,
        }

    def _build_named_date_context(
        self,
        target_date: date,
        when_key: str,
        date_text: str,
        title_suffix: str,
        group_date_type: str,
        group_title: str,
        ranking_title: str,
    ) -> dict:
        return self._build_date_context(
            target_date=target_date,
            when_key=when_key,
            date_text=date_text,
            title_suffix=title_suffix,
            group_date_type=group_date_type,
            group_title=group_title,
            ranking_title=ranking_title,
        )

    def _build_custom_date_context(self, target_date: date, display_label: str) -> dict:
        return self._build_date_context(
            target_date=target_date,
            when_key="custom",
            date_text=display_label,
            title_suffix=f"的{display_label}课程",
            group_date_type=display_label,
            group_title=f"群友{display_label}第一节课程",
            ranking_title=f"{display_label}上课排行榜",
        )

    def _resolve_weekday_date(self, token: str, today: date) -> tuple[date, str] | None:
        prefix = ""
        weekday_token = token
        for candidate in ("这周", "本周", "这星期", "本星期", "下周", "下星期"):
            if token.startswith(candidate):
                prefix = candidate
                weekday_token = token[len(candidate) :]
                break

        if weekday_token not in WEEKDAY_ALIASES:
            return None

        target_weekday = WEEKDAY_ALIASES[weekday_token]
        weekday_delta = target_weekday - today.weekday()

        if prefix in {"下周", "下星期"}:
            weekday_delta += 7
        elif prefix not in {"这周", "本周", "这星期", "本星期"}:
            weekday_delta %= 7

        return today + timedelta(days=weekday_delta), WEEKDAY_NAMES[target_weekday]

    def _resolve_calendar_date(self, token: str, today: date) -> tuple[date, str] | None:
        full_match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", token)
        if full_match:
            year, month, day = map(int, full_match.groups())
            return date(year, month, day), f"{year:04d}-{month:02d}-{day:02d}"

        short_match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", token)
        if short_match:
            month, day = map(int, short_match.groups())
            target_date = date(today.year, month, day)
            return target_date, target_date.strftime("%Y-%m-%d")

        return None

    def resolve_target_date(
        self, when: str | None, default: str = "today"
    ) -> tuple[dict | None, str | None]:
        """解析自然语言日期，返回统一的日期上下文。"""
        token = self._normalize_when(when or default)
        if not token:
            token = default

        today = self.get_today()
        special_date_map = {
            "today": self._build_named_date_context(
                today,
                "today",
                "今天",
                "的今日课程",
                "today",
                "群友当前 / 下一节课程",
                "今日上课排行榜",
            ),
            "今天": self._build_named_date_context(
                today,
                "today",
                "今天",
                "的今日课程",
                "today",
                "群友当前 / 下一节课程",
                "今日上课排行榜",
            ),
            "tomorrow": self._build_named_date_context(
                today + timedelta(days=1),
                "tomorrow",
                "明天",
                "的明日课程",
                "tomorrow",
                "群友明日第一节课程",
                "明日上课排行榜",
            ),
            "明天": self._build_named_date_context(
                today + timedelta(days=1),
                "tomorrow",
                "明天",
                "的明日课程",
                "tomorrow",
                "群友明日第一节课程",
                "明日上课排行榜",
            ),
            "day_after_tomorrow": self._build_named_date_context(
                today + timedelta(days=2),
                "day_after_tomorrow",
                "后天",
                "的后天课程",
                "后天",
                "群友后天第一节课程",
                "后天上课排行榜",
            ),
            "dayaftertomorrow": self._build_named_date_context(
                today + timedelta(days=2),
                "day_after_tomorrow",
                "后天",
                "的后天课程",
                "后天",
                "群友后天第一节课程",
                "后天上课排行榜",
            ),
            "后天": self._build_named_date_context(
                today + timedelta(days=2),
                "day_after_tomorrow",
                "后天",
                "的后天课程",
                "后天",
                "群友后天第一节课程",
                "后天上课排行榜",
            ),
            "后日": self._build_named_date_context(
                today + timedelta(days=2),
                "day_after_tomorrow",
                "后天",
                "的后天课程",
                "后天",
                "群友后天第一节课程",
                "后天上课排行榜",
            ),
        }
        if token in special_date_map:
            return special_date_map[token], None

        weekday_result = self._resolve_weekday_date(token, today)
        if weekday_result:
            target_date, display_label = weekday_result
            return self._build_custom_date_context(target_date, display_label), None

        try:
            calendar_result = self._resolve_calendar_date(token, today)
            if calendar_result:
                target_date, display_label = calendar_result
                return self._build_custom_date_context(target_date, display_label), None
        except ValueError:
            return None, "无法识别日期，请使用今天/明天/后天、周几，或 YYYY-MM-DD 这类日期格式。"

        return None, (
            "参数 when 支持 today、tomorrow、day_after_tomorrow、今天、明天、后天、"
            "周几，或 YYYY-MM-DD / YYYY/MM/DD / M-D / M/D。"
        )

    def get_date_range_text(self, start_date: date, end_date: date) -> str:
        if start_date == end_date:
            return f"统计日期：{start_date.strftime('%Y/%m/%d')}"
        return f"统计时间：{start_date.strftime('%Y/%m/%d')} - {end_date.strftime('%Y/%m/%d')}"

    async def get_schedule_for_date(self, event, target_date, date_description):
        """根据指定日期获取个人课程安排，包含完整的用户验证逻辑"""
        user_id = event.get_sender_id()
        scope_id = self.get_scope_id(event)

        if (
            scope_id not in self.user_data
            or user_id not in self.user_data[scope_id].get("users", {})
        ):
            return None, self.get_bind_hint(scope_id)

        ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)
        if not os.path.exists(ics_file_path):
            return None, "课表文件不存在，可能已被删除。请重新绑定。"

        courses = await asyncio.to_thread(
            self.ics_parser.parse_ics_file, str(ics_file_path)
        )

        target_courses = []
        now = datetime.now(SHANGHAI_TZ)
        for course in courses:
            if course["start_time"].date() == target_date:
                # Only filter by current time for today
                if target_date == now.date():
                    if course["end_time"] > now:
                        target_courses.append(course)
                else:
                    # For future dates, include all courses
                    target_courses.append(course)

        if not target_courses:
            date_label = date_description.removeprefix("的").removesuffix("课程")
            date_label = self._normalize_date_label(date_label)
            return None, f"你{date_label}没有课啦！"

        # Sort courses by start time
        target_courses.sort(key=lambda x: x["start_time"])

        # Add nickname to each course for image generation
        for course in target_courses:
            nickname = (
                self.user_data[scope_id]["users"]
                .get(user_id, {})
                .get("nickname", user_id)
            )
            course["nickname"] = nickname

        return target_courses, None

    async def get_group_schedule_for_date(
        self,
        event,
        target_date,
        is_today=True,
        empty_label: str | None = None,
    ):
        """根据指定日期获取群友课程安排

        Args:
            event: 消息事件
            target_date: 目标日期
            is_today: 是否为今天，True时优先显示正在进行的课程，False时显示最早的课程
            empty_label: 无课时使用的日期标签，如“今日”“明日”“后天”

        Returns:
            tuple: (课程列表, 错误信息)
        """
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            return None, "本群还没有人绑定课表哦。"

        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        next_courses = []

        group_users = self.user_data[group_id].get("users", {})
        for user_id, user_info in group_users.items():
            nickname = user_info.get("nickname", user_id)
            ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            if not os.path.exists(ics_file_path):
                continue

            courses = await asyncio.to_thread(
                self.ics_parser.parse_ics_file, str(ics_file_path)
            )

            # 筛选目标日期的课程
            target_date_courses = [
                c
                for c in courses
                if c.get("start_time") and c.get("start_time").date() == target_date
            ]

            user_next_course = None
            if is_today:
                # 今天的方法：优先找正在进行的课程，否则找接下来的课程
                user_current_course = None
                user_future_course = None

                for course in target_date_courses:
                    start_time = course.get("start_time")
                    end_time = course.get("end_time")

                    if start_time and end_time:
                        # 检查是否是正在进行的课程
                        if start_time <= now < end_time:
                            user_current_course = course
                            break  # 找到正在上的课，就不需要再找下一节了

                        # 检查是否是未来的课程
                        elif start_time > now:
                            if (
                                user_future_course is None
                                or start_time < user_future_course.get("start_time")
                            ):
                                user_future_course = course

                # 优先显示正在上的课
                user_next_course = (
                    user_current_course if user_current_course else user_future_course
                )
            else:
                # 明天的方法：找最早的一节课
                for course in target_date_courses:
                    start_time = course.get("start_time")
                    if start_time:
                        # 找到最早的课程
                        if user_next_course is None or start_time < user_next_course.get(
                            "start_time"
                        ):
                            user_next_course = course

            # 无论用户当天是否有课，都为他创建一个条目
            if user_next_course:
                # 用户有课
                user_course_copy = {
                    "summary": user_next_course["summary"],
                    "description": user_next_course["description"],
                    "location": user_next_course["location"],
                    "start_time": user_next_course["start_time"],
                    "end_time": user_next_course["end_time"],
                    "user_id": user_id,
                    "nickname": nickname,
                }
            else:
                # 用户当天没课
                normalized_label = self._normalize_date_label(empty_label or ("今天" if is_today else "明天"))
                summary = f"{normalized_label}无课"
                user_course_copy = {
                    "summary": summary,
                    "description": "",
                    "location": "",
                    "start_time": None,  # 标记为无课
                    "end_time": None,
                    "user_id": user_id,
                    "nickname": nickname,
                }
            next_courses.append(user_course_copy)

        if not next_courses:
            normalized_label = self._normalize_date_label(empty_label or ("接下来" if is_today else "当天"))
            return None, f"群友们{normalized_label}都没有课啦！"

        # 排序时，将无课的用户（start_time is None）排在最后
        next_courses.sort(key=lambda x: (x["start_time"] is None, x["start_time"]))

        return next_courses, None

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(str(value).split())

    def format_personal_schedule_text(self, courses, title_suffix: str) -> str:
        """将个人课表格式化为适合 LLM 返回的文本。"""
        if not courses:
            return "没有可展示的课程。"

        nickname = self._normalize_text(courses[0].get("nickname")) or "你"
        lines = [f"{nickname}{title_suffix}："]

        for index, course in enumerate(courses, start=1):
            summary = self._normalize_text(course.get("summary")) or "未命名课程"
            location = self._normalize_text(course.get("location"))
            description = self._normalize_text(course.get("description"))
            start_time = course.get("start_time")
            end_time = course.get("end_time")
            time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"

            lines.append(f"{index}. {summary}")
            lines.append(f"时间：{time_str}")
            if location:
                lines.append(f"地点：{location}")
            if description:
                lines.append(f"备注：{description}")

        return "\n".join(lines)

    def format_group_schedule_text(self, courses, is_today: bool = True, title: str | None = None) -> str:
        """将群友课表格式化为适合 LLM 返回的文本。"""
        if not courses:
            return "没有可展示的群友课表。"

        now = datetime.now(SHANGHAI_TZ)
        title = title or ("群友当前 / 下一节课程" if is_today else "群友明日第一节课程")
        lines = [f"{title}："]

        for index, course in enumerate(courses, start=1):
            nickname = self._normalize_text(course.get("nickname")) or "未命名群友"
            summary = self._normalize_text(course.get("summary")) or "无课程"
            location = self._normalize_text(course.get("location"))
            start_time = course.get("start_time")
            end_time = course.get("end_time")

            if not start_time or not end_time:
                lines.append(f"{index}. {nickname}：{summary}")
                continue

            if is_today:
                status = "正在上"
                if start_time > now:
                    status = "下一节"
            else:
                status = "第一节"

            time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
            course_line = f"{index}. {nickname}：{status} {summary}（{time_str}）"
            if location:
                course_line += f" @ {location}"
            lines.append(course_line)

        return "\n".join(lines)

    def format_ranking_text(
        self,
        ranking_data,
        start_of_week,
        end_of_week,
        title: str = "本周上课排行榜",
    ) -> str:
        """将排行榜数据格式化为适合 LLM 返回的文本。"""
        if not ranking_data:
            return "当前条件下大家都没有课呢！"

        lines = [
            f"{title}：",
            self.get_date_range_text(start_of_week, end_of_week),
        ]

        for index, item in enumerate(ranking_data, start=1):
            nickname = self._normalize_text(item.get("nickname")) or str(item.get("user_id", "未知用户"))
            total_duration = item.get("total_duration")
            total_hours = int(total_duration.total_seconds() // 3600)
            remaining_minutes = int((total_duration.total_seconds() % 3600) // 60)
            lines.append(
                f"{index}. {nickname}：{total_hours} 小时 {remaining_minutes} 分钟，共 {item.get('course_count', 0)} 节课"
            )

        return "\n".join(lines)
