# -*- coding: utf-8 -*-
"""
本模块负责生成插件所需的各种图片，如图形化课程表和排行榜。
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone, timedelta, date
from io import BytesIO
from typing import Dict, List, Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from . import constants as c

AVATAR_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_read=5)

class ImageGenerator:
    """图片生成器"""

    @staticmethod
    def process_avatar_data(
        avatar_data: bytes,
        avatar_size: int,
        allowed_formats: List[str] | None = None,
    ) -> Optional[Image.Image]:
        """
        处理头像数据

        Args:
            avatar_data: 头像数据字节
            avatar_size: 头像尺寸
            allowed_formats: 允许的图片格式列表

        Returns:
            处理后的头像图片对象，如果处理失败则返回None
        """
        if not avatar_data:
            return None

        if allowed_formats is None:
            allowed_formats = ["JPEG", "PNG", "GIF", "WEBP", "BMP"]

        try:
            image_stream = BytesIO(avatar_data)
            buffer_size = image_stream.getbuffer().nbytes
            if buffer_size > 0:
                avatar = Image.open(image_stream)
                avatar_format = avatar.format
                if avatar_format in allowed_formats:
                    avatar = avatar.convert("RGBA")
                    avatar = avatar.resize(
                        (avatar_size, avatar_size), resample=Image.LANCZOS
                    )
                    return avatar
        except Exception:
            # 如果处理失败，返回None
            pass
        return None

    def __init__(self):
        self.font_path = self._find_font_file()
        self.font_main = self._load_font(32)
        self.font_sub = self._load_font(24)
        self.font_title = self._load_font(48)
        self.font_header = self._load_font(26)
        self.font_text = self._load_font(28)
        self.font_rank = self._load_font(36)
        self.font_subtitle = self._load_font(24)  # 添加缺失的 font_subtitle 属性
        self.user_font_main = self._load_font(28)
        self.user_font_sub = self._load_font(22)
        self.user_font_title = self._load_font(40)

    def _find_font_file(self) -> str:
        """在插件目录中查找第一个 .ttf 或 .otf 字体文件"""
        plugin_dir = os.path.dirname(__file__)
        for filename in os.listdir(plugin_dir):
            if filename.lower().endswith((".ttf", ".otf")):
                return os.path.join(plugin_dir, filename)
        return ""

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """加载指定大小的字体"""
        try:
            return (
                ImageFont.truetype(self.font_path, size, encoding="utf-8")
                if self.font_path
                else ImageFont.load_default()
            )
        except IOError:
            logger.warning(f"无法加载字体文件: {self.font_path}，将使用默认字体。")
            return ImageFont.load_default()

    def _sanitize_for_pil(
        self, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    ) -> str:
        """移除字体不支持的字符"""
        sanitized_text = ""
        for char in text:
            try:
                font.getbbox(char)
                sanitized_text += char
            except (TypeError, ValueError):
                sanitized_text += " "
        return sanitized_text

    def _draw_rounded_rectangle(self, draw, xy, radius, fill):
        """手动绘制圆角矩形"""
        x1, y1, x2, y2 = xy
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
        draw.pieslice(
            [x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill
        )
        draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
        draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)

    def _calculate_time_delta(
        self, start_time: datetime, end_time: datetime, now: datetime, date_type: str
    ) -> tuple[str, str]:
        """
        计算课程时间状态和详细信息
        
        Args:
            start_time: 课程开始时间
            end_time: 课程结束时间
            now: 当前时间
            date_type: 日期类型 ("today", "tomorrow", 其他)
            
        Returns:
            tuple: (状态文本, 详细信息文本)
        """
        if not start_time or not end_time:
            return self._get_finished_status(date_type)
        
        # 计算完整的时间差，包括天数
        total_seconds_start = int((start_time - now).total_seconds())
        total_seconds_end = int((end_time - now).total_seconds())
        
        if total_seconds_start < 0 <= total_seconds_end:
            # 课程进行中
            status_text = "进行中"
            remaining_minutes = total_seconds_end // 60
            detail_text = self._format_duration(remaining_minutes, "剩余", "")
        elif total_seconds_start > 0:
            # 课程未开始
            status_text = "下一节"
            delta_minutes = total_seconds_start // 60
            detail_text = self._format_duration(delta_minutes, "", "后")
        else:
            # 课程已结束
            return self._get_finished_status(date_type)
        
        return status_text, detail_text
    
    def _format_duration(self, total_minutes: int, prefix: str = "", suffix: str = "") -> str:
        """
        格式化时间持续时间
        
        Args:
            total_minutes: 总分钟数
            prefix: 前缀文本
            suffix: 后缀文本
            
        Returns:
            格式化后的时间文本
        """
        if total_minutes > 60:
            hours = total_minutes // 60
            minutes = total_minutes % 60
            return f"{prefix}{hours} 小时 {minutes} 分钟{suffix}"
        else:
            return f"{prefix}{total_minutes} 分钟{suffix}"
    
    def _get_finished_status(self, date_type: str) -> tuple[str, str]:
        """
        获取已结束状态文本
        
        Args:
            date_type: 日期类型
            
        Returns:
            tuple: (状态文本, 详细信息文本)
        """
        status_text = "已结束"
        if date_type == "today":
            detail_text = "今日所有课程已结束"
        elif date_type == "tomorrow":
            detail_text = "明天所有课程已结束"
        else:
            detail_text = f"{date_type}所有课程已结束"
        return status_text, detail_text

    async def _fetch_avatars(self, user_ids: List[str]) -> List[Optional[bytes]]:
        """异步获取多个用户的头像"""

        if not user_ids:
            return []

        async def fetch_avatar(session: aiohttp.ClientSession, user_id: str):
            avatar_url = (
                f"http://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640&img_type=jpg"
            )
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        avatar_data = await response.read()
                        if avatar_data:
                            return avatar_data
                        logger.warning(
                            f"Failed to download avatar for {user_id}: Empty response from server"
                        )
                        return None
                    logger.warning(
                        f"Failed to download avatar for {user_id}: HTTP {response.status}"
                    )
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Failed to fetch avatar for {user_id}: {type(e).__name__} - {e}"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"Unexpected error when fetching avatar for {user_id}: {type(e).__name__} - {e}"
                )
                return None

        async with aiohttp.ClientSession(timeout=AVATAR_REQUEST_TIMEOUT) as session:
            tasks = [fetch_avatar(session, user_id) for user_id in user_ids]
            return await asyncio.gather(*tasks)

    def _save_temp_image(self, image: Image.Image) -> str:
        """保存临时图片并返回路径"""
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp_path = temp_file.name
        image.save(temp_path, format="PNG")
        temp_file.close()
        return temp_path

    def _generate_schedule_image_sync(
        self,
        courses: List[Dict],
        avatar_datas: List[Optional[bytes]],
        date_type: str,
    ) -> str:
        """在工作线程中生成群课程表图片"""
        height = c.GS_PADDING * 2 + 120 + len(courses) * c.GS_ROW_HEIGHT
        image = Image.new("RGB", (c.GS_WIDTH, height), c.GS_BG_COLOR)
        draw = ImageDraw.Draw(image)

        draw.rectangle(
            [c.GS_PADDING, c.GS_PADDING, c.GS_PADDING + 20, c.GS_PADDING + 60],
            fill="#26A69A",
        )

        if date_type == "today":
            title = "“群友在上什么课?”"
        elif date_type == "tomorrow":
            title = "“群友明天上什么课?”"
        else:
            title = f"“群友{date_type}上什么课?”"

        draw.text(
            (c.GS_PADDING + 40, c.GS_PADDING),
            title,
            font=self.font_title,
            fill=c.GS_TITLE_COLOR,
        )
        draw.rectangle(
            [
                c.GS_PADDING + 40,
                c.GS_PADDING + 70,
                c.GS_PADDING + 40 + 300,
                c.GS_PADDING + 75,
            ],
            fill="#A7FFEB",
        )

        y_offset = c.GS_PADDING + 120
        now = datetime.now(timezone(timedelta(hours=8)))

        for i, course in enumerate(courses):
            user_id = course.get("user_id", "N/A")
            nickname = course.get("nickname", user_id)
            summary = course.get("summary", "无课程信息")
            start_time: datetime = course.get("start_time")
            end_time: datetime = course.get("end_time")

            avatar_data = avatar_datas[i] if i < len(avatar_datas) else None
            if avatar_data:
                avatar = self.process_avatar_data(avatar_data, c.GS_AVATAR_SIZE)
                if avatar:
                    mask = Image.new("L", (c.GS_AVATAR_SIZE, c.GS_AVATAR_SIZE), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse(
                        (0, 0, c.GS_AVATAR_SIZE, c.GS_AVATAR_SIZE), fill=255
                    )
                    image.paste(
                        avatar,
                        (
                            c.GS_PADDING,
                            y_offset + (c.GS_ROW_HEIGHT - c.GS_AVATAR_SIZE) // 2,
                        ),
                        mask,
                    )
                else:
                    logger.debug(
                        f"Skipping avatar for user {user_id}: failed to process avatar data"
                    )
            else:
                logger.debug(
                    f"No avatar data available for user {user_id}, skipping avatar display"
                )

            arrow_x = c.GS_PADDING + c.GS_AVATAR_SIZE + 20
            arrow_y = y_offset + c.GS_ROW_HEIGHT // 2
            arrow_points = [
                (arrow_x, arrow_y - 20),
                (arrow_x + 30, arrow_y),
                (arrow_x, arrow_y + 20),
            ]
            draw.polygon(arrow_points, fill="#BDBDBD")

            status_text, detail_text = self._calculate_time_delta(
                start_time, end_time, now, date_type
            )

            text_x = arrow_x + 50
            nickname = self._sanitize_for_pil(nickname, self.font_main)
            draw.text(
                (text_x, y_offset + 15),
                str(nickname),
                font=self.font_main,
                fill=c.GS_FONT_COLOR,
            )

            status_bg, status_fg = c.GS_STATUS_COLORS.get(
                status_text, ("#000000", "#FFFFFF")
            )
            draw.rectangle(
                [text_x, y_offset + 60, text_x + 100, y_offset + 95], fill=status_bg
            )
            draw.text(
                (text_x + 10, y_offset + 65),
                status_text,
                font=self.font_sub,
                fill=status_fg,
            )

            draw.text(
                (text_x + 120, y_offset + 65),
                summary,
                font=self.font_sub,
                fill=c.GS_FONT_COLOR,
            )
            if start_time and end_time:
                time_str = (
                    f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
                )
                draw.text(
                    (text_x + 120, y_offset + 95),
                    f"{time_str} ({detail_text})",
                    font=self.font_sub,
                    fill=c.GS_SUBTITLE_COLOR,
                )
            else:
                draw.text(
                    (text_x + 120, y_offset + 95),
                    detail_text,
                    font=self.font_sub,
                    fill=c.GS_SUBTITLE_COLOR,
                )

            y_offset += c.GS_ROW_HEIGHT

        return self._save_temp_image(image)

    async def generate_schedule_image(
        self, courses: List[Dict], date_type: str = "today"
    ) -> str:
        """生成课程表图片并返回临时文件路径

        Args:
            courses: 课程列表
            date_type: 日期类型，"today", "tomorrow", 或自定义日期类型如"本周三"等
        """
        user_ids = [course.get("user_id", "N/A") for course in courses]
        avatar_datas = await self._fetch_avatars(user_ids)
        return await asyncio.to_thread(
            self._generate_schedule_image_sync, courses, avatar_datas, date_type
        )

    def _generate_user_schedule_image_sync(
        self, courses: List[Dict], nickname: str, title_suffix: str
    ) -> str:
        """在工作线程中生成个人课程表图片"""
        height = c.US_PADDING * 2 + 100 + len(courses) * c.US_ROW_HEIGHT
        image = Image.new("RGB", (c.US_WIDTH, height), c.US_BG_COLOR)
        draw = ImageDraw.Draw(image)

        sanitized_nickname = self._sanitize_for_pil(nickname, self.user_font_title)
        draw.text(
            (c.US_PADDING, c.US_PADDING),
            f"{sanitized_nickname}{title_suffix}",
            font=self.user_font_title,
            fill=c.US_TITLE_COLOR,
        )

        y_offset = c.US_PADDING + 100

        for course in courses:
            summary = course.get("summary", "无课程信息")
            start_time = course.get("start_time")
            end_time = course.get("end_time")
            location = course.get("location", "未知地点")

            self._draw_rounded_rectangle(
                draw,
                [
                    c.US_PADDING,
                    y_offset,
                    c.US_WIDTH - c.US_PADDING,
                    y_offset + c.US_ROW_HEIGHT - 10,
                ],
                10,
                fill=c.US_COURSE_BG_COLOR,
            )

            time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
            draw.text(
                (c.US_PADDING + 20, y_offset + 15),
                time_str,
                font=self.user_font_main,
                fill=c.US_TITLE_COLOR,
            )

            draw.text(
                (c.US_PADDING + 20, y_offset + 55),
                f"{summary} @ {location}",
                font=self.user_font_sub,
                fill=c.US_FONT_COLOR,
            )

            y_offset += c.US_ROW_HEIGHT

        footer_text = f"生成时间: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}"
        draw.text(
            (c.US_PADDING, height - c.US_PADDING),
            footer_text,
            font=self.user_font_sub,
            fill=c.US_SUBTITLE_COLOR,
        )

        return self._save_temp_image(image)

    async def generate_user_schedule_image(
        self, courses: List[Dict], nickname: str, title_suffix: str = "的今日课程"
    ) -> str:
        """为单个用户生成课程表图片"""
        return await asyncio.to_thread(
            self._generate_user_schedule_image_sync, courses, nickname, title_suffix
        )

    def _generate_ranking_image_sync(
        self,
        ranking_data: List[Dict],
        avatar_datas: List[Optional[bytes]],
        start_date: date,
        end_date: date,
        title: str,
        subtitle: str | None,
    ) -> str:
        """在工作线程中生成排行榜图片"""
        height = (
            c.RANKING_HEADER_HEIGHT
            + len(ranking_data) * c.RANKING_ROW_HEIGHT
            + c.RANKING_PADDING
        )
        image = Image.new("RGB", (c.RANKING_WIDTH, height), c.RANKING_BG_COLOR)
        draw = ImageDraw.Draw(image)

        ranking_title = self._sanitize_for_pil(title, self.font_title)
        draw.text(
            (c.RANKING_PADDING, c.RANKING_PADDING),
            ranking_title,
            font=self.font_title,
            fill=c.RANKING_TITLE_COLOR,
        )
        if subtitle is None:
            if start_date == end_date:
                subtitle = f"统计日期：{start_date.strftime('%Y/%m/%d')}"
            else:
                subtitle = (
                    f"{start_date.strftime('%Y/%m/%d')} - "
                    f"{end_date.strftime('%Y/%m/%d')}"
                )
        date_range_str = self._sanitize_for_pil(subtitle, self.font_subtitle)
        draw.text(
            (c.RANKING_PADDING, c.RANKING_PADDING + 70),
            date_range_str,
            font=self.font_subtitle,
            fill=c.RANKING_SUBTITLE_COLOR,
        )

        y_offset = c.RANKING_HEADER_HEIGHT
        for i, data in enumerate(ranking_data):
            rank = i + 1

            if i % 2 == 1:
                draw.rectangle(
                    [
                        c.RANKING_PADDING,
                        y_offset,
                        c.RANKING_WIDTH - c.RANKING_PADDING,
                        y_offset + c.RANKING_ROW_HEIGHT,
                    ],
                    fill=c.RANKING_ROW_BG_COLOR,
                )

            rank_color = c.RANKING_COLORS.get(rank, c.RANKING_FONT_COLOR)
            rank_text = str(rank)
            try:
                rank_bbox = self.font_rank.getbbox(rank_text)
                rank_width = rank_bbox[2] - rank_bbox[0]
                rank_height = rank_bbox[3] - rank_bbox[1]
            except (TypeError, ValueError):
                rank_width = 10
                rank_height = 10
            draw.text(
                (
                    c.RANKING_PADDING + 40 - rank_width / 2,
                    y_offset + (c.RANKING_ROW_HEIGHT - rank_height) / 2,
                ),
                rank_text,
                font=self.font_rank,
                fill=rank_color,
            )

            avatar_data = avatar_datas[i] if i < len(avatar_datas) else None
            if avatar_data:
                avatar = self.process_avatar_data(avatar_data, c.RANKING_AVATAR_SIZE)
                if avatar:
                    mask = Image.new(
                        "L", (c.RANKING_AVATAR_SIZE, c.RANKING_AVATAR_SIZE), 0
                    )
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse(
                        (0, 0, c.RANKING_AVATAR_SIZE, c.RANKING_AVATAR_SIZE), fill=255
                    )
                    image.paste(
                        avatar,
                        (
                            c.RANKING_PADDING + 100,
                            y_offset
                            + (c.RANKING_ROW_HEIGHT - c.RANKING_AVATAR_SIZE) // 2,
                        ),
                        mask,
                    )
                else:
                    logger.debug(
                        f"Skipping ranking avatar: failed to process avatar data for user at index {i}"
                    )
            else:
                logger.debug(
                    f"No avatar data available for user at index {i}, skipping ranking avatar display"
                )

            nickname = self._sanitize_for_pil(data["nickname"], self.font_text)
            draw.text(
                (c.RANKING_PADDING + 210, y_offset + (c.RANKING_ROW_HEIGHT - 30) / 2),
                nickname,
                font=self.font_text,
                fill=c.RANKING_FONT_COLOR,
            )

            total_seconds = data["total_duration"].total_seconds()
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            duration_str = f"{hours}h {minutes}m"
            count_str = f"{data['course_count']} 节"

            try:
                duration_bbox = self.font_text.getbbox(duration_str)
                duration_width = duration_bbox[2] - duration_bbox[0]
            except (TypeError, ValueError):
                duration_width = 100
            draw.text(
                (
                    c.RANKING_WIDTH - c.RANKING_PADDING - duration_width - 20,
                    y_offset + (c.RANKING_ROW_HEIGHT - 30) / 2 - 15,
                ),
                duration_str,
                font=self.font_text,
                fill=c.RANKING_FONT_COLOR,
            )

            try:
                count_bbox = self.font_subtitle.getbbox(count_str)
                count_width = count_bbox[2] - count_bbox[0]
            except (TypeError, ValueError):
                count_width = 80
            draw.text(
                (
                    c.RANKING_WIDTH - c.RANKING_PADDING - count_width - 20,
                    y_offset + (c.RANKING_ROW_HEIGHT - 30) / 2 + 25,
                ),
                count_str,
                font=self.font_subtitle,
                fill=c.RANKING_SUBTITLE_COLOR,
            )

            y_offset += c.RANKING_ROW_HEIGHT

        return self._save_temp_image(image)

    async def generate_ranking_image(
        self,
        ranking_data: List[Dict],
        start_date: date,
        end_date: date,
        title: str = "本周上课排行榜",
        subtitle: str | None = None,
    ) -> str:
        """生成排行榜图片"""
        user_ids = [data["user_id"] for data in ranking_data]
        avatar_datas = await self._fetch_avatars(user_ids)
        return await asyncio.to_thread(
            self._generate_ranking_image_sync,
            ranking_data,
            avatar_datas,
            start_date,
            end_date,
            title,
            subtitle,
        )
