"""课表命令的日期、时间和成员参数解析。"""

import re
from datetime import datetime, timedelta


def command_arguments(event, command_name):
    """从纯文本组件提取参数，避免把 @成员昵称当作日期。"""
    text_parts = [
        message.text
        for message in event.get_messages()
        if getattr(message, "type", None) == "Plain"
    ]
    text = "".join(text_parts) if text_parts else event.message_str
    _, separator, arguments = text.partition(command_name)
    return (arguments if separator else text).strip()


def mentioned_user(event):
    """获取被查询成员，忽略用于唤醒机器人的 @。"""
    if any(getattr(message, "type", None) == "AtAll" for message in event.get_messages()):
        raise ValueError("请一次只 @一位需要查询课表的群成员。")
    self_id = str(event.get_self_id())
    users = {
        str(message.qq)
        for message in event.get_messages()
        if getattr(message, "type", None) == "At"
        and str(message.qq) != self_id
    }
    if "all" in users or len(users) > 1:
        raise ValueError("请一次只 @一位需要查询课表的群成员。")
    return next(iter(users), None)


def parse_date(value, now):
    """日期支持 YYYY-MM-DD、今天、明天、昨天。"""
    relative_days = {"今天": 0, "明天": 1, "昨天": -1}
    if value in relative_days:
        return now.date() + timedelta(days=relative_days[value])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期格式应为 YYYY-MM-DD，例如 2026-10-20，也可以填写今天、明天或昨天。")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("日期无效，请检查年份、月份和日期。") from error


def parse_group_moment(arguments, now):
    """返回查询日期、状态计算的参考时刻和展示标签。"""
    parts = arguments.split()
    if not parts:
        return now.date(), now, "today"
    if len(parts) > 2:
        raise ValueError("请使用 /群友在上什么课 [YYYY-MM-DD] [HH:MM]。")
    if len(parts) == 1 and re.fullmatch(r"\d{2}:\d{2}", parts[0]):
        target_date, time_text = now.date(), parts[0]
    else:
        target_date = parse_date(parts[0], now)
        if len(parts) == 1:
            return target_date, now, target_date.isoformat()
        time_text = parts[1]
    if not re.fullmatch(r"\d{2}:\d{2}", time_text):
        raise ValueError("时间格式应为 HH:MM，例如 14:30。")
    try:
        target_time = datetime.strptime(time_text, "%H:%M").time()
    except ValueError as error:
        raise ValueError("时间无效，请使用 00:00 至 23:59。") from error
    moment = datetime.combine(target_date, target_time, tzinfo=now.tzinfo)
    return target_date, moment, f"{target_date.isoformat()} {time_text}"
