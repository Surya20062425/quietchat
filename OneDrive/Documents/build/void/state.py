"""Runtime state shared across the process: current session, model, flags, cancellation."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


class CancellationToken:
    """Thread-safe cancellation flag. Set from the UI thread, checked inside the agent loop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class RuntimeState:
    """In-memory runtime state for one CLI process. The agent loop reads this; slash
    commands and CLI flags mutate it. Never persisted directly (config.py owns files)."""

    # session
    session_id: str | None = None
    session_title: str | None = None
    resumed_from: str | None = None
    turn_count: int = 0

    # model routing overrides (slash /model or --model)
    model_override: str | None = None
    provider_override: str | None = None

    # tool filtering overrides
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] = field(default_factory=list)

    # flags
    yolo: bool = False
    safe_mode: bool = False

    # skills preloaded for the session (/skills load)
    loaded_skills: dict[str, str] = field(default_factory=dict)  # name -> content

    # budget / usage tracking
    budget_used_tokens: int = 0
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    calls_made: int = 0

    # per-turn / per-loop bookkeeping (reset each run_conversation)
    iteration: int = 0
    tokens_since_compression: int = 0
    last_error: str = ""
    last_response: str = ""
    active_tools: list[str] = field(default_factory=list)
    memory_enabled: bool = True
    depth: int = 0
    stream_enabled: bool = True
    goal: str = ""
    api_key: str = ""          # explicit key override (rarely used; pools come from config)
    on_event: object | None = None      # callable(event: dict) -> None
    approval_hook: object | None = None  # callable(text: str) -> bool

    # interrupt + worktree
    cancel_token: CancellationToken = field(default_factory=CancellationToken)
    worktree_dir: str | None = None

    # aliases so the agent loop can speak in `provider`/`model` terms
    @property
    def provider(self) -> str | None:
        return self.provider_override

    @property
    def model(self) -> str | None:
        return self.model_override

    @property
    def turn(self) -> int:
        return self.turn_count


    def reset_for_turn(self) -> None:
        self.cancel_token.reset()

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens_total += int(prompt_tokens or 0)
        self.completion_tokens_total += int(completion_tokens or 0)
        self.budget_used_tokens = self.prompt_tokens_total + self.completion_tokens_total
        self.calls_made += 1
