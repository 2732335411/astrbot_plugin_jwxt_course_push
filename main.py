import asyncio
import base64
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import ddddocr
import httpx
from bs4 import BeautifulSoup

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


@dataclass
class CourseItem:
    day: str
    period: str
    name: str
    teacher: str = ""
    location: str = ""
    weeks: str = ""
    day_index: int = 0
    slot_index: int = 0
    raw_text: str = ""


@register(
    "astrbot_plugin_jwxt_course_push",
    "tom",
    "强智教务课表获取与每日上课提醒推送",
    "1.0.0",
    "",
)
class JwxtCoursePushPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._ocr = ddddocr.DdddOcr(show_ad=False)
        self._stop_event = asyncio.Event()
        self._push_task: Optional[asyncio.Task] = None
        self._last_week_no: Optional[int] = None
        self._cache_date: Optional[date] = None
        self._cache_courses: List[CourseItem] = []
        self._cache_loaded_at: Optional[datetime] = None
        self._last_no_week_warn_on: Optional[date] = None
        self._log_lock = Lock()

    async def on_astrbot_loaded(self):
        self._log_info("[jwxt-course-push] 插件已加载")
        if self._cfg_bool("enable_daily_push", True):
            if self._push_task and not self._push_task.done():
                self._log_warning("[jwxt-course-push] 每日推送任务已在运行，跳过重复启动")
                return
            self._stop_event.clear()
            self._push_task = asyncio.create_task(self._push_loop())
            self._log_info("[jwxt-course-push] 每日推送任务已启动")
        else:
            if self._push_task and not self._push_task.done():
                self._stop_event.set()
                self._push_task.cancel()
                try:
                    await self._push_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self._log_exception("[jwxt-course-push] 关闭推送任务时出现异常", e)
            self._log_info("[jwxt-course-push] enable_daily_push=false，未启动推送任务")

    async def terminate(self):
        self._stop_event.set()
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._log_info("[jwxt-course-push] 插件已卸载")

    @filter.command("jwxt_today")
    async def jwxt_today(self, event: AstrMessageEvent):
        try:
            target_date, target_day = self._resolve_target_day("today")
            courses = await self._fetch_courses_for_day(target_day)
            yield event.plain_result(self._format_courses_message(target_date, target_day, courses))
        except Exception as e:
            self._log_exception("[jwxt-course-push] 查询今日课表失败", e)
            yield event.plain_result(f"查询今日课表失败: {e}")

    @filter.command("jwxt_tomorrow")
    async def jwxt_tomorrow(self, event: AstrMessageEvent):
        try:
            target_date, target_day = self._resolve_target_day("tomorrow")
            courses = await self._fetch_courses_for_day(target_day)
            yield event.plain_result(self._format_courses_message(target_date, target_day, courses))
        except Exception as e:
            self._log_exception("[jwxt-course-push] 查询明日课表失败", e)
            yield event.plain_result(f"查询明日课表失败: {e}")

    @filter.command("jwxt_push_on")
    async def jwxt_push_on(self, event: AstrMessageEvent):
        unified_msg_origin = event.unified_msg_origin
        if not unified_msg_origin:
            yield event.plain_result("无法识别当前会话，开启失败")
            return

        subs = await self._load_subscriptions()
        subs[unified_msg_origin] = {
            "enabled": True,
            "updated_at": datetime.now(self._tz()).isoformat(),
        }
        await self._save_subscriptions(subs)
        yield event.plain_result("已开启每日上课提醒（当前会话）")

    @filter.command("jwxt_push_off")
    async def jwxt_push_off(self, event: AstrMessageEvent):
        unified_msg_origin = event.unified_msg_origin
        if not unified_msg_origin:
            yield event.plain_result("无法识别当前会话，关闭失败")
            return

        subs = await self._load_subscriptions()
        if unified_msg_origin in subs:
            subs[unified_msg_origin]["enabled"] = False
            subs[unified_msg_origin]["updated_at"] = datetime.now(self._tz()).isoformat()
            await self._save_subscriptions(subs)
        yield event.plain_result("已关闭每日上课提醒（当前会话）")

    @filter.command("jwxt_push_status")
    async def jwxt_push_status(self, event: AstrMessageEvent):
        unified_msg_origin = event.unified_msg_origin
        subs = await self._load_subscriptions()
        enabled_count = sum(1 for v in subs.values() if v.get("enabled"))
        current_enabled = bool(subs.get(unified_msg_origin, {}).get("enabled")) if unified_msg_origin else False

        msg = (
            f"当前会话提醒状态: {'开启' if current_enabled else '关闭'}\n"
            f"全局已开启会话数: {enabled_count}\n"
            f"提醒策略: 上午/下午首课前 {self._cfg('session_remind_before_minutes', 60)} 分钟\n"
            f"上午节次上限: {self._cfg('morning_period_max', 5)}\n"
            f"下午节次上限: {self._cfg('afternoon_period_max', 9)}"
        )
        yield event.plain_result(msg)

    @filter.command("jwxt_push_test")
    async def jwxt_push_test(self, event: AstrMessageEvent):
        try:
            now = datetime.now(self._tz())
            courses = await self._get_today_courses_cached(now.date())
            if not courses:
                yield event.plain_result("测试推送内容：今日无课程安排")
                return

            morning_max = self._to_int(self._cfg("morning_period_max", 5), 5)
            afternoon_max = self._to_int(self._cfg("afternoon_period_max", 9), 9)
            remind_before = self._to_int(self._cfg("session_remind_before_minutes", 60), 60)
            sessions = self._build_day_sessions(courses, morning_max, afternoon_max)

            chunks: List[str] = []
            for session_name in ("morning", "afternoon"):
                session_courses = sessions.get(session_name, [])
                if not session_courses:
                    continue
                start_time = self._session_start_time(session_courses)
                if not start_time:
                    continue
                chunks.append(
                    self._build_session_message(
                        target_date=now.date(),
                        session_name=session_name,
                        start_time=start_time,
                        remind_before=remind_before,
                        courses=session_courses,
                    )
                )

            if not chunks:
                yield event.plain_result("测试推送内容：今日无上午/下午课程")
                return
            yield event.plain_result("测试推送内容：\n" + "\n\n".join(chunks))
        except Exception as e:
            self._log_exception("[jwxt-course-push] 生成测试推送失败", e)
            yield event.plain_result(f"生成测试推送失败: {e}")

    @filter.llm_tool(name="jwxt_get_courses")
    async def jwxt_get_courses_tool(self, event: AstrMessageEvent, day: str):
        """获取课表信息，供大模型函数调用。

        Args:
            day(string): 查询目标，可填 today/tomorrow/星期一/星期二/星期三/星期四/星期五/星期六/星期日
        """
        try:
            target_date, day_cn = self._resolve_day_input(day)
            courses = await self._fetch_courses_for_day(day_cn)
            yield event.plain_result(self._format_courses_message(target_date, day_cn, courses))
        except Exception as e:
            yield event.plain_result(f"查询课表失败: {e}")

    @filter.llm_tool(name="jwxt_get_push_policy")
    async def jwxt_get_push_policy_tool(self, event: AstrMessageEvent):
        """获取当前插件的上课提醒策略，供大模型函数调用。"""
        remind_before = self._to_int(self._cfg("session_remind_before_minutes", 60), 60)
        morning_max = self._to_int(self._cfg("morning_period_max", 5), 5)
        afternoon_max = self._to_int(self._cfg("afternoon_period_max", 9), 9)
        msg = (
            "当前提醒策略：\n"
            f"- 上午课程：节次 <= {morning_max}，首课前 {remind_before} 分钟提醒\n"
            f"- 下午课程：节次 <= {afternoon_max} 且 > {morning_max}，首课前 {remind_before} 分钟提醒\n"
            "- 每日同一会话同一时段只推送一次"
        )
        yield event.plain_result(msg)

    async def _push_loop(self):
        while not self._stop_event.is_set():
            try:
                await self._try_push_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log_exception("[jwxt-course-push] 推送循环异常", e)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _try_push_once(self):
        if not self._cfg_bool("enable_daily_push", True):
            return

        now = datetime.now(self._tz())

        subs = await self._load_subscriptions()
        targets = [k for k, v in subs.items() if v.get("enabled")]
        if not targets:
            return

        today_courses = await self._get_today_courses_cached(now.date())
        if not today_courses:
            return

        if self._cfg_bool("require_week_filter_for_push", True) and not self._last_week_no:
            if self._last_no_week_warn_on != now.date():
                self._log_warning(
                    "[jwxt-course-push] 当前未获得教学周次，已跳过推送。"
                    "请配置 current_week 或 term_start_date。"
                )
                self._last_no_week_warn_on = now.date()
            return

        morning_max = self._to_int(self._cfg("morning_period_max", 5), 5)
        afternoon_max = self._to_int(self._cfg("afternoon_period_max", 9), 9)
        remind_before = self._to_int(self._cfg("session_remind_before_minutes", 60), 60)
        sessions = self._build_day_sessions(today_courses, morning_max, afternoon_max)

        sent = 0
        for unified_msg_origin in targets:
            sub = subs.get(unified_msg_origin, {})
            marks = set(sub.get("session_push_marks", []))

            for session_name, session_courses in sessions.items():
                if not session_courses:
                    continue
                start_time = self._session_start_time(session_courses)
                if start_time is None:
                    continue

                remind_time = start_time - timedelta(minutes=remind_before)
                session_mark = f"{now.date().isoformat()}|{session_name}"
                if session_mark in marks:
                    continue

                # 在提醒时刻到开课前之间首次命中就推送，避免错过窗口
                if not (remind_time <= now < start_time):
                    continue

                message = self._build_session_message(
                    target_date=now.date(),
                    session_name=session_name,
                    start_time=start_time,
                    remind_before=remind_before,
                    courses=session_courses,
                )
                ok = await self._send_active_message(unified_msg_origin, message)
                if ok:
                    marks.add(session_mark)
                    sent += 1

            sub["session_push_marks"] = sorted(marks)[-20:]
            sub["updated_at"] = now.isoformat()
            subs[unified_msg_origin] = sub

        if sent:
            await self._save_subscriptions(subs)
            self._log_info(f"[jwxt-course-push] 分时段推送完成，成功 {sent} 次")

    async def _get_today_courses_cached(self, day: date) -> List[CourseItem]:
        ttl = self._to_int(self._cfg("course_cache_seconds", 900), 900)
        now = datetime.now(self._tz())

        if (
            self._cache_date == day
            and self._cache_loaded_at is not None
            and (now - self._cache_loaded_at).total_seconds() < ttl
        ):
            return self._cache_courses

        all_courses = await self._fetch_all_courses()
        day_name = WEEKDAY_CN[day.weekday()]
        courses = [c for c in all_courses if c.day == day_name]

        self._cache_date = day
        self._cache_courses = courses
        self._cache_loaded_at = now
        return courses

    def _build_day_sessions(
        self, courses: List[CourseItem], morning_period_max: int, afternoon_period_max: int
    ) -> Dict[str, List[CourseItem]]:
        result: Dict[str, List[CourseItem]] = {"morning": [], "afternoon": []}
        for item in courses:
            pidx = self._period_start_index(item.period)
            if pidx <= 0:
                continue
            if pidx <= morning_period_max:
                result["morning"].append(item)
            elif pidx <= afternoon_period_max:
                result["afternoon"].append(item)
        result["morning"] = self._sort_courses(result["morning"])
        result["afternoon"] = self._sort_courses(result["afternoon"])
        return result

    async def _fetch_courses_for_day(self, day_cn: str) -> List[CourseItem]:
        all_courses = await self._fetch_all_courses()
        day_courses = [c for c in all_courses if c.day == day_cn]
        return self._sort_courses(day_courses)

    async def _fetch_all_courses(self) -> List[CourseItem]:
        account = self._cfg("account", "")
        password = self._cfg("password", "")
        base_url = self._cfg("base_url", "")
        if not account or not password or not base_url:
            raise RuntimeError("插件配置缺少 base_url/account/password")

        self._log_info("[jwxt-course-push] 开始拉取课表数据")
        timeout = float(self._cfg("request_timeout", 20))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": base_url,
            "Referer": f"{base_url.rstrip('/')}/jsxsd/",
        }

        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            await self._jwxt_login(client, base_url, account, password)
            html, week_no = await self._fetch_timetable_html(client, base_url)
            self._last_week_no = week_no
        courses = self._parse_timetable_html(html, week_no)
        self._log_info(
            f"[jwxt-course-push] 课表拉取完成，课程数={len(courses)}，教学周={week_no if week_no else 'unknown'}"
        )
        return courses

    async def _jwxt_login(self, client: httpx.AsyncClient, base_url: str, account: str, password: str):
        base_url = base_url.rstrip("/")
        await client.get(f"{base_url}/jsxsd/")

        max_attempts = int(self._cfg("max_login_attempts", 8))
        for attempt in range(max_attempts):
            captcha_url = f"{base_url}/jsxsd/verifycode.servlet"
            captcha_resp = await client.get(captcha_url, params={"t": int(time.time() * 1000)})
            captcha_raw = await asyncio.to_thread(self._ocr.classification, captcha_resp.content)
            captcha = self._normalize_captcha(captcha_raw)
            if len(captcha) < 4:
                continue

            encoded_account = base64.b64encode(account.encode("utf-8")).decode("utf-8")
            encoded_password = base64.b64encode(password.encode("utf-8")).decode("utf-8")
            encoded = f"{encoded_account}%%%{encoded_password}"

            payload = {
                "userAccount": account,
                "userPassword": "",
                "RANDOMCODE": captcha,
                "encoded": encoded,
                "loginMethod": "LoginToXk",
            }

            resp = await client.post(
                f"{base_url}/jsxsd/xk/LoginToXk",
                data=payload,
                follow_redirects=False,
            )

            if resp.status_code == 302 and "error" not in resp.headers.get("Location", "").lower():
                self._log_info(f"[jwxt-course-push] 登录成功，尝试次数: {attempt + 1}")
                return

        raise RuntimeError("教务登录失败，请检查账号密码或验证码识别")

    async def _fetch_timetable_html(
        self, client: httpx.AsyncClient, base_url: str
    ) -> Tuple[str, Optional[int]]:
        base_url = base_url.rstrip("/")
        main_url = f"{base_url}/jsxsd/xskb/xskb_list.do"
        resp = await client.get(main_url)
        if resp.status_code != 200 or self._looks_like_login_page(resp.text):
            raise RuntimeError("未获取到课表页面，请确认登录状态")

        base_html = resp.text
        soup = BeautifulSoup(base_html, "html.parser")
        xnxq01id = self._selected_option_value(soup, "xnxq01id")
        kbjcmsid = self._selected_option_value(soup, "kbjcmsid")
        current_week = self._resolve_current_week_number()

        if current_week:
            params = {"zc": current_week}
            if xnxq01id:
                params["xnxq01id"] = xnxq01id
            if kbjcmsid:
                params["kbjcmsid"] = kbjcmsid

            week_resp = await client.get(main_url, params=params)
            if (
                week_resp.status_code == 200
                and not self._looks_like_login_page(week_resp.text)
                and ("课表" in week_resp.text or "星期" in week_resp.text)
            ):
                return week_resp.text, current_week

        if "课表" in base_html or "星期" in base_html or "xskb" in base_html.lower():
            return base_html, None
        raise RuntimeError("课表页面结构异常，无法识别")

    def _parse_timetable_html(self, html: str, week_no: Optional[int] = None) -> List[CourseItem]:
        soup = BeautifulSoup(html, "html.parser")
        day_headers = self._extract_weekday_headers(soup)
        course_divs = soup.select("div.kbcontent[id]")

        courses: List[CourseItem] = []
        seen = set()
        for div in course_divs:
            div_id = (div.get("id") or "").strip()
            m = re.match(r".*-(\d+)-(\d+)$", div_id)
            if not m:
                continue
            day_idx = self._to_int(m.group(1), 0)
            slot_idx = self._to_int(m.group(2), 0)
            if day_idx <= 0:
                continue

            raw_text = self._normalize_multiline(div.get_text("\n", strip=True))
            if not raw_text or raw_text in {"&nbsp;", "-", "无"}:
                continue
            day_cn = day_headers.get(day_idx, self._day_by_col(day_idx))
            blocks = [x.strip() for x in re.split(r"-{6,}", raw_text) if x.strip()]
            if not blocks:
                blocks = [raw_text]

            for block in blocks:
                item = self._extract_course_block(block, day_cn, day_idx, slot_idx)
                if not item or not item.name:
                    continue

                if week_no and item.weeks and not self._week_expr_contains(item.weeks, week_no):
                    continue

                key = (
                    item.day,
                    item.period,
                    item.name,
                    item.location,
                    item.teacher,
                    item.weeks,
                )
                if key in seen:
                    continue
                seen.add(key)
                courses.append(item)

        return self._sort_courses(courses)

    def _extract_weekday_headers(self, soup: BeautifulSoup) -> Dict[int, str]:
        headers: Dict[int, str] = {}
        all_th = soup.select("th")
        idx = 1
        for th in all_th:
            text = self._clean_text(th.get_text(" ", strip=True))
            day = self._normalize_weekday(text)
            if not day:
                continue
            headers[idx] = day
            idx += 1
            if idx > 7:
                break

        if not headers:
            for i in range(1, 8):
                headers[i] = self._day_by_col(i)
        return headers

    def _extract_course_block(
        self, block: str, day_cn: str, day_idx: int, slot_idx: int
    ) -> Optional[CourseItem]:
        lines = [self._clean_text(x) for x in block.split("\n")]
        lines = [x for x in lines if x and x != "&nbsp;"]
        if not lines:
            return None

        name = lines[0]
        teacher = ""
        building = ""
        location = ""
        weeks_expr = ""
        period = f"第{slot_idx}时段"

        for line in lines[1:]:
            if not line or line.startswith("通知单编号") or line.startswith("班级") or line.startswith("备注"):
                continue
            if "【" in line and "】" in line and not building:
                building = line
                continue

            week_match = re.search(r"(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)?\(周\)\[[0-9\-]+节\])", line)
            if week_match:
                weeks_expr = week_match.group(1)
                period_match = re.search(r"\[([0-9\-]+节)\]", weeks_expr)
                if period_match:
                    period = period_match.group(1)
                continue

            if not teacher and any(k in line for k in ["教授", "讲师", "研究员", "助教", "--", "教师"]):
                teacher = line
                continue

            if not location and (
                any(k in line for k in ["教室", "机房", "实验", "楼", "室", "馆"])
                or bool(re.search(r"[A-Za-z\u4e00-\u9fff]*\\d+-\\d+", line))
            ):
                location = line

        if building and location:
            location = f"{building} {location}"
        elif building and not location:
            location = building

        return CourseItem(
            day=day_cn,
            period=period,
            name=name,
            teacher=teacher,
            location=location,
            weeks=weeks_expr,
            day_index=day_idx,
            slot_index=slot_idx,
            raw_text=block,
        )

    def _sort_courses(self, courses: List[CourseItem]) -> List[CourseItem]:
        def period_score(period: str) -> int:
            m = re.search(r"(\d+)", period)
            return int(m.group(1)) if m else 999

        return sorted(courses, key=lambda c: (c.day_index or 99, period_score(c.period), c.slot_index or 99, c.name))

    def _format_courses_message(self, target_date: date, day_cn: str, courses: List[CourseItem]) -> str:
        week_suffix = f"（第{self._last_week_no}周）" if self._last_week_no else "（未按周次过滤）"
        title = f"{target_date.isoformat()} {day_cn} 课表{week_suffix}"
        if not courses:
            return f"{title}\n今日无课程安排"

        lines = [title]
        for idx, item in enumerate(courses, start=1):
            lines.append(f"{idx}. {item.name}")
            lines.append(f"   时间: {item.period}")
            if item.teacher:
                lines.append(f"   教师: {item.teacher}")
            if item.location:
                lines.append(f"   地点: {item.location}")
            if item.weeks:
                lines.append(f"   周次: {item.weeks}")
        return "\n".join(lines)

    def _build_session_message(
        self,
        target_date: date,
        session_name: str,
        start_time: datetime,
        remind_before: int,
        courses: List[CourseItem],
    ) -> str:
        session_cn = "上午" if session_name == "morning" else "下午"
        title = (
            f"{self._cfg('push_prefix', '【上课提醒】')}\n"
            f"{target_date.isoformat()} {session_cn}课程将在 "
            f"{start_time.strftime('%H:%M')} 开始（提前{remind_before}分钟提醒）"
        )
        lines = [title]
        for idx, item in enumerate(courses, start=1):
            lines.append(f"{idx}. {item.name} {item.period}")
            if item.location:
                lines.append(f"   地点: {item.location}")
            if item.teacher:
                lines.append(f"   教师: {item.teacher}")
        return "\n".join(lines)

    def _session_start_time(self, courses: List[CourseItem]) -> Optional[datetime]:
        if not courses:
            return None
        now = datetime.now(self._tz())
        times: List[datetime] = []
        for item in courses:
            pidx = self._period_start_index(item.period)
            dt = self._period_start_datetime(now.date(), pidx)
            if dt:
                times.append(dt)
        if not times:
            return None
        return min(times)

    def _period_start_index(self, period_text: str) -> int:
        # 01-02节 -> 1 ; 8-9节 -> 8
        m = re.search(r"(\d+)", period_text or "")
        if not m:
            return -1
        return self._to_int(m.group(1), -1)

    def _period_start_datetime(self, day: date, period_idx: int) -> Optional[datetime]:
        if period_idx <= 0:
            return None
        mapping = self._cfg("period_start_time_map", None)
        if isinstance(mapping, str):
            text = mapping.strip()
            if text:
                try:
                    parsed = json.loads(text)
                    mapping = parsed if isinstance(parsed, dict) else None
                except Exception:
                    mapping = None
            else:
                mapping = None
        if not isinstance(mapping, dict):
            mapping = {
                "1": "08:30",
                "2": "09:15",
                "3": "10:10",
                "4": "10:55",
                "5": "11:40",
                "6": "14:20",
                "7": "15:05",
                "8": "16:00",
                "9": "16:45",
                "10": "19:10",
                "11": "20:00",
                "12": "20:50",
            }
        key = str(period_idx)
        hhmm = mapping.get(key) or mapping.get(key.zfill(2))
        if not hhmm or not isinstance(hhmm, str):
            return None
        try:
            hh, mm = hhmm.strip().split(":")
            return datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=self._tz())
        except Exception:
            return None

    async def _send_active_message(self, unified_msg_origin: str, text: str) -> bool:
        chain = MessageChain().message(text)
        try:
            # AstrBot v4 推荐方式：使用 unified_msg_origin 主动推送
            await self.context.send_message(unified_msg_origin, chain)
            return True
        except TypeError:
            # 兼容部分环境的旧签名
            session = await self.context.conversation_manager.get_session(
                unified_msg_origin, create=False
            )
            if not session:
                self._log_warning(f"[jwxt-course-push] 会话不存在，跳过推送: {unified_msg_origin}")
                return False
            await self.context.send_message(session=session, message_chain=chain, use_t2i=False)
            return True
        except Exception as e:
            self._log_exception(f"[jwxt-course-push] 推送失败({unified_msg_origin})", e)
            return False

    async def _load_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        data = await self.get_kv_data("subscriptions", {})
        if isinstance(data, dict):
            return data
        return {}

    async def _save_subscriptions(self, data: Dict[str, Dict[str, Any]]):
        await self.put_kv_data("subscriptions", data)

    def _resolve_target_day(self, mode: str) -> Tuple[date, str]:
        now = datetime.now(self._tz()).date()
        if mode == "tomorrow":
            target = now + timedelta(days=1)
        else:
            target = now
        return target, WEEKDAY_CN[target.weekday()]

    def _resolve_day_input(self, day: str) -> Tuple[date, str]:
        value = self._clean_text(day).lower()
        today = datetime.now(self._tz()).date()
        if value in {"today", "今天", "今日"}:
            target = today
        elif value in {"tomorrow", "明天", "次日"}:
            target = today + timedelta(days=1)
        else:
            weekday_map = {
                "星期一": 0,
                "星期二": 1,
                "星期三": 2,
                "星期四": 3,
                "星期五": 4,
                "星期六": 5,
                "星期日": 6,
                "星期天": 6,
                "周一": 0,
                "周二": 1,
                "周三": 2,
                "周四": 3,
                "周五": 4,
                "周六": 5,
                "周日": 6,
                "周天": 6,
            }
            idx = weekday_map.get(day.strip())
            if idx is None:
                raise ValueError("day 参数无效，请使用 today/tomorrow/星期X")
            delta = (idx - today.weekday()) % 7
            target = today + timedelta(days=delta)
        return target, WEEKDAY_CN[target.weekday()]

    def _looks_like_login_page(self, html: str) -> bool:
        marker = html[:5000]
        return ("userAccount" in marker and "RANDOMCODE" in marker) or "LoginToXk" in marker

    def _selected_option_value(self, soup: BeautifulSoup, select_id: str) -> str:
        selected = soup.select_one(f"select#{select_id} option[selected]")
        if selected and selected.get("value"):
            return self._clean_text(selected.get("value"))
        first = soup.select_one(f"select#{select_id} option")
        if first and first.get("value"):
            return self._clean_text(first.get("value"))
        return ""

    def _resolve_current_week_number(self) -> Optional[int]:
        override = self._to_int(self._cfg("current_week", 0), 0)
        if override > 0:
            return override

        start_date_str = self._cfg("term_start_date", "")
        if not start_date_str:
            return None

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            self._log_warning("[jwxt-course-push] term_start_date 格式错误，应为 YYYY-MM-DD")
            return None

        base_week = self._to_int(self._cfg("term_start_week", 1), 1)
        now = datetime.now(self._tz()).date()
        delta_days = (now - start_date).days
        week_no = base_week + (delta_days // 7)
        if week_no < 1:
            return 1
        return week_no

    def _week_expr_contains(self, week_expr: str, week_no: int) -> bool:
        if week_no <= 0:
            return True
        # Example: 1-8(周)[01-02节] or 1,3,5(周)[03-04节]
        m = re.search(r"([0-9,\-]+)\(周\)", week_expr)
        if not m:
            return True
        seg = m.group(1)
        for part in seg.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                start = self._to_int(a, -1)
                end = self._to_int(b, -1)
                if start <= week_no <= end:
                    return True
            else:
                if self._to_int(part, -1) == week_no:
                    return True
        return False

    def _normalize_captcha(self, captcha: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9]", "", (captcha or "")).strip()
        return value[:4]

    def _normalize_weekday(self, text: str) -> str:
        mapping = {
            "星期一": "星期一",
            "周一": "星期一",
            "星期二": "星期二",
            "周二": "星期二",
            "星期三": "星期三",
            "周三": "星期三",
            "星期四": "星期四",
            "周四": "星期四",
            "星期五": "星期五",
            "周五": "星期五",
            "星期六": "星期六",
            "周六": "星期六",
            "星期日": "星期日",
            "星期天": "星期日",
            "周日": "星期日",
            "周天": "星期日",
        }
        for key, val in mapping.items():
            if key in text:
                return val
        return ""

    def _day_by_col(self, col_idx: int) -> str:
        if 1 <= col_idx <= 7:
            return WEEKDAY_CN[col_idx - 1]
        return f"第{col_idx}列"

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "")
        return text.strip()

    def _normalize_multiline(self, text: str) -> str:
        lines = [self._clean_text(x) for x in (text or "").split("\n")]
        lines = [x for x in lines if x]
        return "\n".join(lines)

    def _to_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _cfg(self, key: str, default: Any = None) -> Any:
        val = self.config.get(key, default)
        if isinstance(val, str):
            return val.strip()
        return val

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        val = self._cfg(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in {"1", "true", "yes", "on"}
        return bool(val)

    def _tz(self) -> ZoneInfo:
        name = self._cfg("timezone", "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    def _resolve_log_file_path(self) -> str:
        path = self._cfg("log_file_path", "runtime.log")
        if not path:
            path = "runtime.log"
        if os.path.isabs(path):
            return path
        return os.path.join(os.path.dirname(__file__), path)

    def _file_log(self, level: str, message: str):
        if not self._cfg_bool("enable_file_log", True):
            return
        try:
            log_path = self._resolve_log_file_path()
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            ts = datetime.now(self._tz()).strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} [{level}] {message}\n"
            with self._log_lock:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    def _log_info(self, message: str):
        self._file_log("INFO", message)

    def _log_warning(self, message: str):
        self._file_log("WARN", message)

    def _log_exception(self, message: str, exc: Exception):
        stack = traceback.format_exc(limit=6)
        self._file_log("ERROR", f"{message}: {exc}\n{stack}")
