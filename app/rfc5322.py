"""RFC 5322 邮件头解析：把一组原始头文本归一为现有批次模型。

仅使用 Python 标准库：

- ``email.message_from_string``（compat32 策略）解析头块，折行
  （continuation line）由解析器自动拼接；
- ``get_all`` 取重复字段，保持头中出现顺序；
- ``email.header.decode_header`` 解码 RFC 2047 编码词（``=?charset?B/Q?...?=``）；
- ``email.utils.parsedate_to_datetime`` 解析 Date。

字段提取规则：

- Message-ID（必填）：重复头与头内多个 ``<...>`` 候选均按出现顺序处理，
  取首个非空候选；一个都取不到即视为缺失/格式非法；
- In-Reply-To（可选）：同样按出现顺序取首个候选；
- References（可选）：全部候选按出现顺序收集；
- Date（必填）：解析为 datetime；无时区时按 UTC 处理（交由 MessageIn 归一）；
- Subject（必填）：解码编码词并把折行空白折叠为单个空格；空主题视为非法。

缺失或格式非法的必填字段逐项（带 1 起始的输入序号）汇总为
ImportValidationError；调用方保证任一项失败时不写库。
"""

from __future__ import annotations

import email
import email.header
import email.utils
import re
from datetime import datetime
from typing import Optional

from .schemas import BatchIn, MessageIn

# <...> 形式的 msg-id 候选；尖括号内不允许再出现尖括号
_MSG_ID_RE = re.compile(r"<([^<>]+)>")


class ImportValidationError(Exception):
    """POST /imports/rfc5322：一项或多项头文本校验失败（-> 422，不写库）。"""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"invalid rfc5322 header items: {errors}")


def _collapse_ws(value: str) -> str:
    """折行展开后的空白归一：任意连续空白（含换行）折叠为单个空格。"""

    return " ".join(value.split())


def _decode_words(value: str) -> str:
    """解码 RFC 2047 编码词；未知字符集回退 UTF-8 替换解码，绝不抛异常。"""

    parts: list[str] = []
    for text, charset in email.header.decode_header(value):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(charset or "ascii", errors="replace"))
            except (LookupError, ValueError):
                parts.append(text.decode("utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts)


def _first_msg_id(values: list[str]) -> Optional[str]:
    """按头中出现顺序（先重复头、再头内）取首个非空 <...> 候选。"""

    for value in values:
        for candidate in _MSG_ID_RE.findall(value):
            candidate = candidate.strip()
            if candidate:
                return candidate
    return None


def _all_msg_ids(values: list[str]) -> list[str]:
    """按头中出现顺序（先重复头、再头内）收集全部非空 <...> 候选。"""

    out: list[str] = []
    for value in values:
        for candidate in _MSG_ID_RE.findall(value):
            candidate = candidate.strip()
            if candidate:
                out.append(candidate)
    return out


def parse_headers(raw: str) -> tuple[Optional[MessageIn], list[str]]:
    """解析一段原始头文本。

    成功返回 ``(MessageIn, [])``；任一必填字段缺失或格式非法时返回
    ``(None, [错误, ...])``，错误按字段检查顺序排列。
    """

    msg = email.message_from_string(raw)
    errors: list[str] = []

    message_id = _first_msg_id(msg.get_all("message-id", []))
    if message_id is None:
        errors.append("missing or malformed required header: Message-ID")

    sent_at: Optional[datetime] = None
    date_value = msg.get("date")
    if date_value is None:
        errors.append("missing required header: Date")
    else:
        try:
            sent_at = email.utils.parsedate_to_datetime(_collapse_ws(date_value))
            if sent_at is None:
                raise ValueError("unparseable date")
        except (TypeError, ValueError, OverflowError):
            errors.append(f"malformed Date header: {_collapse_ws(date_value)!r}")

    subject: Optional[str] = None
    subject_value = msg.get("subject")
    if subject_value is None:
        errors.append("missing required header: Subject")
    else:
        subject = _collapse_ws(_decode_words(subject_value))
        if not subject:
            errors.append("Subject header must not be empty")

    in_reply_to = _first_msg_id(msg.get_all("in-reply-to", []))
    references = _all_msg_ids(msg.get_all("references", []))

    if errors:
        return None, errors
    return (
        MessageIn(
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            sent_at=sent_at,
            subject=subject,
        ),
        [],
    )


def to_batch(raw_headers: list[str]) -> BatchIn:
    """把一组原始头文本归一为批次模型。

    全部项解析成功时返回 BatchIn；任一项失败即抛 ImportValidationError
    （逐项携带 1 起始的输入序号），调用方不得写库。
    """

    messages: list[MessageIn] = []
    errors: list[dict] = []
    for index, raw in enumerate(raw_headers, start=1):
        message, item_errors = parse_headers(raw)
        if item_errors:
            errors.append({"index": index, "errors": item_errors})
        else:
            messages.append(message)
    if errors:
        raise ImportValidationError(errors)
    return BatchIn(messages=messages)
