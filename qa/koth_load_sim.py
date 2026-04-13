#!/usr/bin/env python3
"""
Authorized load simulator for KOTH labs.

This script creates concurrent "virtual users" that generate TCP connect/probe
traffic against explicitly provided targets and port ranges.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import signal
import time
from dataclasses import dataclass, field


def build_bucket_ports(start: int = 10001, end: int = 10080, bucket_size: int = 10) -> list[int]:
    if bucket_size <= 0:
        raise ValueError("bucket_size must be > 0")
    if start > end:
        raise ValueError("start must be <= end")

    ports: list[int] = []
    bucket_start = start
    while bucket_start <= end:
        bucket_end = min(bucket_start + bucket_size - 1, end)
        ports.extend(range(bucket_start, bucket_end + 1))
        bucket_start += bucket_size
    return ports


def parse_ports(value: str) -> list[int]:
    """
    Parse comma-separated ports and ranges:
    - 10001
    - 10001-10010
    - 10001,10004,10010-10020
    """
    ports: set[int] = set()
    for token in value.split(","):
        item = token.strip()
        if not item:
            continue

        if "-" in item:
            left, right = item.split("-", 1)
            start = int(left)
            end = int(right)
            if start > end:
                start, end = end, start
            ports.update(range(start, end + 1))
        else:
            ports.add(int(item))

    filtered = sorted(p for p in ports if 1 <= p <= 65535)
    if not filtered:
        raise ValueError("no valid ports were parsed")
    return filtered


def random_probe() -> bytes:
    probes = [
        b"\r\n",
        b"PING\r\n",
        b"HEAD / HTTP/1.0\r\nHost: target\r\n\r\n",
        b"GET / HTTP/1.0\r\nHost: target\r\n\r\n",
    ]
    return random.choice(probes)


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    attempts: int = 0
    connect_ok: int = 0
    connect_fail: int = 0
    probe_sent: int = 0
    probe_recv: int = 0
    bytes_sent: int = 0
    bytes_recv: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, **kwargs: int) -> None:
        async with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, getattr(self, key) + value)

    async def snapshot(self) -> dict[str, float]:
        async with self._lock:
            elapsed = max(0.001, time.time() - self.started_at)
            return {
                "elapsed": elapsed,
                "attempts": self.attempts,
                "connect_ok": self.connect_ok,
                "connect_fail": self.connect_fail,
                "probe_sent": self.probe_sent,
                "probe_recv": self.probe_recv,
                "bytes_sent": self.bytes_sent,
                "bytes_recv": self.bytes_recv,
                "attempts_per_sec": self.attempts / elapsed,
            }


async def virtual_user(
    user_id: int,
    host: str,
    ports: list[int],
    duration: int,
    connect_timeout: float,
    think_time_ms: tuple[int, int],
    metrics: Metrics,
    stop_event: asyncio.Event,
) -> None:
    del user_id
    end_time = time.time() + duration
    while not stop_event.is_set() and time.time() < end_time:
        port = random.choice(ports)
        await metrics.add(attempts=1)

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=connect_timeout,
            )
        except (asyncio.TimeoutError, OSError):
            await metrics.add(connect_fail=1)
        else:
            await metrics.add(connect_ok=1)
            payload = random_probe()
            try:
                writer.write(payload)
                await writer.drain()
                await metrics.add(probe_sent=1, bytes_sent=len(payload))
                data = await asyncio.wait_for(reader.read(256), timeout=connect_timeout)
                if data:
                    await metrics.add(probe_recv=1, bytes_recv=len(data))
            except (asyncio.TimeoutError, OSError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        low, high = think_time_ms
        sleep_ms = random.randint(low, high)
        await asyncio.sleep(sleep_ms / 1000.0)


async def reporter(metrics: Metrics, interval: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        snap = await metrics.snapshot()
        print(
            "[stats] "
            f"elapsed={snap['elapsed']:.1f}s "
            f"attempts={int(snap['attempts'])} "
            f"ok={int(snap['connect_ok'])} "
            f"fail={int(snap['connect_fail'])} "
            f"tx={int(snap['bytes_sent'])}B "
            f"rx={int(snap['bytes_recv'])}B "
            f"rate={snap['attempts_per_sec']:.1f}/s"
        )


async def run(args: argparse.Namespace) -> int:
    if args.bucketed_ports:
        ports = build_bucket_ports(
            start=args.bucket_start,
            end=args.bucket_end,
            bucket_size=args.bucket_size,
        )
    else:
        ports = parse_ports(args.ports)

    print(f"[config] target={args.target} users={args.users} duration={args.duration}s ports={len(ports)}")
    if args.bucketed_ports:
        print(
            "[config] bucketed ports enabled: "
            f"{args.bucket_start}-{args.bucket_end} step={args.bucket_size}"
        )

    stop_event = asyncio.Event()
    metrics = Metrics()

    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    tasks = []
    tasks.append(asyncio.create_task(reporter(metrics, args.report_interval, stop_event)))
    for i in range(args.users):
        tasks.append(
            asyncio.create_task(
                virtual_user(
                    user_id=i,
                    host=args.target,
                    ports=ports,
                    duration=args.duration,
                    connect_timeout=args.connect_timeout,
                    think_time_ms=(args.min_think_ms, args.max_think_ms),
                    metrics=metrics,
                    stop_event=stop_event,
                )
            )
        )

    try:
        await asyncio.gather(*tasks[1:])
    finally:
        stop_event.set()
        await asyncio.gather(tasks[0], return_exceptions=True)

    snap = await metrics.snapshot()
    print(
        "[final] "
        f"attempts={int(snap['attempts'])} "
        f"ok={int(snap['connect_ok'])} "
        f"fail={int(snap['connect_fail'])} "
        f"tx={int(snap['bytes_sent'])}B "
        f"rx={int(snap['bytes_recv'])}B "
        f"rate={snap['attempts_per_sec']:.1f}/s"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KOTH authorized network load simulator (TCP probe traffic)."
    )
    parser.add_argument("--target", required=True, help="Target host/IP.")
    parser.add_argument("--users", type=int, default=100, help="Virtual users (default: 100).")
    parser.add_argument("--duration", type=int, default=120, help="Test duration in seconds.")
    parser.add_argument(
        "--ports",
        default="10001-10080",
        help="Port list/ranges (ignored when --bucketed-ports is enabled).",
    )
    parser.add_argument(
        "--bucketed-ports",
        action="store_true",
        help="Enable 10-port buckets (10001-10010, 10011-10020, ...).",
    )
    parser.add_argument("--bucket-start", type=int, default=10001, help="Bucket range start.")
    parser.add_argument("--bucket-end", type=int, default=10080, help="Bucket range end.")
    parser.add_argument("--bucket-size", type=int, default=10, help="Ports per bucket.")
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=1.0,
        help="Connect/read timeout in seconds.",
    )
    parser.add_argument(
        "--min-think-ms",
        type=int,
        default=50,
        help="Minimum delay between user attempts in milliseconds.",
    )
    parser.add_argument(
        "--max-think-ms",
        type=int,
        default=250,
        help="Maximum delay between user attempts in milliseconds.",
    )
    parser.add_argument(
        "--report-interval",
        type=int,
        default=5,
        help="Print stats every N seconds.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.users <= 0:
        raise ValueError("--users must be > 0")
    if args.duration <= 0:
        raise ValueError("--duration must be > 0")
    if args.connect_timeout <= 0:
        raise ValueError("--connect-timeout must be > 0")
    if args.min_think_ms < 0 or args.max_think_ms < 0:
        raise ValueError("think times must be >= 0")
    if args.min_think_ms > args.max_think_ms:
        raise ValueError("--min-think-ms cannot exceed --max-think-ms")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
