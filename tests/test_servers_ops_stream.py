"""OPS-F07: in-memory byte budget, close wakeup and per-terminal ACK isolation."""
import asyncio
from types import SimpleNamespace

import pytest

from app.servers.desktop import _StreamSink
from app.servers.hub import Terminal


async def turns():
    for _ in range(8):
        await asyncio.sleep(0)


async def test_sink_byte_budget_preserves_large_writes_and_handshake_leftovers():
    sink = _StreamSink(capacity=8)
    payload = b'abcdefghijklmnopqrstuvwx'
    writer = asyncio.create_task(sink.send_bytes(payload))
    await turns()
    assert len(sink._buf) == 8 and not writer.done()
    assert await sink.read_exact(3) == payload[:3]
    await turns()
    assert len(sink._buf) == 8 and not writer.done()
    result = bytearray(payload[:3])
    while len(result) < len(payload):
        result.extend(await sink.read_chunk(timeout=1))
        assert len(sink._buf) <= sink.capacity
    await asyncio.wait_for(writer, 1)
    assert bytes(result) == payload
    with pytest.raises(ValueError):
        await sink.read_exact(9)
    await sink.close()
    with pytest.raises(ConnectionError):
        await sink.read_chunk()


async def test_sink_close_wakes_blocked_producers_and_consumers():
    full = _StreamSink(capacity=4)
    await full.send_bytes(b'abcd')
    writers = [asyncio.create_task(full.send_bytes(b'efgh')) for _ in range(2)]
    empty = _StreamSink(capacity=4)
    readers = [asyncio.create_task(empty.read_chunk()), asyncio.create_task(empty.read_exact(2))]
    await turns()
    assert all(not task.done() for task in writers + readers)
    await full.close()
    await empty.close()
    results = await asyncio.wait_for(asyncio.gather(*writers, *readers, return_exceptions=True), 1)
    assert all(isinstance(result, ConnectionError) for result in results)
    assert await full.read_chunk() == b'abcd'  # no silent loss of already accepted bytes


async def test_sink_read_timeout_does_not_lose_bytes_or_wake_a_closed_stream():
    sink = _StreamSink(capacity=8)
    await sink.send_bytes(b'ab')
    with pytest.raises(TimeoutError):
        await sink.read_exact(4, timeout=0.001)
    await sink.send_bytes(b'cd')
    assert await sink.read_exact(4) == b'abcd'
    await sink.close()
    with pytest.raises(ConnectionError):
        await sink.send_bytes(b'e')


async def test_slow_desktop_blocks_its_ack_not_other_terminal_or_node_dispatch():
    messages = []

    async def send_json(message):
        messages.append(message)

    session = SimpleNamespace(channel=SimpleNamespace(send_json=send_json), terminals={})
    slow, fast = _StreamSink(capacity=4), _StreamSink(capacity=16)
    a, b = Terminal(session, 1, slow), Terminal(session, 2, fast)
    for terminal in (a, b):
        session.terminals[terminal.sid] = terminal
        terminal.start(asyncio.create_task)
        terminal.ready.set()
    try:
        a.feed(b'abcd')
        await turns()
        first_ack = sum(m.get('n', 0) for m in messages if m.get('sid') == 1)
        assert first_ack == 4
        # feed is the node receive loop's synchronous/nonblocking entry point.
        a.feed(b'efgh')
        b.feed(b'other')
        await turns()
        assert not a.pump.done() and len(slow._buf) == 4
        assert sum(m.get('n', 0) for m in messages if m.get('sid') == 1) == first_ack
        assert any(m.get('sid') == 2 and m.get('n') == 5 for m in messages)
        assert await fast.read_chunk() == b'other'
        assert await slow.read_chunk() == b'abcd'
        await turns()
        assert await slow.read_chunk() == b'efgh'
        assert sum(m.get('n', 0) for m in messages if m.get('sid') == 1) == 8
        a.feed(b'ijkl')
        a.feed(b'mnop')
        await turns()
    finally:
        # The desktop relay closes its sink before terminal.close, releasing the pump.
        await slow.close()
        await fast.close()
        for terminal in (a, b):
            await terminal.close()
        await asyncio.wait_for(asyncio.gather(a.pump, b.pump), 1)
