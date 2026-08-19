"""运行中插话队列测试。"""
from __future__ import annotations

from app.agent import steering


def test_enqueue_drain_order():
    cid = 9001
    steering.clear(cid)
    assert not steering.has_pending(cid)
    steering.enqueue(cid, "第一条")
    steering.enqueue(cid, "第二条")
    assert steering.has_pending(cid)
    got = steering.drain(cid)
    assert got == ["第一条", "第二条"]      # 按到达顺序
    assert steering.drain(cid) == []        # 取空
    assert not steering.has_pending(cid)


def test_enqueue_ignores_empty():
    cid = 9002
    steering.clear(cid)
    steering.enqueue(cid, "")
    assert not steering.has_pending(cid)


def test_isolated_per_chat():
    steering.clear(1)
    steering.clear(2)
    steering.enqueue(1, "a")
    steering.enqueue(2, "b")
    assert steering.drain(1) == ["a"]
    assert steering.drain(2) == ["b"]


def test_clear():
    cid = 9003
    steering.enqueue(cid, "x")
    steering.clear(cid)
    assert steering.drain(cid) == []
