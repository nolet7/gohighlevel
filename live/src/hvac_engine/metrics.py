"""Tiny in-process Prometheus-style counters (no extra dependency)."""
from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def inc(self, metric: str, /, **labels: str) -> None:
        with self._lock:
            self._c[(metric, tuple(sorted(labels.items())))] += 1

    def value(self, metric: str, /, **labels: str) -> int:
        return self._c.get((metric, tuple(sorted(labels.items()))), 0)

    def render(self) -> str:
        lines = []
        with self._lock:
            for (name, labels), v in sorted(self._c.items()):
                lab = ",".join(f'{k}="{val}"' for k, val in labels)
                lines.append(f"hge_{name}{{{lab}}} {v}" if lab else f"hge_{name} {v}")
        return "\n".join(lines) + "\n"
