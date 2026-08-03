"""Kafka/Redpanda bus using aiokafka. Topics are created with one partition each (see compose),
so per-topic order is preserved, which the event-time driver relies on."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

log = logging.getLogger(__name__)


def _dumps(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


class KafkaSubscription:
    def __init__(self, brokers: str, topic: str, group: str | None, from_beginning: bool) -> None:
        self._consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=brokers,
            group_id=group,
            enable_auto_commit=group is not None,
            auto_offset_reset="earliest" if from_beginning else "latest",
            value_deserializer=lambda v: json.loads(v.decode()),
        )
        self._started = False

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        if not self._started:
            await self._consumer.start()
            self._started = True
        async for record in self._consumer:
            yield record.value

    async def close(self) -> None:
        if self._started:
            await self._consumer.stop()
            self._started = False


class KafkaBus:
    def __init__(self, brokers: str) -> None:
        self.brokers = brokers
        self._producer: AIOKafkaProducer | None = None
        self._subs: list[KafkaSubscription] = []

    async def start(self) -> None:
        if self._producer is None:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.brokers,
                acks="all",
                enable_idempotence=True,
                linger_ms=20,
                value_serializer=_dumps,
            )
            await self._producer.start()

    async def publish(self, topic: str, msg: dict[str, Any], key: str | None = None) -> None:
        if self._producer is None:
            await self.start()
        assert self._producer is not None
        await self._producer.send(topic, msg, key=key.encode() if key else None)

    def subscribe(
        self, topic: str, group: str | None = None, *, from_beginning: bool = True
    ) -> KafkaSubscription:
        sub = KafkaSubscription(self.brokers, topic, group, from_beginning)
        self._subs.append(sub)
        return sub

    async def close(self) -> None:
        for sub in self._subs:
            await sub.close()
        if self._producer is not None:
            await self._producer.flush()
            await self._producer.stop()
            self._producer = None
