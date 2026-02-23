# AstrBot JWXT 课表推送插件

## 功能
- 自动登录强智教务（验证码 `ddddocr`）
- 查询今日/明日课表
- 分时段主动推送上课信息（按会话订阅）
  - 上午有课：上午第一节课前 1 小时提醒（可配置）
  - 下午有课：下午第一节课前 1 小时提醒（可配置）

## 命令
- `/jwxt_today` 查询今日课表
- `/jwxt_tomorrow` 查询明日课表
- `/jwxt_push_on` 在当前会话开启每日提醒
- `/jwxt_push_off` 在当前会话关闭每日提醒
- `/jwxt_push_status` 查看提醒状态
- `/jwxt_push_test` 立即生成一次提醒内容

## 函数工具（Function Tool）
- `jwxt_get_courses(day)`  
  - `day` 支持：`today` / `tomorrow` / `星期一...星期日`
- `jwxt_get_push_policy()`

说明：
- 该插件加载后，以上工具会出现在 AstrBot 的“函数工具”列表中（前提是你启用了函数调用模型与插件函数工具）。

## 配置
在 AstrBot 插件配置中填写：
- `base_url` 教务地址（例如 `http://jw.nnnu.edu.cn`）
- `account` 教务账号
- `password` 教务密码
- `enable_file_log` 是否启用文件日志（默认 true）
- `log_file_path` 日志文件路径（默认 `runtime.log`，相对路径相对插件目录）
- `current_week` 当前教学周（可选，填了最准确）
- `term_start_date` 学期起始日期（可选，用于自动算周）
- `term_start_week` 起始日期对应教学周（默认 1）
- `session_remind_before_minutes` 提前提醒分钟（默认 60）
- `morning_period_max` 上午节次上限（默认 5）
- `afternoon_period_max` 下午节次上限（默认 9）
- `require_week_filter_for_push` 推送前必须获得当前教学周（默认 true）
- `period_start_time_map` 节次开始时间映射（用于计算一小时前）

说明：
- 插件会优先用 `zc=当前周` 请求课表页，拿到“当周课程”再做提醒，避免把 1-8 周和 9-16 周同时推出来。
- 默认节次时间已按你提供的南宁师大武鸣校区作息设置（1-12 节）。
- 为避免误推整学期课程，默认在“无法确定当前教学周”时不推送（命令查询仍可用）。
- 运行日志默认写入 `astrbot_plugin_jwxt_course_push/runtime.log`。

## 安装
1. 将目录 `astrbot_plugin_jwxt_course_push` 放入 AstrBot 插件目录。
2. 重启 AstrBot。
3. 在插件管理中配置账号密码。
4. 在目标会话执行 `/jwxt_push_on` 开启推送。
