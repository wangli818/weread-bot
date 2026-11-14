#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时看门狗：核对 GitHub 原生 schedule 到底有没有触发。

为什么需要它：
    GitHub 的 schedule 是"尽力而为"，不触发既不会报错，也不会在 Actions 里
    留下任何痕迹，你只能看到"今天什么都没有"。这个仓库的实测数据：
      · 2026-10-09 ~ 10-15、2026-11-07 ~ 11-13 两段时间定时器整体静默，一次没跑；
      · 正常时也会比 cron 晚 4~8 小时启动，偶尔直接被丢弃。
    所以不能只看 cron 写对了没有，必须事后核对一次。

判定逻辑：
    1. 从 .github/workflows/<workflow> 解析 cron（每日固定时刻，支持 ``*`` / ``*/n``）；
    2. 换算成本地时区，取"最近一个已经超过宽限期的槽位"；
       —— 用"最近一个"而不是"今天第一个"，这样即使本 workflow 自己被延迟到
          次日凌晨才跑，判定也不会错位到没有槽位的新一天；
    3. 查询该 workflow 在槽位之后的 event=schedule 运行记录：
       有任何一次 → 正常，静默退出（每轮最多一条通知，不打扰）；
       一次都没有 → 判定漏跑，输出告警正文并置 missing=true。

用法：
    python scripts/watchdog.py --repo owner/name --dry-run
    python scripts/watchdog.py --repo owner/name --workflow renew-cookie.yml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = "auto-reading.yml"
DEFAULT_MESSAGE_FILE = "watchdog-message.txt"
API_ROOT = "https://api.github.com"
CRON_RE = re.compile(r"cron:\s*['\"]?([^'\"\n#]+)")


def resolve_timezone(name: str) -> tzinfo:
    """解析时区；没有 tzdata 的环境（例如 Windows 本地）退回固定偏移。

    GitHub runner 自带 /usr/share/zoneinfo，正常走 ZoneInfo；
    本地验证时常见 ``No module named 'tzdata'``，用固定偏移兜底即可。
    """
    try:
        return ZoneInfo(name)
    except Exception:
        fallback = {"Asia/Shanghai": 8, "Asia/Chongqing": 8, "PRC": 8}.get(name)
        if fallback is None:
            raise
        return timezone(timedelta(hours=fallback))


def expand_hour_field(field: str) -> Optional[List[int]]:
    """展开小时的 cron 字段，支持 ``*``、``*/n`` 与整数。"""
    if field == "*":
        return list(range(24))
    if field.startswith("*/"):
        step_text = field[2:]
        if step_text.isdigit() and int(step_text) > 0:
            return list(range(0, 24, int(step_text)))
        return None
    if field.isdigit():
        return [int(field) % 24]
    return None


def parse_crons(workflow_path: Path) -> List[Tuple[int, int]]:
    """从 workflow 文件解析 cron，返回 [(hour, minute)]（UTC）。

    只关心"每天应该在哪些时刻跑"，因此只解析每天都触发的表达式
    （时、分为 ``*`` / ``*/n`` / 整数）；周/月字段不是 ``*`` 的直接跳过。
    """
    if not workflow_path.exists():
        raise FileNotFoundError(f"找不到 workflow 文件: {workflow_path}")

    slots: List[Tuple[int, int]] = []
    for raw in CRON_RE.findall(workflow_path.read_text(encoding="utf-8")):
        fields = raw.split()
        if len(fields) != 5:
            continue
        minute, hour, day, month, weekday = fields
        if (day, month, weekday) != ("*", "*", "*"):
            continue
        if not minute.isdigit():
            continue
        hours = expand_hour_field(hour)
        if hours is None:
            continue
        slots.extend((value, int(minute) % 60) for value in hours)

    unique = sorted(set(slots))
    if not unique:
        raise ValueError(
            f"{workflow_path} 里没有可解析的每日 cron，看门狗无法判断期望时刻"
        )
    return unique


def slots_for_days(
    days: Sequence[date], crons: Sequence[Tuple[int, int]], tz: tzinfo
) -> List[datetime]:
    """把这些本地日期对应的运行时刻都算出来。

    cron 里的时刻是 UTC，跨时区后会落到本地的前一天或后一天
    （例如 UTC 18:00 = 北京次日 02:00），所以每个本地日期都要把
    前后一天的 UTC 时刻都换算一遍再筛。
    """
    slots: List[datetime] = []
    for day in days:
        for offset in (-1, 0, 1):
            utc_day = day + timedelta(days=offset)
            for hour, minute in crons:
                moment = datetime(
                    utc_day.year, utc_day.month, utc_day.day,
                    hour, minute, tzinfo=timezone.utc,
                ).astimezone(tz)
                if moment.date() == day:
                    slots.append(moment)
    return sorted(set(slots))


