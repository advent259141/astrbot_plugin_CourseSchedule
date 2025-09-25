import os
import json
import aiohttp
import asyncio
import shutil
import time
import tempfile
from io import BytesIO
from typing import Dict, List
from datetime import datetime, timezone, timedelta
from icalendar import Calendar
from PIL import Image, ImageDraw, ImageFont
from astrbot.core.star import Star, Context, StarTools
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.core.utils.io import download_file
from pathlib import Path


class Main(Star):
    """课程表插件"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context

        # StarTools 是一个独立的工具集，应该直接通过类名调用
        self.data_path: Path = StarTools.get_data_dir()
        self.ics_path: Path = self.data_path / "ics"
        self.user_data_file: Path = self.data_path / "userdata.json"

        self._init_data()
        self.user_data = self._load_user_data()
        self.binding_requests: Dict[str, Dict] = {}

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
            "nickname": nickname
        }

        yield event.plain_result("请在60秒内，在本群内直接发送你的 .ics 文件。")

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
        ics_file_path = self.ics_path / f"{user_id}_{nickname}_{group_id}.ics"

        try:
            # 使用File组件的异步方法获取文件
            file_path = await file_component.get_file(allow_return_url=True)
            logger.info(f"File component returned path: {file_path}")

            # 检查返回的是字符串路径还是BytesIO对象
            if isinstance(file_path, str):
                if file_path.startswith("http"):
                    # 如果返回的是URL，下载文件
                    logger.info(f"Downloading file from URL: {file_path}")
                    await download_file(file_path, ics_file_path)
                else:
                    # 如果返回的是本地文件路径，直接复制
                    logger.info(f"Copying file from local path: {file_path}")
                    shutil.copy2(file_path, ics_file_path)
            elif hasattr(file_path, "read"):
                # 如果返回的是文件对象（如BytesIO），直接写入
                logger.info("Writing file from file object")
                with open(ics_file_path, "wb") as f:
                    f.write(file_path.read())
            else:
                raise ValueError(f"Unsupported file path type: {type(file_path)}")
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
        logger.info(event.message_obj.raw_message) # 平台下发的原始消息在这里
        logger.info(f"文件下载成功，文件路径: {ics_file_path}")
        logger.info(f"文件大小: {os.path.getsize(ics_file_path)} bytes")

        # 保存用户数据
        if group_id not in self.user_data:
            self.user_data[group_id] = {}
        self.user_data[group_id][user_id] = nickname

        self._save_user_data()

        # 删除绑定请求
        del self.binding_requests[request_key]
        yield event.plain_result(f"课表绑定成功！群号：{group_id}")

    def _parse_ics_file(self, file_path: str) -> List[Dict]:
        """解析 .ics 文件并返回课程列表"""
        courses = []
        with open(file_path, "r", encoding="utf-8") as f:
            cal = Calendar.from_ical(f.read())
            for component in cal.walk():
                if component.name == "VEVENT":
                    summary = component.get("summary")
                    description = component.get("description")
                    location = component.get("location")
                    dtstart = component.get("dtstart").dt
                    dtend = component.get("dtend").dt

                    # 确保 datetime 对象有时区信息，如果没有则添加 UTC 时区
                    if hasattr(dtstart, "tzinfo") and dtstart.tzinfo is not None:
                        # 如果有时区信息，转换为 UTC
                        dtstart = dtstart.astimezone(timezone(timedelta(hours=8)))
                    else:
                        # 如果没有时区信息，假设是 UTC
                        dtstart = dtstart.replace(tzinfo=timezone(timedelta(hours=8)))

                    if hasattr(dtend, "tzinfo") and dtend.tzinfo is not None:
                        # 如果有时区信息，转换为 UTC
                        dtend = dtend.astimezone(timezone(timedelta(hours=8)))
                    else:
                        # 如果没有时区信息，假设是 UTC
                        dtend = dtend.replace(tzinfo=timezone(timedelta(hours=8)))

                    courses.append({
                        "summary": summary,
                        "description": description,
                        "location": location,
                        "start_time": dtstart,
                        "end_time": dtend
                    })
        return courses

    @filter.command("查看课表")
    async def show_today_schedule(self, event: AstrMessageEvent):
        """查看今天还有什么课"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        if (not group_id or group_id not in self.user_data or
                user_id not in self.user_data[group_id]):
            yield event.plain_result(
                "你还没有在这个群绑定课表哦，请在群内发送 /绑定课表 指令，然后发送 .ics 文件来绑定。"
            )
            return

        nickname = self.user_data[group_id].get(user_id, user_id)
        ics_file_path = self.ics_path / f"{user_id}_{nickname}_{group_id}.ics"
        if not os.path.exists(ics_file_path):
            yield event.plain_result("课表文件不存在，可能已被删除。请重新绑定。")
            return

        courses = self._parse_ics_file(ics_file_path)
        today_courses = []
        # 使用上海时区 (UTC+8)
        shanghai_tz = timezone(timedelta(hours=8))
        now = datetime.now(shanghai_tz)

        for course in courses:
            if (course["start_time"].date() == now.date() and
                    course["start_time"] > now):
                today_courses.append(course)

        if not today_courses:
            yield event.plain_result("你今天没有课啦！")
            return

        # Sort courses by start time
        today_courses.sort(key=lambda x: x["start_time"])

        # Add user_id to each course for image generation
        for course in today_courses:
            course["nickname"] = self.user_data[group_id].get(user_id, user_id)

        image_path = await self._generate_user_schedule_image(today_courses, event.get_sender_name())
        yield event.image_result(image_path)

    @filter.command("群友上什么课")
    async def show_group_schedule(self, event: AstrMessageEvent):
        """查看群友接下来有什么课"""
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            yield event.plain_result("本群还没有人绑定课表哦。")
            return

        # 使用上海时区 (UTC+8)
        shanghai_tz = timezone(timedelta(hours=8))
        now = datetime.now(shanghai_tz)
        next_courses = []

        for user_id, nickname in self.user_data[group_id].items():
            ics_file_path = self.ics_path / f"{user_id}_{nickname}_{group_id}.ics"
            if not os.path.exists(ics_file_path):
                continue

            courses = self._parse_ics_file(ics_file_path)
            user_current_course = None
            user_next_course = None

            # 只筛选当天的课程进行判断
            today_courses = [c for c in courses if c.get("start_time") and c.get("start_time").date() == now.date()]

            for course in today_courses:
                start_time = course.get("start_time")
                end_time = course.get("end_time")

                if start_time and end_time:
                    # 检查是否是正在进行的课程
                    if start_time <= now < end_time:
                        user_current_course = course
                        break  # 找到正在上的课，就不需要再找下一节了

                    # 检查是否是未来的课程
                    elif start_time > now:
                        if user_next_course is None or start_time < user_next_course.get("start_time"):
                            user_next_course = course

            # 优先显示正在上的课
            display_course = user_current_course if user_current_course else user_next_course

            if display_course:
                # 创建课程对象的深拷贝，避免引用问题
                user_course_copy = {
                    "summary": display_course["summary"],
                    "description": display_course["description"],
                    "location": display_course["location"],
                    "start_time": display_course["start_time"],
                    "end_time": display_course["end_time"],
                    "user_id": user_id,
                    "nickname": nickname
                }
                next_courses.append(user_course_copy)

        if not next_courses:
            yield event.plain_result("群友们接下来都没有课啦！")
            return

        next_courses.sort(key=lambda x: x["start_time"])

        result_str = "接下来群友们的课程有：\n"
        for course in next_courses:
            result_str += f"\n用户: {course['user_id']}\n"
            result_str += f"课程名称: {course['summary']}\n"
            result_str += (f"时间: {course['start_time'].strftime('%H:%M')} - "
                          f"{course['end_time'].strftime('%H:%M')}\n")
            result_str += f"地点: {course['location']}\n"

        # Instead of sending plain text, we will generate and send an image.
        image_bytes = await self._generate_schedule_image(next_courses)
        yield event.image_result(image_bytes)

    async def _generate_schedule_image(self, courses: List[Dict]) -> str:
        """生成课程表图片并返回临时文件路径"""
        # --- 样式配置 ---
        BG_COLOR = "#FFFFFF"
        FONT_COLOR = "#333333"
        TITLE_COLOR = "#000000"
        SUBTITLE_COLOR = "#888888"
        STATUS_COLORS = {
            "进行中": ("#D32F2F", "#FFFFFF"),
            "下一节": ("#1976D2", "#FFFFFF"),
            "已结束": ("#388E3C", "#FFFFFF"),
            "无课程": ("#757575", "#FFFFFF"),
        }
        AVATAR_SIZE = 80
        ROW_HEIGHT = 120
        PADDING = 40

        # --- 动态字体加载 ---
        font_path = self._find_font_file()
        if font_path:
            try:
                font_main = ImageFont.truetype(font_path, 32)
                font_sub = ImageFont.truetype(font_path, 24)
                font_title = ImageFont.truetype(font_path, 48)
            except IOError:
                logger.warning(f"无法加载字体文件: {font_path}，将使用默认字体。")
                font_main = ImageFont.load_default()
                font_sub = ImageFont.load_default()
                font_title = ImageFont.load_default()
        else:
            logger.warning("未在插件目录中找到字体文件，将使用默认字体。")
            font_main = ImageFont.load_default()
            font_sub = ImageFont.load_default()
            font_title = ImageFont.load_default()

        # --- 图像尺寸计算 ---
        width = 800
        height = PADDING * 2 + 120 + len(courses) * ROW_HEIGHT
        image = Image.new("RGB", (width, height), BG_COLOR)
        draw = ImageDraw.Draw(image)

        # --- 绘制标题 ---
        draw.rectangle([PADDING, PADDING, PADDING + 20, PADDING + 60], fill="#26A69A")
        draw.text((PADDING + 40, PADDING), "“群友在上什么课?”", font=font_title, fill=TITLE_COLOR)
        draw.rectangle([PADDING + 40, PADDING + 70, PADDING + 40 + 300, PADDING + 75], fill="#A7FFEB")

        # --- 获取头像 ---
        async def fetch_avatar(session, user_id):
            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640&img_type=jpg"
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        return await response.read()
            except Exception as e:
                logger.error(f"Failed to download avatar for {user_id}: {e}")
            return None

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_avatar(session, course.get("user_id", "N/A")) for course in courses]
            avatar_datas = await asyncio.gather(*tasks)

        # --- 绘制每一行 ---
        y_offset = PADDING + 120
        now = datetime.now(timezone(timedelta(hours=8)))

        for i, course in enumerate(courses):
            user_id = course.get("user_id", "N/A")
            nickname = course.get("nickname", user_id)
            summary = course.get("summary", "无课程信息")
            start_time = course.get("start_time")
            end_time = course.get("end_time")

            # --- 绘制头像 ---
            avatar_data = avatar_datas[i]
            if avatar_data:
                avatar = Image.open(BytesIO(avatar_data)).convert("RGBA")
                avatar = avatar.resize((AVATAR_SIZE, AVATAR_SIZE))

                # 创建圆形遮罩
                mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)

                image.paste(avatar, (PADDING, y_offset + (ROW_HEIGHT - AVATAR_SIZE) // 2), mask)

            # --- 绘制箭头 ---
            arrow_x = PADDING + AVATAR_SIZE + 20
            arrow_y = y_offset + ROW_HEIGHT // 2
            arrow_points = [
                (arrow_x, arrow_y - 20),
                (arrow_x + 30, arrow_y),
                (arrow_x, arrow_y + 20)
            ]
            draw.polygon(arrow_points, fill="#BDBDBD")

            # --- 状态判断和绘制 ---
            status_text = ""
            detail_text = ""

            if start_time and end_time:
                if start_time <= now < end_time:
                    status_text = "进行中"
                    remaining_minutes = (end_time - now).seconds // 60
                    if remaining_minutes > 60:
                        detail_text = f"剩余 {remaining_minutes // 60} 小时 {remaining_minutes % 60} 分钟"
                    else:
                        detail_text = f"剩余 {remaining_minutes} 分钟"
                elif now < start_time:
                    status_text = "下一节"
                    delta_minutes = (start_time - now).seconds // 60
                    if delta_minutes > 60:
                        detail_text = f"{delta_minutes // 60} 小时 {delta_minutes % 60} 分钟后"
                    else:
                        detail_text = f"{delta_minutes} 分钟后"
                else:
                    status_text = "已结束"
                    detail_text = "今日课程已上完"
            else:
                status_text = "无课程"
                detail_text = "今天地，觉宇宙之无穷"

            # --- 绘制文本 ---
            text_x = arrow_x + 50
            draw.text((text_x, y_offset + 15), str(nickname), font=font_main, fill=FONT_COLOR)

            status_bg, status_fg = STATUS_COLORS.get(status_text, ("#000000", "#FFFFFF"))
            draw.rectangle([text_x, y_offset + 60, text_x + 100, y_offset + 95], fill=status_bg)
            draw.text((text_x + 10, y_offset + 65), status_text, font=font_sub, fill=status_fg)

            draw.text((text_x + 120, y_offset + 65), summary, font=font_sub, fill=FONT_COLOR)
            if start_time and end_time:
                time_str = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
                draw.text((text_x + 120, y_offset + 95), f"{time_str} ({detail_text})", font=font_sub, fill=SUBTITLE_COLOR)
            else:
                 draw.text((text_x + 120, y_offset + 95), detail_text, font=font_sub, fill=SUBTITLE_COLOR)


            y_offset += ROW_HEIGHT

        # --- 保存到临时文件 ---
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp_path = temp_file.name
        image.save(temp_path, format="PNG")
        temp_file.close()

        return temp_path

    async def _generate_user_schedule_image(self, courses: List[Dict], nickname: str) -> str:
        """为单个用户生成今日课程表图片"""
        # --- 样式配置 ---
        BG_COLOR = "#FFFFFF"
        FONT_COLOR = "#333333"
        TITLE_COLOR = "#000000"
        SUBTITLE_COLOR = "#888888"
        COURSE_BG_COLOR = "#E3F2FD"
        ROW_HEIGHT = 100
        PADDING = 40

        # --- 动态字体加载 ---
        font_path = self._find_font_file()
        if font_path:
            try:
                font_main = ImageFont.truetype(font_path, 28)
                font_sub = ImageFont.truetype(font_path, 22)
                font_title = ImageFont.truetype(font_path, 40)
            except IOError:
                logger.warning(f"无法加载字体文件: {font_path}，将使用默认字体。")
                font_main = ImageFont.load_default()
                font_sub = ImageFont.load_default()
                font_title = ImageFont.load_default()
        else:
            logger.warning("未在插件目录中找到字体文件，将使用默认字体。")
            font_main = ImageFont.load_default()
            font_sub = ImageFont.load_default()
            font_title = ImageFont.load_default()

        # --- 图像尺寸计算 ---
        width = 800
        height = PADDING * 2 + 100 + len(courses) * ROW_HEIGHT
        image = Image.new("RGB", (width, height), BG_COLOR)
        draw = ImageDraw.Draw(image)

        # --- 绘制标题 ---
        draw.text((PADDING, PADDING), f"{nickname}的今日课程", font=font_title, fill=TITLE_COLOR)

        # --- 绘制课程 ---
        y_offset = PADDING + 100

        for course in courses:
            summary = course.get("summary", "无课程信息")
            start_time = course.get("start_time")
            end_time = course.get("end_time")
            location = course.get("location", "未知地点")

            # 绘制圆角矩形背景
            self._draw_rounded_rectangle(draw, [PADDING, y_offset, width - PADDING, y_offset + ROW_HEIGHT - 10], 10, fill=COURSE_BG_COLOR)

            # 绘制时间
            time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
            draw.text((PADDING + 20, y_offset + 15), time_str, font=font_main, fill=TITLE_COLOR)

            # 绘制课程名称和地点
            draw.text((PADDING + 20, y_offset + 55), f"{summary} @ {location}", font=font_sub, fill=FONT_COLOR)

            y_offset += ROW_HEIGHT

        # --- 绘制页脚 ---
        footer_text = f"生成时间: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}"
        draw.text((PADDING, height - PADDING), footer_text, font=font_sub, fill=SUBTITLE_COLOR)

        # --- 保存到临时文件 ---
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp_path = temp_file.name
        image.save(temp_path, format="PNG")
        temp_file.close()

        return temp_path

    def _draw_rounded_rectangle(self, draw, xy, radius, fill):
        """手动绘制圆角矩形"""
        x1, y1, x2, y2 = xy
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
        draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
        draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
        draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)

    def _find_font_file(self) -> str:
        """在插件目录中查找第一个 .ttf 或 .otf 字体文件"""
        plugin_dir = os.path.dirname(__file__)
        for filename in os.listdir(plugin_dir):
            if filename.lower().endswith((".ttf", ".otf")):
                return os.path.join(plugin_dir, filename)
        return ""

    def _load_user_data(self) -> Dict:
        """加载用户数据"""
        try:
            with open(self.user_data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_user_data(self):
        """保存用户数据"""
        with open(self.user_data_file, "w", encoding="utf-8") as f:
            json.dump(self.user_data, f, ensure_ascii=False, indent=4)

    def _init_data(self):
        """初始化插件数据文件和目录"""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.ics_path.mkdir(exist_ok=True)
        if not self.user_data_file.exists():
            with open(self.user_data_file, "w", encoding="utf-8") as f:
                json.dump({}, f)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")
