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
from .query_utils import command_arguments, mentioned_user, parse_date, parse_group_moment

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
        """查看自己或 @成员的课表，可指定日期。"""
        now = datetime.now(SHANGHAI_TZ)
        try:
            arguments = command_arguments(event, "查看课表")
            user_id = mentioned_user(event) or event.get_sender_id()
            target_date = parse_date(arguments, now) if arguments else now.date()
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        description = f"在{target_date.isoformat()}的课程" if arguments else "的今日课程"
        courses, error_msg = await self.schedule_helper.get_schedule_for_date(
            event, target_date, description,
            user_id=user_id, include_all=bool(arguments)
        )
        if error_msg:
            yield event.plain_result(error_msg)
            return
        nickname = courses[0].get("nickname") or user_id
        image_path = await self.image_generator.generate_user_schedule_image(
            courses, nickname, description
        )
        yield event.image_result(image_path)

    @filter.command("查看明日课表")
    async def show_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看自己明天的课表。"""
        tomorrow = datetime.now(SHANGHAI_TZ).date() + timedelta(days=1)
        courses, error_msg = await self.schedule_helper.get_schedule_for_date(
            event, tomorrow, "的明日课程", include_all=True
        )
        if error_msg:
            yield event.plain_result(error_msg)
            return
        image_path = await self.image_generator.generate_user_schedule_image(
            courses, courses[0].get("nickname") or event.get_sender_name(), "的明日课程"
        )
        yield event.image_result(image_path)

    @filter.command("群友在上什么课")
    async def show_group_now_schedule(self, event: AstrMessageEvent):
        """查询指定日期及时刻群友正在上或接下来的一节课。"""
        now = datetime.now(SHANGHAI_TZ)
        try:
            arguments = command_arguments(event, "群友在上什么课")
            target_date, reference_time, date_type = parse_group_moment(arguments, now)
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(
            event, target_date, reference_time=reference_time
        )
        if error_msg:
            yield event.plain_result(error_msg)
            return
        image_path = await self.image_generator.generate_schedule_image(
            courses, date_type=date_type, reference_time=reference_time
        )
        yield event.image_result(image_path)

    @filter.command("群友明天上什么课")
    async def show_group_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看群友明天的第一节课。"""
        tomorrow = datetime.now(SHANGHAI_TZ).date() + timedelta(days=1)
        courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(
            event, tomorrow, is_today=False
        )
        if error_msg:
            yield event.plain_result(error_msg)
            return
        image_path = await self.image_generator.generate_schedule_image(courses, date_type="tomorrow")
        yield event.image_result(image_path)

    @filter.command("本周上课排行")
    async def weekly_course_ranking(self, event: AstrMessageEvent):
        """生成本周上课排行榜。"""
        async for result in self._course_ranking(event):
            yield result

    @filter.command("上周上课排行")
    async def previous_week_course_ranking(self, event: AstrMessageEvent):
        """生成上周上课排行榜。"""
        async for result in self._course_ranking(event, week_offset=-1):
            yield result

    @filter.command("下周上课排行")
    async def next_week_course_ranking(self, event: AstrMessageEvent):
        """生成下周上课排行榜。"""
        async for result in self._course_ranking(event, week_offset=1):
            yield result

    @filter.command("全学期上课排行")
    async def semester_course_ranking(self, event: AstrMessageEvent):
        """统计各成员当前绑定 ICS 中的全部课程。"""
        async for result in self._course_ranking(event, all_courses=True):
            yield result

    async def _course_ranking(self, event, week_offset=0, all_courses=False):
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            yield event.plain_result("本群还没有人绑定课表哦。")
            return

        if all_courses:
            start_date = end_date = None
            period_name = "全学期"
        else:
            today = datetime.now(SHANGHAI_TZ).date()
            start_date = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
            end_date = start_date + timedelta(days=6)
            period_name = {-1: "上周", 0: "本周", 1: "下周"}[week_offset]
        ranking_data = []
        parse_failed = False
        for user_id, user_info in self.user_data[group_id].get("users", {}).items():
            file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            if not os.path.exists(file_path):
                parse_failed = True
                continue
            try:
                courses = self.ics_parser.parse_ics_file(
                    str(file_path), start_date=start_date, end_date=end_date,
                    all_courses=all_courses
                )
            except Exception as error:
                logger.error(
                    f"统计课表失败，用户: {user_id}，群: {group_id}，文件: {file_path}，错误: {error}"
                )
                parse_failed = True
                continue
            selected_courses = [
                course for course in courses
                if all_courses or start_date <= course["start_time"].date() <= end_date
            ]
            if selected_courses:
                ranking_data.append({
                    "user_id": user_id,
                    "nickname": user_info.get("nickname", user_id),
                    "total_duration": sum(
                        (course["end_time"] - course["start_time"] for course in selected_courses),
                        timedelta()
                    ),
                    "course_count": len(selected_courses),
                })
        if not ranking_data:
            message = (
                f"有课表文件解析失败或无法完整统计，暂时无法生成{period_name}排行。"
                if parse_failed else f"{period_name}大家都没有课呢！"
            )
            if all_courses and parse_failed:
                message += "全学期统计要求重复课程具有结束日期或次数，单次最多展开 10000 次。"
            yield event.plain_result(message)
            return

        ranking_data.sort(key=lambda row: row["total_duration"], reverse=True)
        if all_courses and parse_failed:
            yield event.plain_result(
                "部分成员课表无法完整展开，已跳过，排行可能不完整。"
                "全学期统计要求重复课程具有结束日期或次数，单次最多展开 10000 次。"
            )
        subtitle = "各成员当前绑定课表中的全部课程" if all_courses else None
        image_path = await self.image_generator.generate_ranking_image(
            ranking_data, start_date, end_date,
            title=f"{period_name}上课排行榜", subtitle=subtitle
        )
        yield event.image_result(image_path)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")
