"""Sai's brain: tap the head to talk; a conversation model answers and hands real jobs to Sai.

Run from this folder:  uv run python main.py
"""
import asyncio
import base64
import json
import logging
import random
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from openai import AsyncOpenAI

import config
from prompt import SYSTEM_PROMPT, TOOLS, fence
from robot import Robot, touch_events
from sai_client import SaiClient, SaiError
from tasks import TaskManager

log = logging.getLogger("brain")

MAX_HISTORY = 40
HISTORY_FILE = config.RUNTIME_DIR / "conversation.json"
FACE_SET = config.PROJECT_DIR / "face" / "sai_face_matrix.rgb565"  # built by face/build_sai_face.py
MAX_TOOL_ROUNDS = 5
MAX_FOLLOW_UPS = 4  # replies in a row without a tap before the conversation pauses
TAP_CONFIRM_S = 0.6  # wait this long after a tap: a second tap or a stroke in this time means a stroke
STROKE_TAIL_S = 1.5  # taps this soon after a stroke are the finger lifting off
DUPLICATE_TAP_S = 3.0  # a tap this soon after one started a conversation is the same tap reported twice
WAITING_TAP_S = 8.0  # a tap made while Sai was busy still counts if Sai frees up within this time
SPEAKER_TAIL_S = 1.0
# Whisper tends to "hear" these in silence.
NOISE_TRANSCRIPTS = {"", "you", "thank you", "thanks for watching", "bye"}
PETTED_LINES = ["Hehe.", "That tickles!", "Aw, thanks.", "Mmm, nice."]


def speakable(text: str) -> str:
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


