"""
主页文字交互接口

当前优先服务：
- 识别模式开关提醒
- 运行状态提醒
- 简单个人信息问候
- 天气与节假日提醒
- 任务组服务周期提醒
"""

from datetime import datetime, timedelta


def build_home_copy_context(config=None, current_mode="抓取模式", mode_notice="",
                            running=False, recognition_status=None, now=None):
    """构造主页文案上下文。"""
    config = config or {}
    profile = config.get("profile", {}) or {}
    weather = config.get("weather_snapshot", {}) or {}
    calendar = config.get("calendar_snapshot", {}) or {}
    tasks = config.get("tasks", []) or []

    return {
        "profile": profile,
        "weather": weather,
        "calendar": calendar,
        "tasks": tasks,
        "current_mode": current_mode or "抓取模式",
        "mode_notice": (mode_notice or "").strip(),
        "running": bool(running),
        "recognition_status": recognition_status or {},
        "now": now or datetime.now(),
    }


def get_home_messages(context: dict) -> list[str]:
    """根据上下文生成主页轮换文案。"""
    context = context or {}
    now = context.get("now") or datetime.now()
    profile = context.get("profile") or {}
    weather = context.get("weather") or {}
    calendar = context.get("calendar") or {}
    tasks = context.get("tasks") or []
    mode = context.get("current_mode") or "抓取模式"
    mode_notice = (context.get("mode_notice") or "").strip()
    running = bool(context.get("running"))
    recognition = context.get("recognition_status") or {}
    messages = []

    greeting = _build_greeting(now, profile, weather)
    if greeting:
        messages.append(greeting)

    weather_message = _build_weather_reminder(weather)
    if weather_message:
        messages.append(weather_message)

    calendar_message = _build_calendar_reminder(calendar)
    if calendar_message:
        messages.append(calendar_message)

    service_message = _build_service_cycle_reminder(tasks, now)
    if service_message:
        messages.append(service_message)

    if running:
        messages.append(f"🛰️ 当前处于{mode}，主页提醒会跟随模式状态自动变化。")
    else:
        messages.append(f"💤 当前默认停留在{mode}，启动监控后会自动刷新互动提醒。")

    if mode_notice:
        messages.append(mode_notice)

    recognition_message = _build_recognition_message(recognition, mode)
    if recognition_message:
        messages.append(recognition_message)

    if mode == "抓取模式":
        messages.append("🧭 抓取模式适合固定脚本跑浏览器、抓页面内容和规则判断。")
    elif mode == "识别模式":
        messages.append("🖼️ 识别模式会低负载监听新截图，按批次确认后再排队发送。主界面右上角可直接点“仅启动识别”。")

    seen = set()
    deduped = []
    for message in messages:
        text = (message or "").strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped or ["欢迎回来，今天的监控动态会显示在这里。"]


def pick_home_message(context: dict, cursor: int = 0) -> str:
    """按游标轮换返回单条主页文案。"""
    messages = get_home_messages(context)
    if not messages:
        return ""
    return messages[cursor % len(messages)]


def _build_greeting(now: datetime, profile: dict, weather: dict) -> str:
    name = str(profile.get("display_name") or profile.get("name") or "").strip()
    city = str(profile.get("city") or weather.get("city") or "").strip()
    weather_text = str(weather.get("summary") or "").strip()
    weather_emoji = _get_weather_emoji(weather)

    hour = now.hour
    if hour < 6:
        prefix = "🌙 夜深了"
    elif hour < 11:
        prefix = "🌅 早上好"
    elif hour < 14:
        prefix = "☀️ 中午好"
    elif hour < 18:
        prefix = "🌇 下午好"
    else:
        prefix = "🌙 晚上好"

    parts = [prefix]
    if name:
        parts.append(name)

    message = "，".join(parts)
    extras = []
    if city:
        extras.append(city)
    if weather_text:
        extras.append(f"{weather_emoji} {weather_text}".strip())
    if extras:
        message = f"{message}，{ '，'.join(extras) }。"
    else:
        message = f"{message}。"
    return message