def fetch_runs(
    repo: str,
    workflow: str,
    token: Optional[str],
    event: str,
    limit: int = 50,
) -> List[Dict[str, object]]:
    """查询该 workflow 指定 event 的最近运行记录。"""
    url = (
        f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}"
        f"/runs?event={event}&per_page={limit}"
    )
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "weread-bot-watchdog")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"GitHub API 返回 {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法访问 GitHub API: {exc.reason}") from exc

    runs = payload.get("workflow_runs") or []
    return [run for run in runs if isinstance(run, dict)]


def parse_api_time(value: str) -> datetime:
    """解析 GitHub 返回的 ISO8601 UTC 时间。"""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def runs_since(
    runs: Sequence[Dict[str, object]], cutoff: datetime
) -> List[Dict[str, object]]:
    """筛出 cutoff 之后创建、且带 created_at 的运行记录。"""
    picked: List[Dict[str, object]] = []
    for run in runs:
        created = run.get("created_at")
        if not created:
            continue
        if parse_api_time(str(created)) >= cutoff:
            picked.append(run)
    return picked


def describe_run(run: Dict[str, object], tz: tzinfo) -> str:
    """把一条运行记录格式化成一行摘要。"""
    local = parse_api_time(str(run.get("created_at", ""))).astimezone(tz)
    number = run.get("run_number", "?")
    conclusion = run.get("conclusion") or run.get("status") or "unknown"
    return f"#{number} {local:%m-%d %H:%M} ({conclusion})"


def build_message(
    repo: str,
    workflow: str,
    tz: tzinfo,
    tz_label: str,
    now: datetime,
    expected: Sequence[datetime],
    today_runs: Sequence[Dict[str, object]],
    dispatch_runs: Sequence[Dict[str, object]],
    all_runs: Sequence[Dict[str, object]],
) -> str:
    """生成告警正文。``expected`` 是被判定那一天的全部排班时刻。"""
    day_label = expected[0].strftime("%m-%d")
    slot_text = "、".join(f"{slot:%H:%M}" for slot in expected)
    waited_hours = (now - expected[0]).total_seconds() / 3600
    actions_url = f"https://github.com/{repo}/actions/workflows/{workflow}"

    lines = [
        "⏰ 微信读书定时任务今天没有触发",
        "",
        f"仓库：{repo}",
        f"{day_label}（{tz_label}）排了 {len(expected)} 班：{slot_text}",
        f"到现在（{now:%H:%M}）已经过去 {waited_hours:.1f} 小时，"
        f"一次 schedule 运行都没有",
    ]

    if dispatch_runs:
        latest = dispatch_runs[0]
        latest_at = parse_api_time(
            str(latest.get("created_at", ""))
        ).astimezone(tz)
        lines.append(
            f"期间的手动运行：{len(dispatch_runs)} 次"
            f"（最近 {latest_at:%m-%d %H:%M}，说明是你手动补的）"
        )

    if all_runs:
        last = all_runs[0]
        last_at = parse_api_time(str(last.get("created_at", ""))).astimezone(tz)
        gap_days = (now - last_at).days
        lines.append(
            f"最近一次 schedule 运行：{last_at:%m-%d %H:%M}"
            f"（{gap_days} 天前，{describe_run(last, tz)}）"
        )
    else:
        lines.append("最近一次 schedule 运行：查不到任何记录")

    lines += [
        "",
        "常见原因",
        "· GitHub 会静默停用长期没有 push 的 fork 的定时器"
        "（这个仓库 10-09、11-07 各出现过一次，push 一次即恢复）",
        "· Actions 队列高峰会把 schedule 延迟数小时，甚至直接丢弃",
        "",
        "处理建议",
        "· 先到 Actions 页面确认没有正在排队的运行，再手动补跑"
        "（同一账号同时跑两轮可能被判定为异常）",
        actions_url,
    ]
    return "\n".join(lines)


def write_message_file(path: Path, message: str) -> None:
    """把告警正文写到文件，供下一步复用（避免命令行转义问题）。"""
    path.write_text(message, encoding="utf-8")


