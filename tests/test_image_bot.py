# -*- coding: utf-8 -*-
"""
image_bot 关键行为回归测试

不依赖真实适配器与 Dashboard：storage 用内存字典替身，
Event 用一个只实现被调用接口的桩对象。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shbot.image_bot import Main, MAX_TAG_LENGTH, MAX_IMAGES_PER_TAG  # noqa: E402


class FakeStorage:
    """最小 storage 替身，模拟 ErisPulse storage 的 a 前缀异步接口"""

    def __init__(self):
        self._data = {}

    async def aget(self, key, default=None):
        return self._data.get(key, default)

    async def aset(self, key, value):
        self._data[key] = value
        return True

    async def adelete(self, key):
        existed = key in self._data
        self._data.pop(key, None)
        return existed

    async def aget_all_keys(self):
        return list(self._data.keys())


class FakeClient:
    """HTTP 客户端替身：默认返回 404，用于断言不会触发下载"""

    def __init__(self):
        self.calls = 0

    async def get(self, url, timeout=None):
        self.calls += 1
        raise AssertionError(f"不应发起下载请求: {url}")


class FakeEvent:
    """Event 桩：只暴露 image_bot 实际调用的接口"""

    def __init__(self, text, images=(), user_id="u1"):
        self._text = text
        self._images = list(images)
        self._user_id = user_id
        self.replies = []

    def get_message(self):
        segments = [{"type": "text", "data": {"text": self._text}}]
        segments += [{"type": "image", "data": {"url": u}} for u in self._images]
        return segments

    def get_user_id(self):
        return self._user_id

    async def reply(self, content, method=None, **kwargs):
        self.replies.append((content, method))
        return {"status": "ok"}


def make_module():
    """构造一个绕过 __init__ 依赖注入的模块实例"""
    m = Main.__new__(Main)
    m.sdk = type("S", (), {"client": FakeClient()})()
    m.storage = FakeStorage()
    m.view_id = "ImageBot"

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    m.logger = _Logger()
    return m


# ---------------- 词条名归一化 ----------------

def test_normalize_tag_collapses_whitespace():
    assert Main._normalize_tag("  猫   咪  ") == "猫 咪"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_normalize_tag_rejects_blank(raw):
    assert Main._normalize_tag(raw) is None


def test_normalize_tag_rejects_overlong():
    assert Main._normalize_tag("x" * (MAX_TAG_LENGTH + 1)) is None
    assert Main._normalize_tag("x" * MAX_TAG_LENGTH) == "x" * MAX_TAG_LENGTH


# ---------------- @ 提及剥离 ----------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        # 回归：原实现用 @\\S+ 全局替换，会把邮箱类词条截断
        ("有人问a@b.com时回答", "有人问a@b.com时回答"),
        # 回归：原实现会把「有人问@张三时回答」截成「有人问」
        ("有人问@张三时回答", "有人问@张三时回答"),
        # 真正的提及形态仍需剥离
        ("@bot 猫", "猫"),
        ("<@123456> 猫", "猫"),
        ("猫", "猫"),
    ],
)
def test_strip_mention_does_not_eat_legit_text(raw, expected):
    assert make_module()._strip_mention(raw) == expected


# ---------------- 存储上限 ----------------

def test_store_image_stops_at_limit():
    m = make_module()
    m.storage._data["img_entries:猫"] = [
        {"url": f"http://x/{i}.png", "uploader": "u1", "time": 0}
        for i in range(MAX_IMAGES_PER_TAG)
    ]
    ok = asyncio.run(m._store_image("猫", "http://x/new.png", "u1"))
    assert ok is False
    assert len(m.storage._data["img_entries:猫"]) == MAX_IMAGES_PER_TAG


def test_store_image_writes_when_under_limit():
    m = make_module()
    assert asyncio.run(m._store_image("猫", "http://x/1.png", "u1")) is True
    assert len(m.storage._data["img_entries:猫"]) == 1


# ---------------- 删除 / 读取 ----------------

def test_delete_last_image_only_removes_own():
    m = make_module()
    m.storage._data["img_entries:猫"] = [
        {"url": "http://x/u1.png", "uploader": "u1", "time": 1},
        {"url": "http://x/u2.png", "uploader": "u2", "time": 2},
        {"url": "http://x/u1b.png", "uploader": "u1", "time": 3},
    ]
    deleted = asyncio.run(m._delete_last_image("猫", "u1"))
    assert deleted == "http://x/u1b.png"
    assert [i["url"] for i in m.storage._data["img_entries:猫"]] == [
        "http://x/u1.png",
        "http://x/u2.png",
    ]


def test_delete_last_image_drops_empty_key():
    m = make_module()
    m.storage._data["img_entries:猫"] = [
        {"url": "http://x/u1.png", "uploader": "u1", "time": 1}
    ]
    assert asyncio.run(m._delete_last_image("猫", "u1")) == "http://x/u1.png"
    assert "img_entries:猫" not in m.storage._data


def test_get_entry_returns_none_for_missing():
    assert asyncio.run(make_module()._get_entry("不存在")) is None


# ---------------- 发送路径 ----------------

def test_send_image_url_goes_straight_to_adapter():
    """URL 应直接交给适配器，不再先下载（避免每次发图都多一次网络往返）"""
    m = make_module()
    ev = FakeEvent("猫")
    asyncio.run(m._send_image(ev, "http://x/1.png"))
    assert ev.replies == [("http://x/1.png", "Image")]
    assert m.sdk.client.calls == 0
