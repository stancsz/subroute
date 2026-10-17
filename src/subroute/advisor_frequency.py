"""Bounded process-local ranking gate, with an approximate target invocation share.

An incoming score is ranked against the previous 256 eligible requests. Stable,
independent request-ID hashes order tied scores and jitter within each rank bin.
No synthetic history is added; the first request uses request-ID jitter.
Count feedback prevents drifting scores from selecting everything or nothing.
For each uninterrupted policy epoch, selected minus target times eligible stays
between -16(1-target) and 16target+1, even on changing traffic. It is not a quota
for short windows and does not claim optimal selection of unknown future asks.
No prompt text is retained; request IDs
remain only in the bounded decision cache, with hashes in the rank window.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import math
import threading


@dataclass
class _Window:
    ranks: deque = field(default_factory=lambda: deque(maxlen=256))
    decisions: OrderedDict = field(default_factory=OrderedDict)
    eligible_count: int = 0
    selected_count: int = 0


class AdvisorFrequencyGate:
    """One lock and at most four independent policy windows per instance.

    A policy key must identify all selection policy settings, including target.
    Repeated request IDs are idempotent while cached (last 1,024 decisions per
    policy); evicted policies or IDs begin fresh. Counters are process-local.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, _Window] = OrderedDict()

    def choose(self, request_id: str, score: float, target: float, policy_key: str) -> dict:
        """Return a detached decision receipt; never store prompt text."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a nonempty string")
        if not isinstance(policy_key, str) or not policy_key:
            raise ValueError("policy_key must be a nonempty string")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and between zero and one")
        if isinstance(target, bool) or target not in (0.3, 0.5, 0.7):
            raise ValueError("target must be 0.3, 0.5, or 0.7")
        digest = hashlib.sha256(request_id.encode("utf-8")).digest()
        tie = int.from_bytes(digest[:16], "big")
        jitter = (int.from_bytes(digest[16:24], "big") >> 11) / (1 << 53)
        rank = (float(score), tie)
        with self._lock:
            window = self._windows.get(policy_key)
            if window is None:
                window = self._windows[policy_key] = _Window()
                if len(self._windows) > 4:
                    self._windows.popitem(last=False)
            self._windows.move_to_end(policy_key)
            cached = window.decisions.get(request_id)
            if cached is not None:
                return dict(cached)
            percentile = (sum(previous < rank for previous in window.ranks) + jitter) / (len(window.ranks) + 1)
            debt = target * (window.eligible_count + 1) - window.selected_count
            # A fixed 16-request feedback scale preserves ranking headroom while
            # bounding rate error under arbitrary trends, not only IID traffic.
            consult = percentile - (1 - target) + debt / 16 >= 0
            window.ranks.append(rank)
            window.eligible_count += 1
            window.selected_count += int(consult)
            decision = {
                "consult": consult,
                "target_rate": target,
                "score": float(score),
                "percentile": percentile,
                **self._stats(window),
            }
            window.decisions[request_id] = decision
            if len(window.decisions) > 1024:
                window.decisions.popitem(last=False)
            return dict(decision)

    def snapshot(self, policy_key: str) -> dict:
        """Return counters without creating, touching, or evicting a window."""
        with self._lock:
            window = self._windows.get(policy_key)
            return self._stats(window) if window else {
                "eligible_count": 0, "selected_count": 0,
                "observed_rate": 0.0, "window_count": 0,
            }

    @staticmethod
    def _stats(window: _Window) -> dict:
        return {
            "eligible_count": window.eligible_count,
            "selected_count": window.selected_count,
            "observed_rate": window.selected_count / window.eligible_count if window.eligible_count else 0.0,
            "window_count": len(window.ranks),
        }
