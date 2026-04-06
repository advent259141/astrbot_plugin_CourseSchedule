import asyncio
import os
import shutil
import time
from datetime import datetime, timezone, timedelta
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.core.star import Star, Context, star_map
from astrbot.core.utils.io import download_file

from .data_manager import DataManager
from .ics_parser import ICSParser
from .image_generator import ImageGenerator
from .schedule_helper import ScheduleHelper

SHANGHAI_TZ = timezone(timedelta(hours=8))
COURSE_SCHEDULE_LLM_HINT = (
    "你具备课表查询能力：当用户询问今天/明天/后天/周几/具体日期有什么课、"
    "群友现在或某天上什么课、或者谁今天/这周课最多时，应优先调用课表相关函数工具。"
    "这类课表问题应优先使用 course_schedule_query_personal、course_schedule_query_group、"
    "course_schedule_query_ranking 或 course_schedule_weekly_ranking。"
    "除非用户明确要求写代码、执行 Python、分析原始 .ics 文件，否则不要改用 Python 执行器、"
    "网页搜索或抓取网页来处理课表问题。"
    "如果用户尚未绑定课表，请明确告知：发送 /绑定课表 后，在当前会话内发送 .ics 文件"
    "或 WakeUp 分享口令即可完成绑定。个人课表支持群聊和私聊，群友查询与排行仅支持群聊。"
)

