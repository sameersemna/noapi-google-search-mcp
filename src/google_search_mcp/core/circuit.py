"""Circuit breaker for upstream blocks (primarily Google).

When Google blocks us, *every* subsequent call immediately deepens the block.
The breaker "opens" after a detected block and fails fast for a cooldown,
escalating the cooldown on repeated blocks (10 min → up to 1 h). While open,
Google tools should raise :class:`CircuitOpenError` (an
:class:`~google_search_mcp.errors.UpstreamBlockedError`) telling the model how
long to wait, and suggesting cached/non-Google alternatives.

States: ``CLOSED`` (normal) → ``OPEN`` (failing fast) → ``HALF_OPEN`` (probe).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .. import config
from ..errors import UpstreamBlockedError


class CircuitOpenError(UpstreamBlockedError):
    """Raised while the breaker is open (a block is in effect)."""

    def __init__(self, remaining: float, cooldown: float):
        mins = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
        super().__init__(
            f"Google is temporarily blocked (bot detection). Circuit open for "
            f"~{mins} more minute(s). Serve from cache or use a non-Google tool "
            f"(e.g. wikipedia, feeds) instead of retrying."
        )
        self.remaining = remaining
        self.cooldown = cooldown


class CircuitBreaker:
    """Cooldown-based breaker with exponential escalation."""

    def __init__(
        self,
        *,
        base_cooldown: float = 600.0,
        max_cooldown: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self._clock = clock
        self._opened_at: float | None = None
        self._cooldown = 0.0
        self._consecutive_blocks = 0
        #: Number of times a block was recorded (for /health + tests).
        self.block_count = 0

    @property
    def is_open(self) -> bool:
        return self._remaining() > 0

    def _remaining(self) -> float:
        if self._opened_at is None:
            return 0.0
        elapsed = self._clock() - self._opened_at
        remaining = self._cooldown - elapsed
        return remaining if remaining > 0 else 0.0

    def remaining(self) -> float:
        """Seconds until the circuit closes (0 when closed)."""
        return self._remaining()

    def check(self) -> None:
        """Raise :class:`CircuitOpenError` if the circuit is open."""
        remaining = self._remaining()
        if remaining > 0:
            raise CircuitOpenError(remaining, self._cooldown)

    def record_block(self) -> float:
        """Register a detected block; opens the circuit. Returns cooldown."""
        self.block_count += 1
        self._consecutive_blocks += 1
        cooldown = min(
            self.max_cooldown,
            self.base_cooldown * (2 ** (self._consecutive_blocks - 1)),
        )
        self._cooldown = cooldown
        self._opened_at = self._clock()
        return cooldown

    def record_success(self) -> None:
        """Register a successful call; closes the circuit and resets backoff."""
        self._opened_at = None
        self._cooldown = 0.0
        self._consecutive_blocks = 0


# Module-level breaker for Google.
google_breaker = CircuitBreaker(
    base_cooldown=float(config.CIRCUIT_COOLDOWN_SECONDS),
    max_cooldown=float(config.CIRCUIT_MAX_COOLDOWN_SECONDS),
)
