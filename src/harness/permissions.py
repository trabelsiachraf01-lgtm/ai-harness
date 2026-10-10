"""Human in the loop. A tool that has side effects asks before every call, unless a rule says
otherwise. The module knows nothing about terminals: the UI supplies the `approver`.

Choices at the prompt: allow once, allow for this session, allow always (saved to disk), or deny
with an optional reason. A rule is keyed on `tool:scope` (e.g. `read:grep`), or on the whole tool.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Remember = Literal["once", "session", "always"]


@dataclass(frozen=True)
class Request:
    tool: str
    scope: str | None  # e.g. the program name; None means the whole tool
    detail: str  # exactly what will run, as shown to the user


@dataclass(frozen=True)
class Decision:
    allow: bool
    remember: Remember = "once"
    whole_tool: bool = False  # remember the tool itself, not just this scope
    reason: str | None = None  # why it was denied, passed back to the model


@dataclass(frozen=True)
class Verdict:
    allow: bool
    source: str  # "once" | "session" | "always" (how it was allowed, or by which rule) | "denied"
    reason: str | None = None


Approver = Callable[[Request], Decision]


def deny_all(request: Request) -> Decision:
    return Decision(False, reason="no one is available to approve this (non-interactive run)")


def allow_all(request: Request) -> Decision:
    return Decision(True)


class Permissions:
    def __init__(self, approver: Approver = deny_all, store: Path | None = None):
        self.approver = approver
        self.store = store
        self.session: set[str] = set()
        self.always: set[str] = self._load()

    def check(self, request: Request) -> Verdict:
        for key in (request.tool, f"{request.tool}:{request.scope}"):
            if key in self.session:
                return Verdict(True, "session")
            if key in self.always:
                return Verdict(True, "always")
        decision = self.approver(request)
        if not decision.allow:
            return Verdict(False, "denied", decision.reason)
        if decision.remember != "once":
            key = request.tool if decision.whole_tool or request.scope is None else f"{request.tool}:{request.scope}"
            if decision.remember == "session":
                self.session.add(key)
            else:
                self.always.add(key)
                self._save()
        return Verdict(True, decision.remember)

    def revoke(self, rule: str) -> bool:
        found = rule in self.session or rule in self.always
        self.session.discard(rule)
        if rule in self.always:
            self.always.discard(rule)
            self._save()
        return found

    def reset_session(self) -> None:
        self.session.clear()

    def _load(self) -> set[str]:
        if self.store is None or not self.store.exists():
            return set()
        try:
            return set(json.loads(self.store.read_text())["allow"])
        except (ValueError, KeyError, TypeError):
            return set()

    def _save(self) -> None:
        if self.store is not None:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps({"allow": sorted(self.always)}, indent=2) + "\n")