def _build_weather_reminder(weather: dict) -> str:
    if not weather:
        return ""

    summary = str(weather.get("summary") or weather.get("condition") or "").strip()
    temp_c = _to_float(weather.get("temp_c", weather.get("temperature")))
    low_c = _to_float(weather.get("low_c", weather.get("temp_min")))
    high_c = _to_float(weather.get("high_c", weather.get("temp_max")))

    has_rain = _contains_any(
        summary,
        ["雨", "雷", "阵雨", "小雨", "中雨", "大雨", "暴雨", "rain", "storm", "shower", "thunder"],
    )
    large_temp_gap = high_c is not None and low_c is not None and (high_c - low_c) >= 8

    outfit_tip = ""
    followup_tip = ""
    if low_c is not None and low_c <= 10:
        if large_temp_gap:
            outfit_tip = "早晚偏冷、昼夜温差也明显，带件外套会更稳妥"
        else:
            outfit_tip = "早晚温度偏低，穿厚一点或带件外套会更舒服些"
    elif temp_c is not None and temp_c <= 15:
        if large_temp_gap:
            outfit_tip = "体感偏凉、昼夜温差也大，外出记得加件衣服"
        else:
            outfit_tip = "体感偏凉，外出记得加件衣服"
    elif low_c is not None and low_c <= 16:
        if large_temp_gap:
            outfit_tip = "早晚稍凉、昼夜温差也大，带件薄外套会更妥帖"
        else:
            outfit_tip = "早晚稍凉，带件薄外套会更妥帖"
    elif large_temp_gap:
        followup_tip = "昼夜温差偏大，记得灵活增减衣物"
    elif high_c is not None and high_c >= 30:
        followup_tip = "白天气温偏高，记得补水防晒"

    if has_rain:
        reminders = ["出门记得带把伞"]
        if outfit_tip:
            reminders.append(_rewrite_weather_tip_for_compound_sentence(outfit_tip))
        elif followup_tip:
            reminders.append(_rewrite_weather_tip_for_compound_sentence(followup_tip))
        return f"🌦️ 今天{'，'.join(reminders)}。"

    if outfit_tip:
        return f"🧥 {outfit_tip}。"
    if followup_tip:
        icon = "🌡️" if large_temp_gap else "🥤"
        return f"{icon} {followup_tip}。"
    return ""


def _rewrite_weather_tip_for_compound_sentence(text: str) -> str:
    cleaned = str(text or "").strip().rstrip("。")
    if not cleaned:
        return ""

    replacements = {
        "外出记得加件衣服": "加件衣服会更舒服",
        "记得灵活增减衣物": "灵活增减衣物更稳妥",
        "记得补水防晒": "做好补水防晒",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)

    if cleaned.startswith("记得"):
        cleaned = cleaned[2:]
    cleaned = cleaned.replace("，记得", "，")
    return cleaned


def _build_service_cycle_reminder(tasks: list[dict], now: datetime) -> str:
    if not tasks:
        return ""

    today = now.date()
    urgent = []
    overdue = []

    for task in tasks:
        if not task.get("enabled", True):
            continue

        start_date = _parse_date(task.get("service_start_date"))
        cycle_days = _to_int(task.get("service_cycle_days"))
        if not start_date or not cycle_days or cycle_days <= 0:
            continue

        end_date = start_date + timedelta(days=cycle_days - 1)
        days_left = (end_date - today).days
        item = {
            "name": str(task.get("name") or "未命名任务组").strip(),
            "end_date": end_date,
            "days_left": days_left,
        }
        if days_left < 0:
            overdue.append(item)
        elif days_left <= 7:
            urgent.append(item)

    overdue.sort(key=lambda item: item["days_left"])
    urgent.sort(key=lambda item: item["days_left"])

    if overdue:
        first = overdue[0]
        extra = len(overdue) - 1
        suffix = f"，另外还有 {extra} 个任务组也已到期" if extra > 0 else ""
        return (
            f"任务组「{first['name']}」服务周期已于 {first['end_date'].isoformat()} 到期，"
            f"记得尽快续期{suffix}。"
        )

    if urgent:
        first = urgent[0]
        extra = len(urgent) - 1
        if first["days_left"] == 0:
            base = f"任务组「{first['name']}」服务周期今天截止，记得确认续期安排。"
        else:
            base = f"任务组「{first['name']}」服务周期还有 {first['days_left']} 天结束，记得提前安排续期。"
        if extra > 0:
            base = f"{base[:-1]}，另外还有 {extra} 个任务组也进入提醒窗口。"
        return base

    return ""


