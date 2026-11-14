#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用通知出口：复用 weread-bot.py 已实现的通知通道。

什么时候用它：
    1. Actions 作业在"阅读阶段之前"就失败（依赖安装、config.yaml 生成、
       Secret / CURL 校验），此时 weread-bot.py 的 main() 还没走到发通知的分支；
    2. 定时看门狗（scripts/watchdog.py）发现今天的 schedule 根本没有触发。

为什么不在 workflow 里重写一遍通道逻辑：
    通道字段映射、必填校验、代理、消息格式都在 NotificationService 里。
    本脚本只负责"组装配置 + 调用发送"，与主流程共用同一份 Secret 和开关，
    新增通道时不需要改这里。

用法：
    python scripts/notify.py "消息正文"
    python scripts/notify.py "消息正文" --event session_failure
    python scripts/notify.py "消息正文" --dry-run
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # 依赖没装好时仍要能进到"没有通道"的分支给出提示
    requests = None

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PATH = REPO_ROOT / "weread-bot.py"
MESSAGE_LIMIT = 3500


def load_bot_module() -> ModuleType:
    """以模块方式加载 weread-bot.py。

    该文件末尾有 ``if __name__ == "__main__"`` 守卫，导入不会启动阅读会话。
    """
    if not BOT_PATH.exists():
        raise FileNotFoundError(f"找不到运行时文件: {BOT_PATH}")
    spec = importlib.util.spec_from_file_location("weread_bot_runtime", BOT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载运行时文件: {BOT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_notification_config(
    bot: ModuleType, config_path: Path
) -> Tuple[object, str]:
    """组装通知配置。

    优先读取 config.yaml，这样通知开关、触发事件、通道启停与主流程完全一致；
    读不到（比如失败发生在"Create config file"之前）时，退化为
    "只用环境变量里配置完整的通道"，保证通知仍然能发出去。
    """
    if config_path.exists():
        try:
            manager = bot.ConfigManager(str(config_path))
            return manager.config.notification, str(config_path)
        except Exception as exc:  # 配置缺失/非法都不应阻断通知
            logging.warning(
                "读取 %s 失败（%s: %s），回退到环境变量组装通道",
                config_path,
                type(exc).__name__,
                exc,
            )
    manager = object.__new__(bot.ConfigManager)  # 只借用通道组装，不做完整校验
    channels = manager._load_notification_channels({})
    config = bot.NotificationConfig(enabled=True, channels=channels)
    return config, "环境变量"


def build_notification_service(bot: ModuleType, config: object) -> object:
    """构造通知服务，并修正 WxPusher 的"HTTP 200 即成功"误判。

    WxPusher 的极简接口在失败时同样返回 HTTP 200，只是 body 里
    ``code`` 不是 1000（例如 SPT 错误是 1001）。weread-bot.py 的实现只做了
    ``raise_for_status()``，于是"发送失败"会被报成"✅ 发送成功"。
    这里只覆写这一个方法补上响应体校验，其余通道完全走原实现。
    """

    class VerifyingService(bot.NotificationService):
        def _send_wxpusher(self, message: str, wx_config: Dict[str, Any]) -> bool:
            spt = wx_config.get("spt")
            if not spt:
                logging.error("❌ WxPusher SPT 未配置")
                return False
            if requests is None:
                logging.error("❌ 缺少 requests，无法发送 WxPusher 通知")
                return False

            url = (
                "https://wxpusher.zjiecode.com/api/send/message/"
                f"{spt}/{urllib.parse.quote(message)}"
            )
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                logging.error("❌ WxPusher 请求失败: %s", type(exc).__name__)
                return False

            if payload.get("code") == 1000:
                logging.info("✅ WxPusher 通知发送成功")
                return True
            logging.error(
                "❌ WxPusher 返回错误: code=%s msg=%s",
                payload.get("code"),
                payload.get("msg"),
            )
            return False

    return VerifyingService(config)


class CaptureHandler(logging.Handler):
    """收集通知服务自己的状态行（✅ / ❌ / ⚠️），供 Job Summary 展示。

    这些行原本只出现在步骤日志里，需要点开折叠才看得到；把它们推到
    Summary，才能"一眼看出哪个通道成了/没成"。
    """

    PREFIXES = ("✅", "❌", "⚠️", "📊", "🔕")

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage().lstrip()
        except Exception:
            return
        if text.startswith(self.PREFIXES):
            self.lines.append(text)


def active_channel_names(channels: List[object]) -> List[str]:
    """列出已启用通道名，用于日志与 dry-run。"""
    names: List[str] = []
    for channel in channels:
        if getattr(channel, "enabled", False):
            names.append(getattr(channel, "name", "unknown"))
    return names


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用 weread-bot 的通知通道发送一条消息",
    )
    parser.add_argument("message", help="通知正文")
    parser.add_argument(
        "--event",
        default="general",
        choices=[
            "general",
            "session_success",
            "session_failure",
            "multi_user_summary",
            "runtime_error",
        ],
        help="通知事件类型，决定是否被 config.yaml 的 triggers 拦截（默认 general）",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.yaml"),
        help="config.yaml 路径（默认仓库根目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将使用的通道，不真正发送",
    )
    return parser.parse_args(argv)


def write_step_summary(lines: List[str]) -> None:
    """把结果写进 Actions 的 Job Summary，运行页面直接可见。

    只写日志是不够的：日志要点开折叠才看得到，而"没配通道"这种问题
    必须在结果页一眼可见，否则会误以为通知已发出。
    """
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    message = args.message.strip()
    if not message:
        logging.error("消息正文为空，跳过发送")
        return 0
    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 20] + "\n...（内容过长已截断）"

    bot = load_bot_module()
    config, source = build_notification_config(bot, Path(args.config))
    names = active_channel_names(config.channels)
    logging.info("通知配置来源: %s", source)
    logging.info("可用通道: %s", ", ".join(names) if names else "无")

    if not names:
        # 这里必须"响亮地失败"：没有通道等于告警永远发不出去，
        # 若静默退出 0，运行记录会显示绿色，问题被彻底掩盖。
        write_step_summary([
            "### ❌ 通知未发送：没有可用的通知通道",
            "",
            f"- 配置来源：`{source}`",
            "- 已检查的 Secret：`PUSHPLUS_TOKEN`、`TELEGRAM_BOT_TOKEN` + "
            "`TELEGRAM_CHAT_ID`、`WXPUSHER_SPT`、`FEISHU_WEBHOOK_URL`、"
            "`WEWORK_WEBHOOK_URL`、`DINGTALK_WEBHOOK_URL`、`BARK_DEVICE_KEY`、"
            "`NTFY_TOPIC`、`GOTIFY_TOKEN`、`SERVERCHAN3_SENDKEY`、"
            "`PUSHDEER_PUSHKEY`、`APPRISE_URL`",
            "- 处理：仓库 Settings → Secrets and variables → Actions 里补一个通道的 Secret",
        ])
        logging.error(
            "::error::没有可用的通知通道，消息未发送。"
            "请到仓库 Settings → Secrets and variables → Actions 配置至少一个通道"
            "（例如 PUSHPLUS_TOKEN），详见本次运行的 Job Summary"
        )
        return 3

    if args.dry_run:
        logging.info("dry-run：跳过后发送，消息内容如下")
        logging.info("----\n%s\n----", message)
        write_step_summary([
            "### 🧪 dry-run（未真正发送）",
            "",
            f"- 配置来源：`{source}`",
            f"- 将使用的通道：{', '.join(names)}",
        ])
        return 0

    service = build_notification_service(bot, config)
    capture = CaptureHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    try:
        ok = service.send_notification(
            message, event=bot.NotificationEvent(args.event)
        )
    finally:
        root_logger.removeHandler(capture)

    detail = ["", "通道明细："] + [f"- {line}" for line in capture.lines[-8:]]
    if not ok:
        write_step_summary(
            [
                "### ❌ 通知发送失败",
                "",
                f"- 尝试的通道：{', '.join(names)}",
                "- 请核对 token / webhook 是否有效（明细见下）",
            ]
            + detail
        )
        logging.error("通知发送失败，请检查通道配置与网络")
        return 1

    write_step_summary(
        [
            "### ✅ 通知已发送",
            "",
            f"- 通道：{', '.join(names)}",
            f"- 配置来源：`{source}`",
            f"- 事件：`{args.event}`",
        ]
        + detail
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
