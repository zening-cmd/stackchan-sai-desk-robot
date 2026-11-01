"""Sai task bookkeeping: one running task, a local queue behind it, approvals, and a watcher that follows events."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sai_client import SaiClient, SaiError

log = logging.getLogger("tasks")

# kind is "approval", "done", "error" or "started"; payload carries the task and the untrusted text.
Notify = Callable[[str, dict], Awaitable[None]]


@dataclass
class Task:
    text: str
    session_id: str = ""
    machine_id: str = ""
    cursor: str | None = None
    steps: list[str] = field(default_factory=list)
    approval: dict | None = None
    aborted: bool = False


class TaskManager:
    def __init__(self, client: SaiClient | None, notify: Notify, unavailable_reason: str = ""):
        self.client = client
        self.notify = notify
        self.unavailable_reason = unavailable_reason
        self.current: Task | None = None
        self.queue: list[str] = []
        self.queue_attachments: dict[str, list[dict]] = {}  # attachments for queued tasks, by task text
        self._watcher: asyncio.Task | None = None

    # --- tools the conversation model calls ---------------------------------------------------

    async def start(self, text: str, attachments: list[dict] | None = None) -> str:
        if not self.client:
            return f"Cannot start work: {self.unavailable_reason}"
        if self.current:
            self.queue.append(text)
            if attachments:
                self.queue_attachments[text] = attachments
            return f"Queued (position {len(self.queue)}) behind the running task: {self.current.text}"
        return await self._launch(text, attachments)

    async def update(self, message: str) -> str:
        """Pass a correction or addition into the RUNNING task (Sai folds it into the current turn)."""
        if not self.client:
            return f"Cannot reach the computer: {self.unavailable_reason}"
        if not self.current:
            return "Nothing is running, so there is nothing to update. To begin work, use start_task."
        if self.current.approval:
            return ("The running task is paused on an approval request; it must be answered (approve, deny or "
                    "choose_option) before anything else reaches it.")
        try:
            result = await self.client.send(message)
        except SaiError as exc:
            return f"Could not pass that on: {exc}"
        if not result.get("queued"):
            # The task finished in the meantime, so the message started a new one; follow it like any task.
            self.current = None
            return await self._adopt(message, result)
        self.current.text += f" (then: {message})"
        return "Passed into the running task."

    def status(self) -> str:
        if not self.client:
            return f"Computer work is unavailable: {self.unavailable_reason}"
        if not self.current:
            parts = ["Nothing is running."]
        else:
            parts = [f"Running: {self.current.text}"]
            if self.current.approval:
                parts.append("BLOCKED waiting on the user's answer to an approval request; nothing progresses until "
                             "they answer.")
            if self.current.steps:
                parts.append("Recent steps: " + " | ".join(self.current.steps[-5:]))
            else:
                parts.append("No steps reported yet.")
        if self.queue:
            parts.append("Waiting in line: " + " | ".join(self.queue))
        return " ".join(parts)

    def pending_approval(self) -> dict | None:
        return self.current.approval if self.current else None

    async def approve(self, whole_task: bool = False) -> str:
        approval = self.pending_approval()
        if not approval:
            return "No approval is pending. To begin work, use start_task."
        if approval.get("isLinkOnly"):
            return "This step can only be finished by the user in the Sai app; it cannot be approved by voice."
        await self.client.approve(approval["approvalId"], "task" if whole_task else "yes")
        self.current.approval = None
        return "Approved; the work continues."

    async def deny(self) -> str:
        approval = self.pending_approval()
        if not approval:
            return "No approval is pending."
        if approval.get("isLinkOnly"):
            return "This step can only be finished or skipped by the user in the Sai app."
        await self.client.approve(approval["approvalId"], "no")
        self.current.approval = None
        return "Declined."

    async def choose(self, values: list[str]) -> str:
        approval = self.pending_approval()
        if not approval:
            return "No choice is pending."
        selections, problem = group_selections(approval, values)
        if problem:
            return problem
        try:
            await self.client.approve(approval["approvalId"], "yes", selections)
        except SaiError as exc:
            return f"The choice was rejected ({exc}); present the options to the user again."
        self.current.approval = None
        return "Choice sent; the work continues."

    async def stop(self, scope: str = "everything") -> str:
        dropped = 0
        if scope != "running":
            dropped = len(self.queue)
            self.queue.clear()
        if not self.current:
            return f"Nothing was running. Dropped {dropped} waiting task(s)."
        self.current.aborted = True
        await self.client.abort(self.current.machine_id)
        return f"Stopped: {self.current.text}. Dropped {dropped} waiting task(s)."

    def cancel_queued(self, words: str | None) -> str:
        if not self.queue:
            return "Nothing is waiting in line."
        if not words:
            count = len(self.queue)
            self.queue.clear()
            return f"Dropped all {count} waiting task(s)."
        matches = [t for t in self.queue if words.lower() in t.lower()]
        if len(matches) != 1:
            return f"Could not tell which waiting task is meant. Waiting: {' | '.join(self.queue)}"
        self.queue.remove(matches[0])
        return f"Dropped: {matches[0]}"

    # --- running and following a task ---------------------------------------------------------

    async def _launch(self, text: str, attachments: list[dict] | None = None) -> str:
        try:
            result = await self.client.send(text, attachments)
        except SaiError as exc:
            return f"Could not start the task: {exc}"
        return await self._adopt(text, result)

    async def _adopt(self, text: str, result: dict) -> str:
        """Start following a task that Sai accepted."""
        self.current = Task(text=text, session_id=result["sessionId"], machine_id=result.get("machineId", ""))
        self._watcher = asyncio.create_task(self._watch(self.current))
        await self.notify("started", {"task": text})
        return "Started."

    async def _watch(self, task: Task) -> None:
        failures = 0
        seen_approvals: set[str] = set()
        while True:
            try:
                data = await self.client.events(task.session_id, task.cursor)
                failures = 0
            except Exception as exc:  # a dropped poll is not a completion: back off and keep following
                failures += 1
                log.warning("event poll failed (%d): %s", failures, exc)
                await asyncio.sleep(min(30, 2 * failures))
                continue
            task.cursor = data.get("cursor", task.cursor)
            errors = []
            for event in data.get("events", []):
                if event.get("type") == "data-progress":
                    line = (event.get("data") or {}).get("text", "").strip()
                    if line:
                        task.steps.append(line[:200])
                elif event.get("type") == "error":
                    errors.append(event.get("errorText") or "unknown error")
            status = data.get("status")
            if status == "needs_approval":
                approval = data.get("approval") or {}
                if approval.get("approvalId") and approval["approvalId"] not in seen_approvals:
                    seen_approvals.add(approval["approvalId"])
                    task.approval = approval
                    await self.notify("approval", {"task": task.text, "approval": approval})
            elif status == "idle":
                await self._finish(task, "done", data.get("text") or "")
                return
            elif status == "error":
                await self._finish(task, "error", "; ".join(errors) or data.get("text") or "the task failed")
                return

    async def _finish(self, task: Task, kind: str, text: str) -> None:
        if self.current is task:
            self.current = None
        if task.aborted:
            log.info("task stopped by the user: %s", task.text)
        else:
            await self.notify(kind, {"task": task.text, "text": text})
        if self.queue:
            next_text = self.queue.pop(0)
            reply = await self._launch(next_text, self.queue_attachments.pop(next_text, None))
            if reply != "Started.":
                await self.notify("error", {"task": next_text, "text": reply})


def group_selections(approval: dict, values: list[str]) -> tuple[list[list[str]] | None, str | None]:
    """Put spoken picks back into one group per question, in order (sai-fi VOICE_FSM section 9).

    A value must exactly equal an offered option value; questions that allow free text take unmatched text."""
    questions = approval.get("questions")
    if not questions:
        questions = [{"options": approval.get("options") or [], "allowOther": approval.get("allowOther", False)}]
    groups: list[list[str]] = [[] for _ in questions]
    for value in values:
        placed = False
        for i, question in enumerate(questions):
            offered = [o.get("value") if isinstance(o, dict) else o for o in question.get("options") or []]
            if value in offered:
                groups[i].append(value)
                placed = True
                break
        if not placed:
            for i, question in enumerate(questions):
                if question.get("allowOther"):
                    groups[i].append(value)
                    placed = True
                    break
        if not placed:
            return None, f"'{value}' is not one of the offered options; present the options to the user again."
    return groups, None
