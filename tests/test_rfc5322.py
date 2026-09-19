"""POST /imports/rfc5322：原始头文本解析、逐项校验、整批一次性写入。"""

from __future__ import annotations


def _block(*lines: str) -> str:
    return "\r\n".join(lines) + "\r\n"


def test_import_basic_threading_and_snapshot(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <A>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject: 例会通知",
        ),
        _block(
            "Message-ID: <B>",
            "In-Reply-To: <A>",
            "References: <A>",
            "Date: Fri, 18 Sep 2026 10:00:00 +0000",
            "Subject: Re: 例会通知",
        ),
    ]})
    assert r.status_code == 201
    body = r.json()
    assert body["totals"]["submitted"] == 2
    assert body["totals"]["unique_new"] == 2
    assert body["totals"]["trees"] == 1

    tree = body["threads"][0]
    assert tree["message_id"] == "A"
    assert tree["parent_id"] is None
    assert [c["message_id"] for c in tree["children"]] == ["B"]
    assert tree["children"][0]["in_reply_to"] == "A"

    # 与 /batches 相同的快照语义：GET /batches/{id} 原样返回
    assert client.get(f"/batches/{body['batch_id']}").json() == body
    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == body["batch_id"]

    got = client.get("/messages/B").json()
    assert got["parent_id"] == "A"
    assert got["subject"] == "Re: 例会通知"


def test_import_folded_and_rfc2047_encoded_subject(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <E1>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            # 折行 + Q 编码词：相邻编码词之间的空白按 RFC 2047 丢弃
            "Subject: =?UTF-8?Q?=E4=BE=8B=E4=BC=9A?=",
            "\t=?UTF-8?Q?meeting?=",
        ),
        _block(
            "Message-ID: <E2>",
            "Date: Fri, 18 Sep 2026 09:05:00 +0000",
            # B 编码词与普通文本混排
            "Subject: =?UTF-8?B?5L6L5Lya?= notice",
        ),
    ]})
    assert r.status_code == 201
    subjects = {m["message_id"]: m["subject"] for m in r.json()["threads"]}
    assert subjects == {"E1": "例会meeting", "E2": "例会 notice"}


def test_import_repeated_message_id_headers_first_candidate_wins(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <first@x>",
            "Message-ID: <second@x>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject: s",
        ),
    ]})
    assert r.status_code == 201
    assert r.json()["threads"][0]["message_id"] == "first@x"
    assert client.get("/messages/first@x").status_code == 200
    assert client.get("/messages/second@x").status_code == 404


def test_import_multiple_candidates_in_appearance_order(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <R>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject: root",
        ),
        _block(
            "Message-ID: <C>",
            # In-Reply-To 多个候选：按出现顺序取首个
            "In-Reply-To: <R> <other>",
            # 重复 References 头 + 折行：全部候选按出现顺序收集
            "References: <R>",
            "\t<mid>",
            "References: <tail>",
            "Date: Fri, 18 Sep 2026 10:00:00 +0000",
            "Subject: child",
        ),
    ]})
    assert r.status_code == 201
    got = client.get("/messages/C").json()
    assert got["in_reply_to"] == "R"
    assert got["references"] == ["R", "mid", "tail"]
    assert got["parent_id"] == "R"


def test_import_date_timezone_normalized_to_utc(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <T1>",
            "Date: Fri, 18 Sep 2026 10:00:00 +0800",
            "Subject: tz",
        ),
        _block(
            "Message-ID: <T2>",
            # 无时区：按 UTC 处理
            "Date: 18 Sep 2026 02:00:00",
            "Subject: naive",
        ),
    ]})
    assert r.status_code == 201
    sent = {m["message_id"]: m["sent_at"] for m in r.json()["threads"]}
    assert sent == {
        "T1": "2026-09-18T02:00:00Z",
        "T2": "2026-09-18T02:00:00Z",
    }


def test_import_validation_errors_per_item_and_nothing_written(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        # 第 1 项：缺 Message-ID、Date 非法
        _block(
            "Date: not-a-date",
            "Subject: broken",
        ),
        # 第 2 项：合法
        _block(
            "Message-ID: <ok>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject: fine",
        ),
        # 第 3 项：缺 Subject、Message-ID 无尖括号
        _block(
            "Message-ID: no-brackets",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
        ),
    ]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["message"] == "invalid rfc5322 headers; nothing was imported"
    errors = detail["errors"]
    assert [e["index"] for e in errors] == [1, 3]
    assert errors[0]["errors"] == [
        "missing or malformed required header: Message-ID",
        "malformed Date header: 'not-a-date'",
    ]
    assert errors[1]["errors"] == [
        "missing or malformed required header: Message-ID",
        "missing required header: Subject",
    ]

    # 任一项失败 -> 整批不写库
    assert client.get("/batches").json()["total"] == 0
    assert client.get("/messages/ok").status_code == 404


def test_import_empty_subject_rejected(client):
    r = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <S>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject:   ",
        ),
    ]})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == [
        {"index": 1, "errors": ["Subject header must not be empty"]}
    ]
    assert client.get("/batches").json()["total"] == 0


def test_import_empty_headers_list_rejected(client):
    r = client.post("/imports/rfc5322", json={"headers": []})
    assert r.status_code == 422


def test_import_reuses_archive_service_duplicate_semantics(client):
    first = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <D>",
            "Date: Fri, 18 Sep 2026 09:00:00 +0000",
            "Subject: first",
        ),
    ]})
    assert first.status_code == 201

    second = client.post("/imports/rfc5322", json={"headers": [
        _block(
            "Message-ID: <D>",
            "Date: Fri, 18 Sep 2026 10:00:00 +0000",
            "Subject: again",
        ),
    ]})
    assert second.status_code == 201
    body = second.json()
    assert body["duplicates"] == [{
        "message_id": "D",
        "archived": True,
        "first_submitted_seq": None,
        "occurrences": [1],
    }]
    assert body["totals"]["archived_duplicates"] == 1
    # 已归档邮件不被改写
    assert client.get("/messages/D").json()["subject"] == "first"
