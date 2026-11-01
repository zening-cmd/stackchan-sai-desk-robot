"""The robot's body: talks to the stackchan-mcp gateway over MCP and plays Sai's reactions."""
import asyncio
import contextlib
import json
import logging
import math
import os
import random
import re
import shutil
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import PROJECT_DIR, RUNTIME_DIR, Config

log = logging.getLogger("robot")

TOUCH_EVENTS = RUNTIME_DIR / "touch-events.jsonl"
LED_COUNT = 12
CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")
CHINESE_VOICE = "zh-CN-XiaoyiNeural"
# Sai's face animation: the custom set stores 5 frames per face in the "mouth" slots; per-face frame time (ms)
# comes from face/build_sai_face.py, matching each pose's wing-flap pace on the cast page.
ANIM_FRAMES = ["closed", "half", "open", "e", "u"]
FACE_TIMING_FILE = PROJECT_DIR / "face" / "sai_face_timing.json"
FACE_TIMING: dict[str, int] = json.loads(FACE_TIMING_FILE.read_text()) if FACE_TIMING_FILE.exists() else {}
TALKING_STEP_MS = 110  # flap faster while speaking
MAX_SEQUENCE_STEPS = 255


def _text(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


class Robot:
    def __init__(self, config: Config):
        self.config = config
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._mood_task: asyncio.Task | None = None
        self.speech_lock = asyncio.Lock()
        self.custom_face_loaded = False
        self._face = "idle"
        self._talking = False
        self._anim_task: asyncio.Task | None = None
        self.listening = False  # True while a listen() capture is running
        self.moving_until = 0.0  # loop time until which Sai is moving its own head (touches then are unreliable)

    async def start(self) -> None:
        env = {
            **os.environ,
            "STACKCHAN_TOKEN": self.config.stackchan_token,
            "VISION_HOST": self.config.pc_ip,
            "STACKCHAN_TTS_ENGINE": "edge-tts",
            "STACKCHAN_EDGE_TTS_DEFAULT_VOICE": self.config.voice,
            "STACKCHAN_LISTEN_LANGUAGE": "en",
            "STACKCHAN_FASTER_WHISPER_MODEL": "small.en",
            # Cloud speech-to-text through OpenRouter's OpenAI-compatible transcription endpoint.
            "OPENAI_API_KEY": self.config.openrouter_key,
            "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
            "STACKCHAN_OPENAI_WHISPER_MODEL": self.config.stt_model,
            "STACKCHAN_NOTIFY_CONFIG": str(write_notify_config()),
        }
        if self.config.save_captures:
            env["STACKCHAN_LISTEN_DUMP_DIR"] = str(RUNTIME_DIR / "captures")
        gateway = shutil.which("stackchan-mcp") or str(Path.home() / ".local" / "bin" / "stackchan-mcp.exe")
        params = StdioServerParameters(command=gateway, args=[], env=env)
        errlog = open(RUNTIME_DIR / "gateway.log", "a", encoding="utf-8")
        self._stack.callback(errlog.close)
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def wait_connected(self, timeout_s: float = 180) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            status = json.loads(await self.call("get_status"))
            if status.get("connected") and status.get("initialized"):
                await asyncio.sleep(1)  # let the device finish its own startup after the handshake
                return
            await asyncio.sleep(2)
        raise TimeoutError("the robot did not connect to the gateway")

    async def close(self) -> None:
        if self._anim_task:
            self._anim_task.cancel()
        await self.stop_mood()
        await self._stack.aclose()

    async def call(self, tool: str, **args) -> str:
        result = await self._session.call_tool(tool, args)
        text = _text(result)
        if '"error"' in text[:40]:
            log.warning("%s %s -> %s", tool, args, text[:200])
        return text

    # --- basic actions -------------------------------------------------------------------------

    async def say(self, text: str) -> None:
        if not text.strip():
            return
        async with self.speech_lock:
            log.info("SAY: %s", text)
            await self._set_talking(True)
            try:
                if CJK.search(text):
                    await self.call("say", text=text, speaker_name=CHINESE_VOICE)
                else:
                    await self.call("say", text=text)
            finally:
                await self._set_talking(False)

    async def listen(self) -> tuple[str, bool]:
        """Record one utterance. Returns (text, spoke): spoke is False when the gateway heard no speech at all."""
        # No listen "motion": that would set a static face and stop the animation; react_listening sets it instead.
        self.listening = True
        try:
            raw = await self.call("listen", duration_ms=self.config.listen_ms, language="en", engine="openai-whisper")
        finally:
            self.listening = False
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {}
        heard = (result.get("text") or "").strip()
        spoke = (result.get("vad") or {}).get("reason") != "no speech" if result.get("vad") else bool(heard)
        log.info("HEARD: %r (spoke=%s, vad=%s)", heard, spoke, result.get("vad"))
        if CJK.search(heard):
            # The user speaks English; non-Latin script means the transcriber misheard.
            log.info("ignoring misheard non-English transcript")
            return "", spoke
        return heard, spoke

    async def take_photo(self, question: str) -> Path | None:
        """Look at the user (the camera is above the screen), flash the lights like a camera flash, take a JPEG.
        Returns the saved file on this PC, or None if the capture failed."""
        await self.stop_mood()
        await self.head(0, 50)
        await self.leds(255, 255, 255)
        raw = await self.call("take_photo", question=question)
        await self.leds_off()
        try:
            image = Path(json.loads(raw.split("\n", 1)[0]).get("image_path", ""))
        except (json.JSONDecodeError, AttributeError):
            match = re.search(r'"image_path"\s*:\s*"([^"]+)"', raw)
            image = Path(match.group(1).encode().decode("unicode_escape")) if match else Path("")
        if not image.is_file():
            log.warning("photo failed: %s", raw[:200])
            return None
        log.info("photo taken: %s", image)
        return image

    async def load_face_set(self, path: Path) -> bool:
        """Load Sai's custom face (90-frame matrix set). The robot keeps it in RAM only, so call this after
        every (re)connect. Takes ~1-3 minutes; speech keeps working meanwhile."""
        if not path.exists():
            log.warning("face set %s not found; keeping the built-in faces", path)
            return False
        log.info("loading face set %s", path.name)
        result = await self.call("load_avatar_set", archive_path=str(path), mode="matrix", timeout=300)
        ok = '"ok": true' in result.replace("'", '"').lower() or '"ok":true' in result.lower()
        log.info("face set load %s: %s", "ok" if ok else "FAILED", result[:200])
        self.custom_face_loaded = ok
        if ok:
            self._restart_animation()
        return ok

    async def face(self, name: str) -> None:
        """Show a face and keep it animated (the custom set's 5 frames per face, looped at the pose's pace)."""
        self._face = name
        await self.call("set_avatar", face=name)
        self._restart_animation()

    def _restart_animation(self) -> None:
        if self._anim_task:
            self._anim_task.cancel()
        if self.custom_face_loaded and self._face in FACE_TIMING:
            self._anim_task = asyncio.create_task(self._animate(self._face))

    async def _animate(self, name: str) -> None:
        """Loop the face's frames with set_mouth_sequence (played on the robot, so it stays smooth); flap faster
        while talking. Each sequence covers ~20 s and is re-sent before it runs out."""
        try:
            while True:
                step = FACE_TIMING[name]
                if self._talking:
                    step = min(step, TALKING_STEP_MS)
                cycles = max(1, min(MAX_SEQUENCE_STEPS // len(ANIM_FRAMES), 20_000 // (step * len(ANIM_FRAMES))))
                steps = [{"shape": shape, "duration_ms": step} for _ in range(cycles) for shape in ANIM_FRAMES]
                await self.call("set_mouth_sequence", steps=steps)
                await asyncio.sleep(len(steps) * step / 1000 - 0.3)
        except asyncio.CancelledError:
            pass

    async def _set_talking(self, talking: bool) -> None:
        if self._talking != talking:
            self._talking = talking
            self._restart_animation()

    async def head(self, yaw: int, pitch: int, speed: str | None = None) -> None:
        """Move the head in true degrees; applies this robot's calibration. speed: "low" (~30 deg/s, deliberate),
        "mid" (~120, natural), "high" (~240, a jolt); None is the gateway default."""
        pitch = max(5, min(self.config.pitch_max, pitch + self.config.pitch_offset))
        args = {"yaw": yaw + self.config.yaw_offset, "pitch": pitch}
        if speed:
            args["speed"] = speed
        await self.call("move_head", **args)

    async def leds(self, r: int, g: int, b: int) -> None:
        await self.call("set_all_leds", r=r, g=g, b=b)

    async def leds_off(self) -> None:
        await self.call("clear_leds")

    # --- moods: looping animations that run until replaced ------------------------------------

    async def stop_mood(self) -> None:
        if self._mood_task:
            self._mood_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._mood_task
            self._mood_task = None

    async def mood_working(self) -> None:
        """Thinking face, slow blue breathing LEDs, an occasional curious head tilt."""
        await self.stop_mood()
        await self.face("thinking")
        self._mood_task = asyncio.create_task(self._working_loop())

    async def _working_loop(self) -> None:
        step = 0
        while True:
            level = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(step / 6))
            await self.leds(0, int(60 * level), int(255 * level))
            if step % 60 == 30:
                await self.head(random.choice((-12, 12)), 50)
            elif step % 60 == 0 and step:
                await self.head(0, 45)
            step += 1
            await asyncio.sleep(0.25)

    async def idle(self) -> None:
        """Animated idle face; the head stays still (the user doesn't want the robot moving on its own)."""
        await self.stop_mood()
        await self.leds_off()
        await self.face("idle")
        await self.head(0, 45)

    # --- one-shot reactions --------------------------------------------------------------------

    async def react_listening(self) -> None:
        """Look up at the user; green lights mean "talk now" (set last, right before recording starts)."""
        await self.stop_mood()
        await self.face("thinking")
        await self.head(0, 55)
        await self.leds(0, 255, 70)

    async def react_done(self) -> None:
        """A task finished: the happy performance."""
        await self.perform("happy")

    async def react_attention(self) -> None:
        """Something needs the user: the surprised performance, then stay looking up at them in amber."""
        await self.perform("surprised")
        await self.leds(255, 140, 0)

    async def react_error(self) -> None:
        await self.perform("sad")

    async def dance(self) -> None:
        """About six seconds of head sways and bounces with rainbow lights."""
        await self.stop_mood()
        await self.face("happy")
        moves = [(-25, 40), (25, 40), (-25, 55), (25, 55), (0, 35), (0, 60), (-30, 45), (30, 45),
                 (-15, 50), (15, 50), (0, 40), (0, 55), (-25, 45), (25, 45), (0, 45)]
        self._moving_for(len(moves) * 0.45)
        for i, (yaw, pitch) in enumerate(moves):
            hue = (i % LED_COUNT) / LED_COUNT
            await asyncio.gather(
                self.head(yaw, pitch),
                self.call("set_leds", colors=[_hsv((hue + n / LED_COUNT) % 1) for n in range(LED_COUNT)]),
            )
            await asyncio.sleep(0.4)
        await self.leds_off()

    async def express(self, feeling: str) -> None:
        """Show a feeling as a short performance (face, choreographed head movement, animated lights)."""
        await self.perform(feeling if feeling in PERFORMANCES else "happy")

    async def perform(self, feeling: str) -> None:
        """Play one of PERFORMANCES: the face, the head moves (each held for its time) and the light show in step."""
        face, moves, lights = PERFORMANCES[feeling]
        await self.stop_mood()
        await self.face(face)
        duration = sum(hold for *_, hold in moves)
        self._moving_for(duration)
        await asyncio.gather(self._move(moves), self._light_show(lights, duration))

    def _moving_for(self, seconds: float) -> None:
        self.moving_until = asyncio.get_running_loop().time() + seconds + 0.5

    async def _move(self, moves) -> None:
        for yaw, pitch, speed, hold in moves:
            if yaw is not None:
                await self.head(yaw, pitch, speed)
            await asyncio.sleep(hold)

    async def _light_show(self, frame, duration: float, fps: int = 15) -> None:
        """frame(t, duration) returns the 12 LED colours at t seconds."""
        started = asyncio.get_running_loop().time()
        while (t := asyncio.get_running_loop().time() - started) < duration:
            await self.call("set_leds", colors=frame(t, duration))
            await asyncio.sleep(1 / fps)

    async def gesture(self, kind: str) -> None:
        await self.stop_mood()
        if kind == "nod":
            steps = [(0, 35), (0, 55), (0, 35), (0, 55), (0, 45)]
        else:
            steps = [(-20, 45), (20, 45), (-20, 45), (20, 45), (0, 45)]
        self._moving_for(len(steps) * 0.3)
        for yaw, pitch in steps:
            await self.head(yaw, pitch)
            await asyncio.sleep(0.3)

    async def react_petted(self) -> None:
        await self.perform("shy")


def _hsv(h: float) -> list[int]:
    i = int(h * 6)
    f = h * 6 - i
    q, t = int(255 * (1 - f)), int(255 * f)
    return [[255, t, 0], [q, 255, 0], [0, 255, t], [0, q, 255], [t, 0, 255], [255, 0, q]][i % 6]


def write_notify_config() -> Path:
    """Gateway notify config: append head-touch events to TOUCH_EVENTS as JSON lines, which touch_events() tails."""
    path = RUNTIME_DIR / "notify.yml"
    path.write_text("legacy_event:\n  enabled: false\nchannels:\n  enabled: false\njsonl:\n  enabled: true\n"
                    f"  path: {TOUCH_EVENTS.as_posix()}\n", encoding="utf-8")
    return path


async def touch_events(path: Path = TOUCH_EVENTS):
    """Yield touch events appended by the gateway after this call starts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _scale(colour, level: float) -> list[int]:
    return [max(0, min(255, round(c * level))) for c in colour]


def lights_rainbow_spin(t: float, duration: float) -> list[list[int]]:
    """A rainbow spinning round the ring, ending in white twinkles."""
    if t > duration - 0.6:
        return [[255, 255, 255] if random.random() < 0.3 else [40, 30, 60] for _ in range(LED_COUNT)]
    return [_hsv((n / LED_COUNT + t * 1.5) % 1) for n in range(LED_COUNT)]


def lights_heartbeat(t: float, duration: float) -> list[list[int]]:
    """Soft pink heartbeat: two quick pulses, then a pause."""
    phase = t % 1.2
    level = 1.0 if phase < 0.12 or 0.26 < phase < 0.38 else 0.18
    return [_scale((255, 70, 150), level)] * LED_COUNT


def lights_tears(t: float, duration: float) -> list[list[int]]:
    """Fill blue, then the lights go out one by one like falling tears, leaving a dim glow."""
    if t < 0.4:
        return [[30, 60, 255]] * LED_COUNT
    out = int((t - 0.4) / 0.25)
    return [[6, 10, 40] if n < out else [30, 60, 255] for n in range(LED_COUNT)]


def lights_surprise(t: float, duration: float) -> list[list[int]]:
    """White flash, fading to amber, then amber blinks."""
    if t < 0.15:
        return [[255, 255, 255]] * LED_COUNT
    if t < 0.6:
        k = (t - 0.15) / 0.45
        return [[255, round(255 - 115 * k), round(255 - 255 * k)]] * LED_COUNT
    return [[255, 140, 0] if int(t * 4) % 2 == 0 else [60, 30, 0]] * LED_COUNT


def lights_orbit(t: float, duration: float) -> list[list[int]]:
    """A blue dot orbiting slowly round the ring with a fading trail."""
    head = (t * 6) % LED_COUNT
    colours = []
    for n in range(LED_COUNT):
        behind = (head - n) % LED_COUNT
        colours.append(_scale((40, 110, 255), max(0.05, 1 - behind / 4)))
    return colours


# feeling -> (face, [(yaw, pitch, speed, hold seconds)], light show). yaw None = hold the current pose.
PERFORMANCES = {
    "happy": ("happy", [  # bouncy hops, then a happy side-to-side wiggle
        (0, 35, "high", 0.2), (0, 58, "high", 0.2), (0, 35, "high", 0.2), (0, 58, "high", 0.2),
        (0, 38, "high", 0.2), (0, 55, "high", 0.25),
        (-22, 50, "mid", 0.3), (22, 50, "mid", 0.3), (-22, 50, "mid", 0.3), (22, 50, "mid", 0.3),
        (0, 48, "mid", 0.4)], lights_rainbow_spin),
    "shy": ("embarrassed", [  # turn away and look down, peek back, hide again quickly, slowly come back
        (-35, 25, "low", 1.3), (None, None, None, 0.5), (-12, 40, "mid", 0.5), (None, None, None, 0.4),
        (-38, 22, "high", 0.8), (0, 42, "low", 1.4)], lights_heartbeat),
    "sad": ("sad", [  # slow droop, a tired little head-shake sigh, slowly lift back up
        (0, 18, "low", 1.3), (-8, 16, "low", 0.7), (8, 16, "low", 0.7), (0, 16, "low", 0.8),
        (0, 42, "low", 1.3)], lights_tears),
    "surprised": ("surprised", [  # jolt back, freeze, double-take left-right
        (0, 72, "high", 0.35), (None, None, None, 0.4), (-28, 65, "high", 0.3), (28, 65, "high", 0.3),
        (0, 62, "mid", 0.9)], lights_surprise),
    "thinking": ("thinking", [  # slow tilt up to the side, ponder, small second tilt
        (18, 60, "low", 1.1), (None, None, None, 0.6), (-10, 58, "low", 1.0), (0, 55, "low", 0.6)], lights_orbit),
}
