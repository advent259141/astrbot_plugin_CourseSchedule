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

    async def get_schedule_for_date(self, event, target_date, date_description):
        """根据指定日期获取个人课程安排，包含完整的用户验证逻辑"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        if (
            not group_id
            or group_id not in self.user_data
            or user_id not in self.user_data[group_id].get("users", {})
        ):
            return None, "你还没有在这个群绑定课表哦，请在群内发送 /绑定课表 指令，然后发送 .ics 文件来绑定。"

        ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
        if not os.path.exists(ics_file_path):
            return None, "课表文件不存在，可能已被删除。请重新绑定。"

        try:
            courses = self.ics_parser.parse_ics_file(str(ics_file_path))
        except Exception as error:
            logger.error(
                f"解析课表失败，用户: {user_id}，群: {group_id}，文件: {ics_file_path}，错误: {error}"
            )
            return None, "课表文件解析失败，请重新绑定课表。"

        target_courses = []
        for course in courses:
            if course["start_time"].date() == target_date:
                # Only filter by current time for today
                if target_date == datetime.now(timezone(timedelta(hours=8))).date():
                    if course["start_time"] > datetime.now(timezone(timedelta(hours=8))):
                        target_courses.append(course)
                else:
                    # For future dates, include all courses
                    target_courses.append(course)

        if not target_courses:
            # Map date_description to short form for error message
            date_map = {
                "的今日课程": "今天",
                "的明日课程": "明天"
            }
            date_str = date_map.get(date_description)
            return None, f"你{date_str}没有课啦！"

        # Sort courses by start time
        target_courses.sort(key=lambda x: x["start_time"])

        # Add nickname to each course for image generation
        for course in target_courses:
            nickname = (
                self.user_data[group_id]["users"]
                .get(user_id, {})
                .get("nickname", user_id)
            )
            course["nickname"] = nickname

        return target_courses, None

    async def get_group_schedule_for_date(self, event, target_date, is_today=True):
        """根据指定日期获取群友课程安排

        Args:
            event: 消息事件
            target_date: 目标日期
            is_today: 是否为今天，True时优先显示正在进行的课程，False时显示最早的课程

        Returns:
            tuple: (课程列表, 错误信息)
        """
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            return None, "本群还没有人绑定课表哦。"

        # 使用上海时区 (UTC+8)
        shanghai_tz = timezone(timedelta(hours=8))
        now = datetime.now(shanghai_tz)
        next_courses = []
        parse_failed = False

        group_users = self.user_data[group_id].get("users", {})
        for user_id, user_info in group_users.items():
            nickname = user_info.get("nickname", user_id)
            ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            if not os.path.exists(ics_file_path):
                continue

            try:
                courses = self.ics_parser.parse_ics_file(str(ics_file_path))
            except Exception as error:
                logger.error(
                    f"解析课表失败，用户: {user_id}，群: {group_id}，文件: {ics_file_path}，错误: {error}"
                )
                parse_failed = True
                continue

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
                        if user_next_course is None or start_time < user_next_course.get("start_time"):
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
                summary = "今日无课" if is_today else "明日无课"
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
            if parse_failed:
                return None, "群课表文件解析失败，请相关成员重新绑定课表。"
            return None, f"群友们{'接下来都没有课啦！' if is_today else '明天都没有课啦！'}"

        # 排序时，将无课的用户（start_time is None）排在最后
        next_courses.sort(key=lambda x: (x["start_time"] is None, x["start_time"]))

        return next_courses, None