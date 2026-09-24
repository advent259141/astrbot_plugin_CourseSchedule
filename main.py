import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.core.star import Star, Context, star_map
from astrbot.core.utils.io import download_file

from .data_manager import DataManager
from .ics_parser import ICSParser
from .image_generator import ImageGenerator
from .schedule_helper import ScheduleHelper

SHANGHAI_TZ = timezone(timedelta(hours=8))

class Main(Star):
    """课程表插件"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context
        self.data_manager = DataManager(star_map[self.__module__])
        self.ics_parser = ICSParser()
        self.image_generator = ImageGenerator()
        self.user_data = self.data_manager.load_user_data()
        self.schedule_helper = ScheduleHelper(self.data_manager, self.ics_parser, self.image_generator, self.user_data)
        self.binding_requests: Dict[str, Dict] = {}

    def _validate_and_replace_ics(self, temporary_path, ics_file_path):
        """只在新课表能正常解析后替换已有文件。"""
        try:
            self.ics_parser.parse_ics_file(str(temporary_path))
        finally:
            self.ics_parser.clear_cache(str(temporary_path))

        os.replace(temporary_path, ics_file_path)
        self.ics_parser.clear_cache(str(ics_file_path))

    @filter.command("绑定课表")
    async def bind_schedule(self, event: AstrMessageEvent):
        """绑定课表"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此指令。")
            return

        user_id = event.get_sender_id()
        nickname = event.get_sender_name()

        # 记录绑定请求
        request_key = f"{group_id}-{user_id}"
        self.binding_requests[request_key] = {
            "timestamp": time.time(),
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname,
        }

        yield event.plain_result(
            "请在60秒内，在本群内直接发送你的 .ics 文件或 WakeUp 分享口令。"
        )

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def handle_wakeup_token(self, event: AstrMessageEvent):
        """处理文本消息，检查是否为 WakeUp 口令"""
        group_id = event.get_group_id()
        if not group_id:
            return

        user_id = event.get_sender_id()
        request_key = f"{group_id}-{user_id}"

        # 检查是否有绑定请求
        if request_key not in self.binding_requests:
            return

        request = self.binding_requests[request_key]

        # 检查是否超时（60秒）
        if time.time() - request["timestamp"] > 60:
            del self.binding_requests[request_key]
            return

        # 检查是否为纯文本消息
        if not event.message_str:
            return

        token = self.ics_parser.parse_wakeup_token(event.message_str)
        if not token:
            return

        try:
            json_data = await self.ics_parser.fetch_wakeup_schedule(token)
            if not json_data:
                yield event.plain_result(
                    "无法获取 WakeUp 课程表数据，请检查口令是否正确或已过期。"
                )
                return

            ics_content = self.ics_parser.convert_wakeup_to_ics(json_data)
            if not ics_content:
                yield event.plain_result("课程表数据解析失败，无法生成 ICS 文件。")
                return

            # 先验证生成的 ICS，再替换已有课表
            nickname = request.get("nickname", user_id)
            ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            fd, temporary_path = tempfile.mkstemp(suffix=".ics", dir=ics_file_path.parent)
            os.close(fd)
            try:
                with open(temporary_path, "w", encoding="utf-8") as f:
                    f.write(ics_content)
                self._validate_and_replace_ics(temporary_path, ics_file_path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)

            # --- 复用绑定成功逻辑 ---
            if group_id not in self.user_data:
                self.user_data[group_id] = {
                    "umo": event.unified_msg_origin,
                    "users": {},
                }
            elif "umo" not in self.user_data[group_id]:
                self.user_data[group_id]["umo"] = event.unified_msg_origin

            self.user_data[group_id]["users"][user_id] = {
                "nickname": nickname,
                "reminder": False,
            }
            self.data_manager.save_user_data(self.user_data)

            # 清除该用户的课表缓存
            self.ics_parser.clear_cache(str(ics_file_path))

            del self.binding_requests[request_key]
            yield event.plain_result(f"通过 WakeUp 口令绑定课表成功！群号：{group_id}")

        except Exception as e:
            logger.error(f"处理 WakeUp 口令失败，用户: {user_id}，群: {group_id}，错误: {e}")
            yield event.plain_result("处理 WakeUp 口令失败，绑定未更新。")
            del self.binding_requests[request_key]

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息，检查是否为课表绑定请求"""
        # 只处理群消息
        group_id = event.get_group_id()
        if not group_id:
            return

        user_id = event.get_sender_id()
        request_key = f"{group_id}-{user_id}"

        # 检查是否有绑定请求
        if request_key not in self.binding_requests:
            return

        request = self.binding_requests[request_key]

        # 检查是否超时（60秒）
        if time.time() - request["timestamp"] > 60:
            del self.binding_requests[request_key]
            return

        # 获取消息链中的文件组件
        messages = event.get_messages()
        file_component = None

        for message in messages:
            if hasattr(message, "type") and message.type == "File":
                file_component = message
                break

        if not file_component:
            return

        nickname = request.get("nickname", user_id)
        ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)

        temporary_path = None
        try:
            # 使用File组件的异步方法获取文件
            file_path = await file_component.get_file(allow_return_url=True)
            logger.info(f"File component returned path: {file_path}")

            if not isinstance(file_path, str) or not file_path.startswith("http"):
                del self.binding_requests[request_key]
                return

            logger.info(f"Downloading file from URL: {file_path}")
            fd, temporary_path = tempfile.mkstemp(suffix=".ics", dir=ics_file_path.parent)
            os.close(fd)
            await download_file(file_path, temporary_path)
        except Exception as e:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
            logger.error(f"获取文件信息失败: {e}")
            yield event.plain_result("无法获取课表文件，绑定失败。")
            del self.binding_requests[request_key]
            return

        try:
            if os.path.getsize(temporary_path) == 0:
                raise ValueError("下载的课表文件为空")
            self._validate_and_replace_ics(temporary_path, ics_file_path)
        except Exception as e:
            logger.error(
                f"绑定课表失败，用户: {user_id}，群: {group_id}，文件: {temporary_path}，错误: {e}"
            )
            yield event.plain_result("课表文件解析或保存失败，绑定未更新。")
            del self.binding_requests[request_key]
            return
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

        logger.info(event.message_obj.raw_message)  # 平台下发的原始消息在这里
        logger.info(f"文件下载成功，文件路径: {ics_file_path}")
        logger.info(f"文件大小: {os.path.getsize(ics_file_path)} bytes")

        # 保存用户数据
        if group_id not in self.user_data:
            self.user_data[group_id] = {"umo": event.unified_msg_origin, "users": {}}
        elif "umo" not in self.user_data[group_id]:
            self.user_data[group_id]["umo"] = event.unified_msg_origin

        self.user_data[group_id]["users"][user_id] = {
            "nickname": nickname,
            "reminder": False,
        }

        self.data_manager.save_user_data(self.user_data)

        # 清除该用户的课表缓存
        self.ics_parser.clear_cache(str(ics_file_path))

        # 删除绑定请求
        del self.binding_requests[request_key]
        yield event.plain_result(f"课表绑定成功！群号：{group_id}")

    @filter.command("查看课表")
    async def show_today_schedule(self, event: AstrMessageEvent):
        """查看今天还有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()

        courses, error_msg = await self.schedule_helper.get_schedule_for_date(event, today, "的今日课程")

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_path = await self.image_generator.generate_user_schedule_image(
            courses, event.get_sender_name(), "的今日课程"
        )
        yield event.image_result(image_path)

    @filter.command("查看明日课表")
    async def show_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看明天还有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        tomorrow = now.date() + timedelta(days=1)

        courses, error_msg = await self.schedule_helper.get_schedule_for_date(event, tomorrow, "的明日课程")

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_path = await self.image_generator.generate_user_schedule_image(courses, event.get_sender_name(), "的明日课程"
        )
        yield event.image_result(image_path)

    @filter.command("群友在上什么课")
    async def show_group_now_schedule(self, event: AstrMessageEvent):
        """查看群友接下来有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()

        next_courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(event, today, is_today=True)

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_bytes = await self.image_generator.generate_schedule_image(next_courses, date_type="today")
        yield event.image_result(image_bytes)

    @filter.command("群友明天上什么课")
    async def show_group_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看群友明天有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        tomorrow = now.date() + timedelta(days=1)  # 明天的日期

        next_courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(event, tomorrow, is_today=False)

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_bytes = await self.image_generator.generate_schedule_image(next_courses, date_type="tomorrow")
        yield event.image_result(image_bytes)

    @filter.command("本周上课排行")
    async def weekly_course_ranking(self, event: AstrMessageEvent):
        """生成本周上课排行榜"""
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            yield event.plain_result("本群还没有人绑定课表哦。")
            return

        now = datetime.now(SHANGHAI_TZ)
        today = now.date()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        ranking_data = []
        parse_failed = False
        group_users = self.user_data[group_id].get("users", {})

        for user_id, user_info in group_users.items():
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
            total_duration = timedelta()
            course_count = 0

            for course in courses:
                course_date = course["start_time"].date()
                if start_of_week <= course_date <= end_of_week:
                    total_duration += course["end_time"] - course["start_time"]
                    course_count += 1

            if course_count > 0:
                ranking_data.append(
                    {
                        "user_id": user_id,
                        "nickname": user_info.get("nickname", user_id),
                        "total_duration": total_duration,
                        "course_count": course_count,
                    }
                )

        if not ranking_data:
            message = (
                "有课表文件解析失败，暂时无法确定完整排行。"
                if parse_failed
                else "本周大家都没有课呢！"
            )
            yield event.plain_result(message)
            return

        # 根据总时长降序排名
        ranking_data.sort(key=lambda x: x["total_duration"], reverse=True)

        image_path = await self.image_generator.generate_ranking_image(
            ranking_data, start_of_week, end_of_week
        )
        yield event.image_result(image_path)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")
