"""Fake Redis for tests — supports both list and stream operations.

Used by the job queue tests to avoid requiring a real Redis. Implements the
subset of Redis commands that JobQueue uses:
- Lists: rpush, blpop, llen (legacy, kept for compatibility)
- Streams: xadd, xreadgroup, xack, xgroup_create, xlen
"""
from __future__ import annotations

import time


class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[str, dict[str, dict]] = {}  # stream -> group -> {consumers, pending}
        self._msg_counter = 0

    # -- list operations (legacy) -------------------------------------------

    def rpush(self, name, value):
        self.lists.setdefault(name, []).append(value)

    def blpop(self, name, timeout=0):
        items = self.lists.get(name) or []
        return (name, items.pop(0)) if items else None

    def llen(self, name):
        return len(self.lists.get(name) or [])

    # -- stream operations --------------------------------------------------

    def xadd(self, stream, fields):
        self._msg_counter += 1
        msg_id = f"{int(time.time() * 1000)}-{self._msg_counter}"
        self.streams.setdefault(stream, []).append((msg_id, dict(fields)))
        return msg_id

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if mkstream and stream not in self.streams:
            self.streams[stream] = []
        if stream not in self.streams:
            raise Exception("ERR no such key")
        if group in self.groups.get(stream, {}):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.setdefault(stream, {})[group] = {
            "consumers": {},
            "pending": {},  # msg_id -> consumer_name
            "last_delivered_id": id,
        }

    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        results = []
        for stream_name, start_id in streams.items():
            if stream_name not in self.streams:
                continue
            stream = self.streams[stream_name]
            grp = self.groups.get(stream_name, {}).get(group)
            if grp is None:
                continue
            # Ensure consumer exists
            grp["consumers"].setdefault(consumer, [])

            delivered = []
            for msg_id, fields in stream:
                # For ">" we deliver only new (never-delivered) messages
                if start_id == ">":
                    if msg_id in grp["pending"]:
                        continue  # already pending
                    delivered.append((msg_id, fields))
                    grp["pending"][msg_id] = consumer
                    grp["consumers"][consumer].append(msg_id)
                    if len(delivered) >= count:
                        break
                else:
                    # Specific ID — deliver messages after that ID
                    pass

            if delivered:
                results.append((stream_name, delivered))

        return results if results else []

    def xack(self, stream, group, *msg_ids):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        acked = 0
        for mid in msg_ids:
            if mid in grp["pending"]:
                del grp["pending"][mid]
                acked += 1
        return acked

    def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def xpending(self, stream, group):
        grp = self.groups.get(stream, {}).get(group)
        if grp is None:
            return 0
        return len(grp["pending"])
