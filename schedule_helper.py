import os
from datetime import datetime, timezone, timedelta

from astrbot.api import logger



class ScheduleHelper:
    """课表查询辅助类，包含通用的课表获取和验证逻辑"""
    
    def __init__(self, data_manager, ics_parser, image_generator, user_data):
        self.data_manager = data_manager
        self.ics_parser = ics_parser
        self.image_generator = image_generator
        self.user_data = user_data

    async def get_schedule_for_date(
        self, event, target_date, date_description, user_id=None, include_all=False
    ):
        """查询当前群内某位成员的课程，显式日期可包含当天已结束课程。"""
        user_id = user_id or event.get_sender_id()
        group_id = event.get_group_id()
        group_users = self.user_data.get(group_id, {}).get("users", {})
        if not group_id or user_id not in group_users:
            if user_id != event.get_sender_id():
                return None, "该成员还没有在这个群绑定课表。"
            return None, "你还没有在这个群绑定课表哦，请在群内发送 /绑定课表 指令，然后发送 .ics 文件来绑定。"

        ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
        if not os.path.exists(ics_file_path):
            return None, "课表文件不存在，可能已被删除。请重新绑定。"
        try:
            courses = self.ics_parser.parse_ics_file(
                str(ics_file_path), start_date=target_date, end_date=target_date
            )
        except Exception as error:
            logger.error(
                f"解析课表失败，用户: {user_id}，群: {group_id}，文件: {ics_file_path}，错误: {error}"
            )
            return None, "课表文件解析失败，请重新绑定课表。"

        now = datetime.now(timezone(timedelta(hours=8)))
        nickname = group_users[user_id].get("nickname", user_id)
        target_courses = []
        for course in courses:
            if course["start_time"].date() != target_date:
                continue
            if not include_all and target_date == now.date() and course["start_time"] <= now:
                continue
            course_copy = dict(course)
            course_copy["nickname"] = nickname
            target_courses.append(course_copy)
        if not target_courses:
            date_text = {"的今日课程": "今天", "的明日课程": "明天"}.get(
                date_description, target_date.isoformat()
            )
            name = "你" if user_id == event.get_sender_id() else nickname
            return None, f"{name}在{date_text}没有课啦！"
        target_courses.sort(key=lambda course: course["start_time"])
        return target_courses, None

    async def get_group_schedule_for_date(
        self, event, target_date, is_today=True, reference_time=None
    ):
        """按指定日期及时刻查询群友当前课程或下一节课程。"""
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            return None, "本群还没有人绑定课表哦。"
        shanghai_tz = timezone(timedelta(hours=8))
        now = reference_time or datetime.now(shanghai_tz)
        use_current_time = is_today or reference_time is not None
        next_courses = []
        parse_failed = False
        for user_id, user_info in self.user_data[group_id].get("users", {}).items():
            ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            if not os.path.exists(ics_file_path):
                continue
            try:
                courses = self.ics_parser.parse_ics_file(
                    str(ics_file_path), start_date=target_date, end_date=target_date
                )
            except Exception as error:
                logger.error(
                    f"解析课表失败，用户: {user_id}，群: {group_id}，文件: {ics_file_path}，错误: {error}"
                )
                parse_failed = True
                continue

            if use_current_time:
                current_courses = [
                    course for course in courses
                    if course["start_time"] <= now < course["end_time"]
                ]
                future_courses = [
                    course for course in courses if course["start_time"] > now
                ]
                selected = current_courses or future_courses
            else:
                selected = [
                    course for course in courses
                    if course["start_time"].date() == target_date
                ]
            user_next_course = min(selected, key=lambda course: course["start_time"]) if selected else None
            nickname = user_info.get("nickname", user_id)
            if user_next_course:
                user_course = dict(user_next_course)
                user_course.update(user_id=user_id, nickname=nickname)
            else:
                user_course = {
                    "summary": "当前及后续无课" if use_current_time else "当天无课",
                    "description": "", "location": "",
                    "start_time": None, "end_time": None,
                    "user_id": user_id, "nickname": nickname,
                }
            next_courses.append(user_course)

        if not next_courses:
            if parse_failed:
                return None, "群课表文件解析失败，请相关成员重新绑定课表。"
            return None, "本群没有可查询的课表。"
        next_courses.sort(key=lambda course: (course["start_time"] is None, course["start_time"]))
        return next_courses, None