def set_github_output(name: str, value: str) -> None:
    """写入 $GITHUB_OUTPUT（本地运行时不生效，静默跳过）。"""
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="核对 GitHub schedule 是否触发，漏跑时输出告警正文",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="owner/name，默认取 $GITHUB_REPOSITORY",
    )
    parser.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"workflow 文件名，默认 {DEFAULT_WORKFLOW}",
    )
    parser.add_argument(
        "--workflow-dir",
        default=str(REPO_ROOT / ".github" / "workflows"),
        help="workflow 文件所在目录",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="判定『哪个时刻』用的时区（默认 Asia/Shanghai）",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=8.0,
        help="槽位过后再等多久才判定漏跑（默认 8 小时，覆盖实测 4~8 小时延迟）",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN", ""),
        help="GitHub token，默认取 $GITHUB_TOKEN",
    )
    parser.add_argument(
        "--now",
        default="",
        help="调试用：用指定时间代替当前时间（格式 2026-11-14T19:30，按时区解释）",
    )
    parser.add_argument(
        "--message-file",
        default=DEFAULT_MESSAGE_FILE,
        help=f"告警正文输出路径，默认 {DEFAULT_MESSAGE_FILE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="打印判定过程，不写文件、不设 output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.repo:
        print("::error::缺少 --repo 或 $GITHUB_REPOSITORY", file=sys.stderr)
        return 1

    tz = resolve_timezone(args.timezone)
    if args.now:
        now = datetime.fromisoformat(args.now).replace(tzinfo=tz)
    else:
        now = datetime.now(tz)
    workflow_path = Path(args.workflow_dir) / args.workflow

    try:
        crons = parse_crons(workflow_path)
        slots = slots_for_days(
            [now.date() - timedelta(days=1), now.date()], crons, tz
        )
        schedule_runs = fetch_runs(
            args.repo, args.workflow, args.token or None, "schedule"
        )
        dispatch_runs = fetch_runs(
            args.repo, args.workflow, args.token or None, "workflow_dispatch"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"::error::看门狗执行失败: {exc}", file=sys.stderr)
        return 1

    cron_text = "、".join(f"{hour:02d}:{minute:02d} UTC" for hour, minute in crons)
    print(f"仓库: {args.repo}")
    print(f"workflow: {args.workflow}（cron: {cron_text}）")
    print(f"当前时间: {now:%Y-%m-%d %H:%M} {args.timezone}")

    # 先找出"最近一个已经过了宽限期的槽位"，用它确定要核对哪一天；
    # 用"最近一个"而不是"今天第一个"，即使本轮自己被延迟到次日凌晨，
    # 也不会错位到还没到点的新一天。
    passed = [
        slot for slot in slots
        if now >= slot + timedelta(hours=args.grace_hours)
    ]
    if not passed:
        print(f"近两天内没有已过 {args.grace_hours:g} 小时宽限期的槽位，跳过核对")
        return 0

    checked_day = passed[-1].date()
    day_slots = [slot for slot in slots if slot.date() == checked_day]
    cutoff_slot = day_slots[0]
    cutoff = cutoff_slot.astimezone(timezone.utc)
    print(
        f"核对日期: {checked_day}（{args.timezone}），"
        f"当天排班: {'、'.join(f'{s:%H:%M}' for s in day_slots)}"
        f"，宽限期 {args.grace_hours:g} 小时"
    )

    todays_schedule = runs_since(schedule_runs, cutoff)
    todays_dispatch = runs_since(dispatch_runs, cutoff)
    print(f"当天 {cutoff_slot:%H:%M} 之后的 schedule 运行: {len(todays_schedule)} 次")
    for run in todays_schedule[:5]:
        print(f"  · {describe_run(run, tz)}")
    if todays_dispatch:
        print(f"当天的手动运行: {len(todays_dispatch)} 次")

    if todays_schedule:
        print("判定: 正常，定时器有触发，不发通知")
        return 0

    message = build_message(
        args.repo,
        args.workflow,
        tz,
        args.timezone,
        now,
        day_slots,
        todays_schedule,
        todays_dispatch,
        schedule_runs,
    )
    print("判定: 漏跑，需要告警")
    print("---- 消息预览 ----")
    print(message)
    print("------------------")

    if args.dry_run:
        print("dry-run：未写文件、未设置 output")
        return 0

    write_message_file(Path(args.message_file), message)
    set_github_output("missing", "true")
    print(f"告警正文已写入 {args.message_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