def _build_calendar_reminder(calendar: dict) -> str:
    if not calendar:
        return ""

    holiday_name = str(
        calendar.get("holiday_name")
        or calendar.get("name")
        or calendar.get("festival")
        or ""
    ).strip()
    if not holiday_name:
        return ""

    days_until = _to_int(calendar.get("days_until"))
    is_holiday = bool(calendar.get("is_holiday", False))
    is_makeup_workday = bool(calendar.get("is_makeup_workday", False))
    holiday_type = str(calendar.get("holiday_type") or "").strip().lower()

    if is_makeup_workday:
        return f"📅 今天是{holiday_name}调休上班日，出行和工作节奏记得提前安排。"

    if _is_memorial_holiday(holiday_name, holiday_type):
        if is_holiday:
            return f"🕯️ 今天是{holiday_name}假期。"
        if days_until is not None and 0 <= days_until <= 3:
            return f"📅 距离{holiday_name}还有 {days_until} 天，假期将至，行程可以提前安排。"
        return ""

    emoji = _get_holiday_emoji(holiday_name)
    if is_holiday:
        return f"{emoji} 今天开始放{holiday_name}假期，提前祝你假期愉快。"
    if days_until is not None and days_until == 0:
        return f"{emoji} 今天就是{holiday_name}，祝你节日愉快。"
    if days_until is not None and 0 < days_until <= 3:
        return f"{emoji} 距离{holiday_name}还有 {days_until} 天，提前祝你假期愉快。"
    return ""


def _build_recognition_message(recognition: dict, mode: str) -> str:
    if not recognition:
        return ""

    pending_batches = int(recognition.get("pending_batches") or 0)
    buffered_images = int(recognition.get("buffered_images") or 0)
    queued_images = int(recognition.get("queued_images") or 0)
    idle_minutes = recognition.get("idle_minutes")

    if pending_batches > 0:
        return f"📨 识别模式当前有 {pending_batches} 个待确认批次，建议优先处理发送开关。"
    if buffered_images > 0 or queued_images > 0:
        return f"🧾 识别模式还有 {buffered_images + queued_images} 张截图在处理中，请留意是否需要切换。"
    if mode == "识别模式" and idle_minutes is not None and idle_minutes >= 30:
        return f"🔁 识别模式已经 {int(idle_minutes)} 分钟没有新截图，适合手动切回抓取模式。"
    return ""


def _get_weather_emoji(weather: dict) -> str:
    summary = str(weather.get("summary") or weather.get("condition") or "").lower()
    if _contains_any(summary, ["雷", "暴雨", "storm", "thunder"]):
        return "⛈️"
    if _contains_any(summary, ["雨", "rain", "shower"]):
        return "🌧️"
    if _contains_any(summary, ["雪", "snow"]):
        return "❄️"
    if _contains_any(summary, ["阴", "cloud", "overcast"]):
        return "☁️"
    if _contains_any(summary, ["多云", "cloudy"]):
        return "⛅"
    if _contains_any(summary, ["晴", "sunny", "clear"]):
        return "☀️"
    return "🌤️"


def _get_holiday_emoji(holiday_name: str) -> str:
    text = holiday_name or ""
    if "春节" in text:
        return "🧨"
    if "元旦" in text:
        return "🎉"
    if "劳动节" in text or "五一" in text:
        return "🎈"
    if "端午" in text:
        return "🐉"
    if "中秋" in text:
        return "🌕"
    if "国庆" in text:
        return "🇨🇳"
    return "🎊"


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _is_memorial_holiday(holiday_name: str, holiday_type: str = "") -> bool:
    text = f"{holiday_name} {holiday_type}".lower()
    memorial_keywords = [
        "清明", "memorial", "mourning", "remembrance", "哀悼",
        "祭", "公祭", "纪念",
    ]
    return any(keyword.lower() in text for keyword in memorial_keywords)


def _contains_any(text: str, keywords: list[str]) -> bool:
    text = (text or "").lower()
    return any(keyword.lower() in text for keyword in keywords)


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None