class Brain:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.robot = Robot(cfg)
        self.llm = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=cfg.openrouter_key)
        self.history: list[dict] = self._load_history()
        self.turn_lock = asyncio.Lock()
        # When a tap arrived while Sai was busy (loop time); -1e9 = none.
        self.tap_waiting_since = -1e9
        self.turn_started_at = -1e9  # when the last tap-started conversation began (loop time)
        self.last_photo: Path | None = None  # the latest camera photo, for attaching to a task
        # A light colour the user asked for; kept until the next interaction.
        self.requested_lights: tuple[int, int, int] | None = None
        self.tasks: TaskManager | None = None
        self.sai: SaiClient | None = None

    # --- startup --------------------------------------------------------------------------------

    async def start(self) -> None:
        await self._setup_sai()
        log.info("starting gateway; waiting for the robot")
        await self.robot.start()
        await self.robot.wait_connected()
        await self.robot.call("set_touch_sensor_enabled", enabled=True)
        await self.robot.idle()
        await self.robot.say("Hi, I'm Sai. Tap my head when you need me.")
        asyncio.create_task(self._guarded(self._load_face()))

    async def _load_face(self) -> None:
        if await self.robot.load_face_set(FACE_SET):
            if not self.turn_lock.locked() and not self.tasks.current:
                await self.robot.face("idle")

    async def _setup_sai(self) -> None:
        reason = ""
        if not self.cfg.sai_key:
            reason = "no Sai API key is configured"
        else:
            client = SaiClient(self.cfg.sai_key, self.cfg.sai_url, self.cfg.sai_machine_id)
            try:
                await client.check()
                if not self.cfg.sai_machine_id:
                    machines = await client.machines()
                    if len(machines) == 1:
                        client.machine_id = machines[0]["machineId"]
                    elif machines:
                        names = ", ".join(f"{m.get('name')} ({m['machineId']})" for m in machines)
                        reason = "no computer is chosen; set SAI_MACHINE_ID in .env"
                        log.warning("several Sai computers: %s", names)
                if not reason:
                    self.sai = client
            except (SaiError, Exception) as exc:
                reason = f"the Sai API key was rejected ({exc})"
            if not self.sai:
                await client.close()
        if reason:
            log.warning("computer work disabled: %s", reason)
        self.tasks = TaskManager(self.sai, self.on_task_event, reason)

    # --- the main loop: react to the head -------------------------------------------------------

    async def run(self) -> None:
        """Handle touches until the gateway stops answering; then return so the keeper restarts us."""
        touches = asyncio.create_task(self._touch_loop())
        try:
            await self._watchdog()
        finally:
            touches.cancel()

    async def _touch_loop(self) -> None:
        """Turn the head pad's raw events into intent.

        The pad confuses the two gestures: a stroke usually arrives as two taps ~0.4 s apart (one per zone crossed)
        and only sometimes as a "stroke"; lifting the finger after a stroke often reports an extra tap; the head
        moving under a resting finger adds touches. So: a tap is acted on only after TAP_CONFIRM_S with nothing
        else; a second tap or a stroke within that time makes it a stroke; taps right after a stroke are dropped;
        touches while Sai is moving its own head are dropped."""
        loop = asyncio.get_running_loop()
        last_stroke = 0.0
        tap_check: asyncio.Task | None = None
        async for event in touch_events():
            kind, now = event.get("subtype"), loop.time()
            if now < self.robot.moving_until:
                log.info("touch %s ignored: Sai is moving its head", kind)
            elif kind == "stroke":
                last_stroke = now
                if tap_check and not tap_check.done():
                    tap_check.cancel()  # the "tap" was the start of this stroke
                self._on_stroke()
            elif kind == "tap":
                if now - last_stroke < STROKE_TAIL_S:
                    log.info("tap ignored: end of a stroke")
                elif tap_check and not tap_check.done():
                    # A second tap within TAP_CONFIRM_S: the finger crossed two zones of the pad, which the pad
                    # reports as two taps far more often than as a stroke. Treat it as a stroke.
                    tap_check.cancel()
                    last_stroke = now
                    log.info("two quick taps -> stroke")
                    self._on_stroke()
                else:
                    tap_check = asyncio.create_task(self._confirm_tap())

    async def _confirm_tap(self) -> None:
        await asyncio.sleep(TAP_CONFIRM_S)
        now = asyncio.get_running_loop().time()
        if self.robot.listening:
            log.info("tap ignored: already listening")
        elif now - self.turn_started_at < DUPLICATE_TAP_S:
            log.info("tap ignored: repeat of the tap that just started a conversation")
        elif self.turn_lock.locked():
            log.info("tap remembered: Sai is busy, will listen when free")
            self.tap_waiting_since = asyncio.get_running_loop().time()
        else:
            asyncio.create_task(self._guarded(self.user_turn()))

    def _on_stroke(self) -> None:
        if self.turn_lock.locked():
            log.info("stroke ignored: busy")
        else:
            asyncio.create_task(self._guarded(self.petted()))

    def _start_waiting_tap(self) -> None:
        """A tap that arrived while Sai was busy: start listening now that Sai is free (if it's still recent)."""
        waited = asyncio.get_running_loop().time() - self.tap_waiting_since
        self.tap_waiting_since = -1e9
        if waited < WAITING_TAP_S and not self.turn_lock.locked():
            log.info("starting the remembered tap")
            asyncio.create_task(self._guarded(self.user_turn()))

    async def _watchdog(self) -> None:
        """Poll the gateway. The robot dropping off Wi-Fi is fine (it reconnects by itself); a dead gateway is not."""
        was_connected = True
        while True:
            await asyncio.sleep(20)
            try:
                status = json.loads(await asyncio.wait_for(self.robot.call("get_status"), timeout=15))
            except Exception as exc:
                log.error("gateway not answering (%s); exiting for a restart", exc)
                return
            connected = bool(status.get("connected") and status.get("initialized"))
            if connected and not was_connected:
                log.info("robot reconnected")
                asyncio.create_task(self._guarded(self._welcome_back()))
            elif not connected and was_connected:
                log.warning("robot disconnected; waiting for it to come back")
            was_connected = connected

    async def _welcome_back(self) -> None:
        async with self.turn_lock:
            await self.robot.idle()
            await self.robot.say("I'm back.")
        await self._load_face()  # the robot forgets its custom face when it restarts

    async def _guarded(self, coro) -> None:
        """Run one interaction; on failure, show it on the robot instead of dying silently. Afterwards, honour a tap
        that arrived while Sai was busy."""
        try:
            await coro
        except Exception:
            log.exception("interaction failed")
            try:
                await self.robot.react_error()
                await self.robot.say("Sorry, something went wrong on my side.")
                await self.settle()
            except Exception:
                log.exception("could not report the failure on the robot")
        self._start_waiting_tap()

    async def petted(self) -> None:
        async with self.turn_lock:
            await self.robot.react_petted()
            await self.robot.say(random.choice(PETTED_LINES))
            await self.settle()

    async def user_turn(self) -> None:
        async with self.turn_lock:
            self.turn_started_at = asyncio.get_running_loop().time()
            self.requested_lights = None
            await self._dialogue(follow_up=False)
            await self._after_dialogue()

    async def _dialogue(self, follow_up: bool) -> None:
        """Listen and answer, then keep listening for follow-ups without another tap.

        The first listen after a tap apologises when nothing usable was heard; a follow-up listen that hears
        nothing just ends the conversation quietly."""
        for _ in range(MAX_FOLLOW_UPS + 1):
            if follow_up:
                await asyncio.sleep(SPEAKER_TAIL_S)  # the speaker still holds the last ~0.7 s of the reply
            await self.robot.react_listening()
            heard, spoke = await self.robot.listen()
            await self.robot.leds(0, 60, 255)  # heard you; thinking
            if not spoke or heard.lower().strip(" .!?,") in NOISE_TRANSCRIPTS:
                if not follow_up:
                    await self.robot.say("Sorry, I didn't catch that.")
                return
            await self.robot.face("thinking")
            await self.converse(f"{self.context()}\n{heard}")
            follow_up = True

    async def _after_dialogue(self) -> None:
        if self.requested_lights and not self.tasks.current:
            # Keep the colour the user asked for until the next interaction.
            await self.robot.stop_mood()
            await self.robot.face("idle")
            await self.robot.leds(*self.requested_lights)
            return
        await self.settle()

    async def settle(self) -> None:
        """Return to the working mood while a task runs, else to idle."""
        if self.tasks.current and not self.tasks.pending_approval():
            await self.robot.mood_working()
        elif not self.tasks.current:
            await self.robot.idle()

    # --- events from Sai's work ------------------------------------------------------------------

    async def on_task_event(self, kind: str, payload: dict) -> None:
        if kind == "started":
            await self.robot.mood_working()
            return
        asyncio.create_task(self._guarded(self._announce(kind, payload)))

    async def _announce(self, kind: str, payload: dict) -> None:
        async with self.turn_lock:
            if kind == "done":
                await self.robot.react_done()
                notice = (f"The task \"{payload['task']}\" finished. Tell the user the result in your own words, "
                          f"briefly.\n{fence('work_result', payload['text'] or '(no text came back)')}")
            elif kind == "approval":
                await self.robot.react_attention()
                notice = ("The task \"" + payload["task"] + "\" paused and needs the user's go-ahead. Ask them once, "
                          "plainly.\n" + fence("approval_request", json.dumps(payload["approval"], ensure_ascii=False)))
            else:
                await self.robot.react_error()
                notice = (f"The task \"{payload['task']}\" failed. Tell the user briefly and honestly.\n"
                          f"{fence('work_result', payload['text'])}")
            await self.converse(f"{self.context()}\n[System notice, not the user speaking] {notice}")
            # The user can answer an approval, or follow up on a result, without tapping.
            self.requested_lights = None
            await self._dialogue(follow_up=True)
            await self._after_dialogue()

    # --- the conversation model ------------------------------------------------------------------

    def context(self) -> str:
        now = datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M %Z")
        return f"<context>Local time: {now}. Work: {self.tasks.status()}</context>"

    async def converse(self, user_content: str) -> None:
        self.history.append({"role": "user", "content": user_content})
        spoken: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self.llm.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "system", "content": [
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]}]
                + self.history,
                tools=TOOLS,
                max_tokens=600,
                extra_body={"reasoning": {"effort": "low"}},
            )
            message = response.choices[0].message
            entry = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                entry["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
            self.history.append(entry)
            if message.content:
                spoken.append(message.content)
            if not message.tool_calls:
                break
            for call in message.tool_calls:
                result = await self.run_tool(call.function.name, call.function.arguments)
                log.info("TOOL %s(%s) -> %s", call.function.name, call.function.arguments, result)
                self.history.append({"role": "tool", "tool_call_id": call.id, "content": result})
        self._trim_history()
        await self.robot.say(speakable(" ".join(spoken)))

    async def run_tool(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "Invalid arguments."
        try:
            if name == "dance":
                await self.robot.dance()
                return "Danced."
            if name == "express":
                await self.robot.express(args.get("feeling", "happy"))
                return "Shown."
            if name == "gesture":
                await self.robot.gesture(args.get("kind", "nod"))
                return "Done."
            if name == "set_lights":
                r, g, b = (max(0, min(255, int(args.get(k, 0)))) for k in ("r", "g", "b"))
                await self.robot.leds(r, g, b)
                self.requested_lights = (r, g, b) if (r or g or b) else None
                return "Lights set."
            if name == "take_photo":
                return await self.look(args.get("question") or "Describe what you see.")
            if name == "start_task":
                attachments = None
                if args.get("attach_photo"):
                    if not self.last_photo:
                        return "No photo has been taken yet; call take_photo first."
                    attachments = [await self.sai.upload(str(self.last_photo))]
                return await self.tasks.start(args["task"], attachments)
            if name == "update_task":
                return await self.tasks.update(args["message"])
            if name == "task_status":
                return self.tasks.status()
            if name == "approve":
                return await self.tasks.approve(bool(args.get("for_whole_task")))
            if name == "deny":
                return await self.tasks.deny()
            if name == "choose_option":
                return await self.tasks.choose(list(args.get("values") or []))
            if name == "stop_task":
                return await self.tasks.stop(args.get("scope", "everything"))
            if name == "cancel_queued":
                return self.tasks.cancel_queued(args.get("task"))
        except SaiError as exc:
            return f"The computer service returned an error: {exc}"
        return f"Unknown tool {name}."

    async def look(self, question: str) -> str:
        """Take a photo with the robot's camera and have the model look at it. The photo is kept so a following
        start_task can attach it."""
        photo = await self.robot.take_photo(question)
        await self.robot.face("thinking")
        if not photo:
            return "The camera didn't return a photo (the capture failed)."
        self.last_photo = photo
        data_url = "data:image/jpeg;base64," + base64.b64encode(photo.read_bytes()).decode()
        response = await self.llm.chat.completions.create(
            model=self.cfg.model,
            max_tokens=300,
            extra_body={"reasoning": {"effort": "low"}},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "This photo was just taken by a small desk robot's camera, which faces the "
                                         "person talking to it. Answer their question about it in one or two plain "
                                         f"sentences, for speaking aloud.\nQuestion: {question}"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
        )
        seen = (response.choices[0].message.content or "").strip()
        log.info("PHOTO %s -> %s", photo.name, seen)
        return f"Photo taken and kept (it can be attached to a task). What it shows: {seen}"

    def _trim_history(self) -> None:
        """Keep the last MAX_HISTORY messages, cutting only at a user message so tool calls stay paired,
        and save them so the conversation survives a restart."""
        if len(self.history) > MAX_HISTORY:
            cut = len(self.history) - MAX_HISTORY
            while cut < len(self.history) and self.history[cut]["role"] != "user":
                cut += 1
            self.history = self.history[cut:]
        HISTORY_FILE.write_text(json.dumps(self.history, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def _load_history() -> list[dict]:
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        # Start at a user message so a saved tool call never arrives without its result.
        while history and history[0].get("role") != "user":
            history.pop(0)
        return history


async def main() -> None:
    config.RUNTIME_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(config.RUNTIME_DIR / "brain.log", maxBytes=5_000_000, backupCount=2,
                                      encoding="utf-8"), logging.StreamHandler()],
    )
    for noisy in ("httpx", "mcp", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    brain = Brain(config.load())
    try:
        await brain.start()
        await brain.run()
    except Exception:
        log.exception("brain stopped")
        raise
    finally:
        try:
            await brain.robot.close()
        except Exception:
            pass
        if brain.sai:
            await brain.sai.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
