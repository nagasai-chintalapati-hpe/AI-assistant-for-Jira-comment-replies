"""RabbitMQ message broker for async webhook event processing."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class MessageBroker:
    """RabbitMQ broker wrapper with graceful degradation."""

    def __init__(
        self,
        url: str = "",
        queue_name: str = "",
        prefetch_count: int = 0,
    ) -> None:
        from src.config import settings

        self._url = url or settings.queue.url
        self._queue_name = queue_name or settings.queue.queue_name
        self._prefetch_count = prefetch_count or settings.queue.prefetch_count
        self._connection: Any = None
        self._channel: Any = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        if settings.queue.enabled:
            self._connect()

    # Connection management

    def _connect(self) -> None:
        """Establish a blocking AMQP connection and declare the queue."""
        try:
            import pika  # type: ignore[import]

            params = pika.URLParameters(self._url)
            params.heartbeat = 60
            params.blocked_connection_timeout = 300
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            self._channel.queue_declare(queue=self._queue_name, durable=True)
            self._channel.basic_qos(prefetch_count=self._prefetch_count)
            logger.info(
                "RabbitMQ connected — queue=%s host=%s",
                self._queue_name,
                self._url.split("@")[-1],  # hide credentials in log
            )
        except ImportError:
            logger.warning(
                "pika not installed — RabbitMQ integration disabled "
                "(run: pip install pika)"
            )
            self._connection = None
            self._channel = None
        except Exception as exc:
            logger.warning(
                "RabbitMQ connection failed (%s) — falling back to synchronous processing",
                exc,
            )
            self._connection = None
            self._channel = None

    # Properties

    @property
    def enabled(self) -> bool:
        """``True`` when a live RabbitMQ channel is available."""
        return self._channel is not None

    # Producer

    def publish(self, event_dict: dict) -> bool:
        """Publish event payload to the queue; returns True on success."""
        if not self._channel:
            return False
        try:
            import pika  # type: ignore[import]

            body = json.dumps(event_dict).encode("utf-8")
            self._channel.basic_publish(
                exchange="",
                routing_key=self._queue_name,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    content_type="application/json",
                ),
            )
            logger.debug("Published event to queue '%s'", self._queue_name)
            return True
        except Exception as exc:
            logger.error("Failed to publish event to RabbitMQ queue: %s", exc)
            return False

    # Consumer

    def start_consumer(self, handler: Callable[[dict], None]) -> None:
        """Start a daemon consumer thread that dispatches events to *handler*."""
        if not self._channel:
            logger.info(
                "Queue consumer not started — RabbitMQ channel unavailable"
            )
            return

        def _on_message(ch, method, properties, body: bytes) -> None:
            try:
                event_dict = json.loads(body.decode("utf-8"))
                handler(event_dict)
                ch.basic_ack(delivery_tag=method.delivery_tag)
            except Exception as exc:
                logger.error(
                    "Queue message processing failed (nacking): %s", exc
                )
                ch.basic_nack(
                    delivery_tag=method.delivery_tag, requeue=False
                )

        def _consume_loop() -> None:
            try:
                self._channel.basic_consume(
                    queue=self._queue_name,
                    on_message_callback=_on_message,
                )
                logger.info(
                    "RabbitMQ consumer listening on queue '%s'",
                    self._queue_name,
                )
                while not self._stop_event.is_set():
                    # process_data_events drives heartbeats + delivers messages
                    self._connection.process_data_events(time_limit=1)
            except Exception as exc:
                logger.error("RabbitMQ consumer loop exited with error: %s", exc)

        self._consumer_thread = threading.Thread(
            target=_consume_loop,
            daemon=True,
            name="rabbitmq-consumer",
        )
        self._consumer_thread.start()
        logger.info("RabbitMQ consumer thread started")

    # Shutdown

    def stop(self) -> None:
        """Signal the consumer thread to stop and close the AMQP connection."""
        self._stop_event.set()
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=5)
        try:
            if self._connection and not self._connection.is_closed:
                self._connection.close()
        except Exception:
            pass
        logger.info("RabbitMQ connection closed")