class Main(Star):
    """课程表插件"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context
        self.data_manager = DataManager(star_map[self.__module__])
        self.ics_parser = ICSParser()
        self.image_generator = ImageGenerator()
        self.user_data = self.data_manager.load_user_data()
        self.schedule_helper = ScheduleHelper(
            self.data_manager,
            self.ics_parser,
            self.image_generator,
            self.user_data,
        )
        self.binding_requests: Dict[str, Dict] = {}

    def _get_scope_id(self, event: AstrMessageEvent) -> str:
        return self.schedule_helper.get_scope_id(event)

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        return f"{self._get_scope_id(event)}-{event.get_sender_id()}"

    def _get_session_label(self, event: AstrMessageEvent) -> str:
        return "当前私聊" if event.is_private_chat() else "当前群聊"

    def _ensure_scope_entry(self, scope_id: str, event: AstrMessageEvent) -> None:
        if scope_id not in self.user_data:
            self.user_data[scope_id] = {"umo": event.unified_msg_origin, "users": {}}
        elif "umo" not in self.user_data[scope_id]:
            self.user_data[scope_id]["umo"] = event.unified_msg_origin

    def _save_binding_record(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        user_id: str,
        nickname: str,
    ) -> None:
        self._ensure_scope_entry(scope_id, event)
        self.user_data[scope_id]["users"][user_id] = {
            "nickname": nickname,
            "reminder": False,
            "umo": event.unified_msg_origin,
        }
        self.data_manager.save_user_data(self.user_data)

    @staticmethod
    def _normalize_intent_text(text: str | None) -> str:
        return "".join(str(text or "").strip().lower().split())

    def _is_course_schedule_intent(self, text: str | None) -> bool:
        normalized_text = self._normalize_intent_text(text)
        if not normalized_text:
            return False

        keywords = (
            "课表",
            "上什么课",
            "有什么课",
            "今天课",
            "明天课",
            "后天课",
            "周一课",
            "周二课",
            "周三课",
            "周四课",
            "周五课",
            "周六课",
            "周日课",
            "课程安排",
            "群友在上什么课",
            "群友明天上什么课",
            "群友后天上什么课",
            "谁课最多",
            "谁今天课最多",
            "谁明天课最多",
            "谁后天课最多",
            "谁这周课最多",
            "谁本周课最多",
            "上课排行",
            "课程排行",
            "查看课表",
        )
        if any(keyword in normalized_text for keyword in keywords):
            return True
        return ("星期" in normalized_text or "礼拜" in normalized_text) and (
            "课" in normalized_text or "排行" in normalized_text
        )

    def _remove_competing_tools(self, request: ProviderRequest) -> None:
        if not getattr(request, "func_tool", None):
            return

        competing_tools = {
            "execute_python_code",
            "astrbot_execute_python",
            "astrbot_execute_shell",
            "fetch_url",
            "web_search",
            "search_web",
        }
        for tool_name in competing_tools:
            try:
                request.func_tool.remove_tool(tool_name)
            except Exception:
                continue

    def _get_group_query_options(self, date_context: dict) -> tuple[bool, str]:
        target_date = date_context["target_date"]
        is_realtime = target_date == self.schedule_helper.get_today()
        if is_realtime and date_context["when_key"] != "today":
            return True, f"群友{date_context['date_text']}当前 / 下一节课程"
        return is_realtime, date_context["group_title"]

    async def _send_personal_schedule_image(
        self,
        event: AstrMessageEvent,
        courses,
        title_suffix: str,
    ) -> None:
        display_name = courses[0].get("nickname") or event.get_sender_name() or "你"
        image_path = await self.image_generator.generate_user_schedule_image(
            courses,
            display_name,
            title_suffix,
        )
        try:
            await event.send(event.image_result(image_path))
        except Exception as exc:
            logger.warning(f"发送个人课表图片失败: {exc}")

    async def _send_group_schedule_image(
        self,
        event: AstrMessageEvent,
        courses,
        date_type: str,
    ) -> None:
        image_path = await self.image_generator.generate_schedule_image(
            courses, date_type=date_type
        )
        try:
            await event.send(event.image_result(image_path))
        except Exception as exc:
            logger.warning(f"发送群友课表图片失败: {exc}")

    async def _send_ranking_image(
        self,
        event: AstrMessageEvent,
        ranking_data,
        start_date,
        end_date,
        title: str,
    ) -> None:
        image_path = await self.image_generator.generate_ranking_image(
            ranking_data,
            start_date,
            end_date,
            title=title,
            subtitle=self.schedule_helper.get_date_range_text(start_date, end_date),
        )
        try:
            await event.send(event.image_result(image_path))
        except Exception as exc:
            logger.warning(f"发送课表排行图片失败: {exc}")

    async def _build_ranking_data(
        self,
        event: AstrMessageEvent,
        start_date,
        end_date,
        empty_message: str,
    ):
        group_id = event.get_group_id()
        if not group_id:
            return None, None, None, self.schedule_helper.get_group_only_message()
        if group_id not in self.user_data:
            return None, None, None, "本群还没有人绑定课表哦。"

        ranking_data = []
        group_users = self.user_data[group_id].get("users", {})

        for user_id, user_info in group_users.items():
            ics_file_path = self.data_manager.get_ics_file_path(user_id, group_id)
            if not os.path.exists(ics_file_path):
                continue

            courses = await asyncio.to_thread(
                self.ics_parser.parse_ics_file, str(ics_file_path)
            )
            total_duration = timedelta()
            course_count = 0

            for course in courses:
                course_date = course["start_time"].date()
                if start_date <= course_date <= end_date:
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
            return None, None, None, empty_message

        ranking_data.sort(key=lambda x: x["total_duration"], reverse=True)
        return ranking_data, start_date, end_date, None

    async def _build_weekly_ranking_data(self, event: AstrMessageEvent):
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        return await self._build_ranking_data(
            event,
            start_of_week,
            end_of_week,
            "本周大家都没有课呢！",
        )

    @filter.on_llm_request()
    async def inject_course_schedule_hint(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ) -> None:
        """在 LLM 请求前补充课表能力提示，帮助模型主动调用工具。"""
        if not request.func_tool:
            return
        if not self._is_course_schedule_intent(event.message_str):
            return

        self._remove_competing_tools(request)
        system_prompt = request.system_prompt or ""
        if COURSE_SCHEDULE_LLM_HINT not in system_prompt:
            request.system_prompt = f"{system_prompt}\n{COURSE_SCHEDULE_LLM_HINT}\n"

    @filter.llm_tool(name="course_schedule_query_personal")
    async def query_personal_schedule_tool(
        self,
        event: AstrMessageEvent,
        when: str = "today",
    ) -> str:
        """查询当前用户已绑定的个人课表，适合处理“我今天还有什么课”“我明天什么课”这类自然语言问题。

        Args:
            when(string): 查询日期，支持 today、tomorrow、day_after_tomorrow、今天、明天、后天、周几或具体日期
        """
        date_context, error_msg = self.schedule_helper.resolve_target_date(
            when, default="today"
        )
        if error_msg:
            return error_msg

        courses, error_msg = await self.schedule_helper.get_schedule_for_date(
            event,
            date_context["target_date"],
            date_context["title_suffix"],
        )
        if error_msg:
            return error_msg

        await self._send_personal_schedule_image(
            event, courses, date_context["title_suffix"]
        )
        return self.schedule_helper.format_personal_schedule_text(
            courses, date_context["title_suffix"]
        )

    @filter.llm_tool(name="course_schedule_query_group")
    async def query_group_schedule_tool(
        self,
        event: AstrMessageEvent,
        when: str = "today",
    ) -> str:
        """查询当前群聊内群友的课程状态，适合处理“群友现在在上什么课”“大家明天第一节上什么”这类问题。

        Args:
            when(string): 查询日期，支持 today、tomorrow、day_after_tomorrow、今天、明天、后天、周几或具体日期
        """
        if event.is_private_chat():
            return self.schedule_helper.get_group_only_message()

        date_context, error_msg = self.schedule_helper.resolve_target_date(
            when, default="today"
        )
        if error_msg:
            return error_msg
        is_today, group_title = self._get_group_query_options(date_context)

        next_courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(
            event,
            date_context["target_date"],
            is_today=is_today,
            empty_label=date_context["empty_label"],
        )
        if error_msg:
            return error_msg

        await self._send_group_schedule_image(
            event,
            next_courses,
            date_context["group_date_type"],
        )
        return self.schedule_helper.format_group_schedule_text(
            next_courses,
            is_today=is_today,
            title=group_title,
        )

    @filter.llm_tool(name="course_schedule_query_ranking")
    async def query_course_ranking_tool(
        self,
        event: AstrMessageEvent,
        when: str = "week",
    ) -> str:
        """查询群聊上课排行，适合处理“谁今天课最多”“谁这周课最多”“后天谁课最多”这类问题。

        Args:
            when(string): 查询范围，支持 week、this_week、本周、这周，或 today、tomorrow、day_after_tomorrow、周几、具体日期
        """
        when_normalized = self._normalize_intent_text(when or "week")
        if when_normalized in {"week", "this_week", "本周", "这周"}:
            ranking_data, start_date, end_date, error_msg = await self._build_weekly_ranking_data(event)
            ranking_title = "本周上课排行榜"
        else:
            date_context, error_msg = self.schedule_helper.resolve_target_date(when, default="today")
            if error_msg:
                return error_msg
            ranking_data, start_date, end_date, error_msg = await self._build_ranking_data(
                event,
                date_context["target_date"],
                date_context["target_date"],
                f"{date_context['date_text']}大家都没有课呢！",
            )
            ranking_title = date_context["ranking_title"]

        if error_msg:
            return error_msg

        await self._send_ranking_image(
            event, ranking_data, start_date, end_date, ranking_title
        )
        return self.schedule_helper.format_ranking_text(
            ranking_data,
            start_date,
            end_date,
            title=ranking_title,
        )

    @filter.llm_tool(name="course_schedule_weekly_ranking")
    async def weekly_course_ranking_tool(self, event: AstrMessageEvent) -> str:
        """查询当前群聊内本周上课排行，适合处理“本周谁课最多”“本周上课排行”这类问题。"""
        ranking_data, start_of_week, end_of_week, error_msg = await self._build_weekly_ranking_data(event)
        if error_msg:
            return error_msg
        await self._send_ranking_image(
            event,
            ranking_data,
            start_of_week,
            end_of_week,
            "本周上课排行榜",
        )
        return self.schedule_helper.format_ranking_text(
            ranking_data,
            start_of_week,
            end_of_week,
            title="本周上课排行榜",
        )

    @filter.command("绑定课表")
    async def bind_schedule(self, event: AstrMessageEvent):
        """绑定课表"""
        scope_id = self._get_scope_id(event)
        user_id = event.get_sender_id()
        nickname = event.get_sender_name()

        # 记录绑定请求
        request_key = self._get_request_key(event)
        self.binding_requests[request_key] = {
            "timestamp": time.time(),
            "scope_id": scope_id,
            "user_id": user_id,
            "nickname": nickname,
        }

        yield event.plain_result(
            f"请在60秒内，在{self._get_session_label(event)}直接发送你的 .ics 文件或 WakeUp 分享口令。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_wakeup_token(self, event: AstrMessageEvent):
        """处理文本消息，检查是否为 WakeUp 口令"""
        user_id = event.get_sender_id()
        request_key = self._get_request_key(event)

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

            ics_content = await asyncio.to_thread(
                self.ics_parser.convert_wakeup_to_ics, json_data
            )
            if not ics_content:
                yield event.plain_result("课程表数据解析失败，无法生成 ICS 文件。")
                return

            # 保存 ICS 文件
            nickname = request.get("nickname", user_id)
            scope_id = request.get("scope_id", self._get_scope_id(event))
            ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)
            with open(ics_file_path, "w", encoding="utf-8") as f:
                f.write(ics_content)

            # --- 复用绑定成功逻辑 ---
            self._save_binding_record(event, scope_id, user_id, nickname)

            # 清除该用户的课表缓存
            self.ics_parser.clear_cache(str(ics_file_path))

            del self.binding_requests[request_key]
            yield event.plain_result(
                f"通过 WakeUp 口令绑定课表成功！已绑定到{self._get_session_label(event)}。"
            )

        except Exception as e:
            logger.error(f"处理 WakeUp 口令失败: {e}")
            yield event.plain_result(f"处理 WakeUp 口令失败: {e}")
            del self.binding_requests[request_key]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息，检查是否为课表绑定请求"""
        user_id = event.get_sender_id()
        request_key = self._get_request_key(event)

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
        scope_id = request.get("scope_id", self._get_scope_id(event))
        ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)

        try:
            # 使用File组件的异步方法获取文件
            file_path = await file_component.get_file(allow_return_url=True)
            logger.info(f"File component returned path: {file_path}")

            if not isinstance(file_path, str):
                del self.binding_requests[request_key]
                return

            if file_path.startswith("http"):
                logger.info(f"Downloading file from URL: {file_path}")
                await download_file(file_path, ics_file_path)
            elif os.path.exists(file_path):
                shutil.copyfile(file_path, ics_file_path)
            else:
                del self.binding_requests[request_key]
                return
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}")
            yield event.plain_result(f"无法获取文件信息，绑定失败。错误：{str(e)}")
            del self.binding_requests[request_key]
            return

        # 检查下载的文件是否存在
        if not os.path.exists(ics_file_path):
            logger.error(f"文件下载失败，文件不存在: {ics_file_path}")
            yield event.plain_result("文件下载失败，请重试。")
            del self.binding_requests[request_key]
            return
        logger.info(f"课表文件下载成功，作用域: {scope_id}，用户: {user_id}")
        logger.info(f"文件下载成功，文件路径: {ics_file_path}")
        logger.info(f"文件大小: {os.path.getsize(ics_file_path)} bytes")

        # 保存用户数据
        self._save_binding_record(event, scope_id, user_id, nickname)

        # 清除该用户的课表缓存
        self.ics_parser.clear_cache(str(ics_file_path))

        # 删除绑定请求
        del self.binding_requests[request_key]
        yield event.plain_result(f"课表绑定成功！已绑定到{self._get_session_label(event)}。")

    @filter.command("查看课表")
    async def show_today_schedule(self, event: AstrMessageEvent):
        """查看今天还有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()

        courses, error_msg = await self.schedule_helper.get_schedule_for_date(
            event, today, "的今日课程"
        )

        if error_msg:
            yield event.plain_result(error_msg)
            return

        display_name = courses[0].get("nickname") or event.get_sender_name() or "你"
        image_path = await self.image_generator.generate_user_schedule_image(
            courses, display_name, "的今日课程"
        )
        yield event.image_result(image_path)

    @filter.command("查看明日课表")
    async def show_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看明天还有什么课"""
        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        tomorrow = now.date() + timedelta(days=1)

        courses, error_msg = await self.schedule_helper.get_schedule_for_date(
            event, tomorrow, "的明日课程"
        )

        if error_msg:
            yield event.plain_result(error_msg)
            return

        display_name = courses[0].get("nickname") or event.get_sender_name() or "你"
        image_path = await self.image_generator.generate_user_schedule_image(
            courses, display_name, "的明日课程"
        )
        yield event.image_result(image_path)

    @filter.command("群友在上什么课")
    async def show_group_now_schedule(self, event: AstrMessageEvent):
        """查看群友接下来有什么课"""
        if event.is_private_chat():
            yield event.plain_result(self.schedule_helper.get_group_only_message())
            return

        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        today = now.date()

        next_courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(
            event, today, is_today=True
        )

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_path = await self.image_generator.generate_schedule_image(
            next_courses, date_type="today"
        )
        yield event.image_result(image_path)

    @filter.command("群友明天上什么课")
    async def show_group_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看群友明天有什么课"""
        if event.is_private_chat():
            yield event.plain_result(self.schedule_helper.get_group_only_message())
            return

        # 使用上海时区 (UTC+8)
        now = datetime.now(SHANGHAI_TZ)
        tomorrow = now.date() + timedelta(days=1)  # 明天的日期

        next_courses, error_msg = await self.schedule_helper.get_group_schedule_for_date(
            event, tomorrow, is_today=False
        )

        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_path = await self.image_generator.generate_schedule_image(
            next_courses, date_type="tomorrow"
        )
        yield event.image_result(image_path)

    @filter.command("本周上课排行")
    async def weekly_course_ranking(self, event: AstrMessageEvent):
        """生成本周上课排行榜"""
        ranking_data, start_of_week, end_of_week, error_msg = (
            await self._build_weekly_ranking_data(event)
        )
        if error_msg:
            yield event.plain_result(error_msg)
            return

        image_path = await self.image_generator.generate_ranking_image(
            ranking_data,
            start_of_week,
            end_of_week,
            title="本周上课排行榜",
            subtitle=self.schedule_helper.get_date_range_text(
                start_of_week, end_of_week
            ),
        )
        yield event.image_result(image_path)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")
