"""Local fixes to the installed stackchan-mcp 0.20.0 gateway.

1. Speech prebuffer: the gateway sends speech frames at exactly the playback rate with no prebuffer, so any network
   delay drains the device's queue (max ~40 frames = 2.4 s) and the voice stutters. send_pcm_audio now pushes the
   first STACKCHAN_TTS_PREBUFFER_FRAMES frames (default 12 = 0.72 s) immediately, then paces in real time.
2. Cloud transcription format: the openai-whisper engine asks for response_format "verbose_json", which
   gpt-4o-mini-transcribe rejects (400). It now asks for STACKCHAN_OPENAI_STT_FORMAT (default "json") and passes
   STACKCHAN_OPENAI_STT_PROMPT as a vocabulary hint when set.

3. End of speech: listen() always recorded the full duration_ms. It now stops once the user has spoken and then been
   quiet for STACKCHAN_LISTEN_END_SILENCE_MS (default 1200), gives up after STACKCHAN_LISTEN_NO_SPEECH_MS (default
   4000) with no speech, and treats duration_ms as the maximum. Speech = frame RMS above
   max(STACKCHAN_LISTEN_MIN_RMS (default 250), STACKCHAN_LISTEN_FLOOR_FACTOR (default 2) x the quietest level seen). Each capture logs its levels.
   STACKCHAN_LISTEN_VAD=0 restores the fixed window.

4. Capture dump (debugging): when STACKCHAN_LISTEN_DUMP_DIR is set, every listen() capture is also saved there as
   listen-<time>.wav (the newest 10 are kept).

5. Automatic language: listen(language="auto") lets the openai-whisper engine detect the language (the tool schema
   only takes a string, and an empty string falls back to Japanese).

6. Skip the start: every capture began with ~0.4 s of the robot's own chime/servo noise (RMS 8-15k, clipping), which
   the VAD counted as speech and the transcriber turned into nonsense. The first STACKCHAN_LISTEN_SKIP_MS (default
   480) of each capture is now ignored by the VAD and dropped before transcription.

7. VAD result in listen(): the result gains "vad": {"reason": "end of speech"|"no speech"|"max duration",
   "speech_ms": …} so a caller can tell "nobody spoke" from "spoke but transcribed as nothing".

Idempotent. Re-run after reinstalling or upgrading stackchan-mcp:
    python tools/patch_gateway.py
"""
import subprocess
import sys
from pathlib import Path

MARKER = "LOCAL PATCH (stackchan-sai): prebuffer"

OLD_PACING = """            next_send_time = loop.time()
            for frame in opus_frames:"""
NEW_PACING = f"""            # {MARKER}
            next_send_time = loop.time() - _PREBUFFER_FRAMES * frame_period_s
            for frame in opus_frames:"""

OLD_HEADER = "TTS_ENGINE_ENV_VAR = \"STACKCHAN_TTS_ENGINE\""
NEW_HEADER = f"""TTS_ENGINE_ENV_VAR = "STACKCHAN_TTS_ENGINE"

# {MARKER}
import os as _os
_PREBUFFER_FRAMES = int(_os.environ.get("STACKCHAN_TTS_PREBUFFER_FRAMES", "12"))"""


STT_MARKER = "LOCAL PATCH (stackchan-sai): stt format"

OLD_STT = """            "response_format": "verbose_json",
        }
        if language is not None:
            kwargs["language"] = language"""
NEW_STT = f"""            # {STT_MARKER}
            "response_format": os.getenv("STACKCHAN_OPENAI_STT_FORMAT", "json"),
        }}
        if language is not None:
            kwargs["language"] = language
        if os.getenv("STACKCHAN_OPENAI_STT_PROMPT"):
            kwargs["prompt"] = os.getenv("STACKCHAN_OPENAI_STT_PROMPT")"""


VAD_MARKER = "LOCAL PATCH (stackchan-sai): end of speech"

OLD_VAD_WAIT = """                await asyncio.sleep(duration_ms / 1000.0)
            finally:"""
NEW_VAD_WAIT = f"""                # {VAD_MARKER}
                await _wait_for_end_of_speech(duration_ms)
            finally:"""

OLD_VAD_ANCHOR = "async def listen_and_transcribe("
NEW_VAD_ANCHOR = f'''# {VAD_MARKER}
async def _wait_for_end_of_speech(max_ms: int) -> None:
    """Return when the speaker has finished, nobody spoke, or max_ms passed."""
    if os.getenv("STACKCHAN_LISTEN_VAD", "1") == "0":
        await asyncio.sleep(max_ms / 1000.0)
        return
    import array
    import math

    import opuslib

    from .. import audio_stream

    end_silence_ms = int(os.getenv("STACKCHAN_LISTEN_END_SILENCE_MS", "1200"))
    no_speech_ms = int(os.getenv("STACKCHAN_LISTEN_NO_SPEECH_MS", "4000"))
    min_rms = float(os.getenv("STACKCHAN_LISTEN_MIN_RMS", "250"))
    floor_factor = float(os.getenv("STACKCHAN_LISTEN_FLOOR_FACTOR", "2"))
    decoder = opuslib.Decoder(DEVICE_SAMPLE_RATE, 1)
    samples_per_frame = DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000
    loop = asyncio.get_running_loop()
    started = loop.time()
    processed = 0
    floor = None
    peak = 0.0
    speech_ms = silence_ms = 0
    reason = "max duration"
    while True:
        await asyncio.sleep(DEVICE_FRAME_DURATION_MS / 1000.0)
        frames = audio_stream._recording_frames
        while processed < len(frames):
            try:
                pcm = decoder.decode(frames[processed], samples_per_frame)
                samples = array.array("h", pcm)
                rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
            except Exception:
                rms = 0.0
            processed += 1
            floor = rms if floor is None else min(floor, rms)
            peak = max(peak, rms)
            if rms > max(min_rms, floor_factor * floor):
                speech_ms += DEVICE_FRAME_DURATION_MS
                silence_ms = 0
            elif speech_ms:
                silence_ms += DEVICE_FRAME_DURATION_MS
        elapsed_ms = (loop.time() - started) * 1000
        if speech_ms >= 300 and silence_ms >= end_silence_ms:
            reason = "end of speech"
            break
        if not speech_ms and elapsed_ms >= no_speech_ms:
            reason = "no speech"
            break
        if elapsed_ms >= max_ms:
            break
    logger.info(
        "listen VAD: %s after %d ms (speech %d ms, floor %.0f, peak %.0f, threshold %.0f)",
        reason, elapsed_ms, speech_ms, floor or 0, peak, max(min_rms, floor_factor * (floor or 0)),
    )


async def listen_and_transcribe('''


DUMP_MARKER = "LOCAL PATCH (stackchan-sai): capture dump"

OLD_DUMP = """            try:
                pcm = decode_opus_frames(frames)
            except Exception as exc:
                raise RuntimeError(f"Opus decode failed: {exc}") from exc
"""
NEW_DUMP = OLD_DUMP + f"""
            # {DUMP_MARKER}
            if pcm and os.getenv("STACKCHAN_LISTEN_DUMP_DIR"):
                try:
                    import pathlib, time as _time, wave as _wave
                    dump_dir = pathlib.Path(os.environ["STACKCHAN_LISTEN_DUMP_DIR"])
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    with _wave.open(str(dump_dir / f"listen-{{_time.strftime('%Y%m%d-%H%M%S')}}.wav"), "wb") as w:
                        w.setnchannels(1); w.setsampwidth(2); w.setframerate(DEVICE_SAMPLE_RATE); w.writeframes(pcm)
                    for old in sorted(dump_dir.glob("listen-*.wav"))[:-10]:
                        old.unlink()
                except Exception as exc:
                    logger.warning("capture dump failed: %s", exc)
"""


AUTO_MARKER = "LOCAL PATCH (stackchan-sai): auto language"

OLD_AUTO = """        if language is not None:
            kwargs["language"] = language"""
NEW_AUTO = f"""        # {AUTO_MARKER}
        if language is not None and language != "auto":
            kwargs["language"] = language"""


SKIP_MARKER = "LOCAL PATCH (stackchan-sai): skip the start"

OLD_SKIP_TRIM = """            frame_count = len(frames)
"""
NEW_SKIP_TRIM = f"""            # {SKIP_MARKER}
            frames = frames[int(os.getenv("STACKCHAN_LISTEN_SKIP_MS", "480")) // DEVICE_FRAME_DURATION_MS:]
            frame_count = len(frames)
"""
OLD_SKIP_VAD = """            processed += 1
            floor = rms if floor is None else min(floor, rms)"""
NEW_SKIP_VAD = f"""            processed += 1
            # {SKIP_MARKER}
            if processed <= int(os.getenv("STACKCHAN_LISTEN_SKIP_MS", "480")) // DEVICE_FRAME_DURATION_MS:
                continue
            floor = rms if floor is None else min(floor, rms)"""


VADINFO_MARKER = "LOCAL PATCH (stackchan-sai): vad result"

VADINFO_REPLACEMENTS = [
    ("async def _wait_for_end_of_speech(max_ms: int) -> None:\n",
     f"# {VADINFO_MARKER}\n_LAST_VAD: dict = {{}}\n\n\nasync def _wait_for_end_of_speech(max_ms: int) -> None:\n"),
    ('    """Return when the speaker has finished, nobody spoke, or max_ms passed."""\n',
     '    """Return when the speaker has finished, nobody spoke, or max_ms passed."""\n    _LAST_VAD.clear()\n'),
    ('    logger.info(\n        "listen VAD: %s after',
     '    _LAST_VAD.update(reason=reason, speech_ms=speech_ms)\n    logger.info(\n        "listen VAD: %s after'),
    ('        "duration_ms": actual_duration_ms,\n        "frame_count": frame_count,\n',
     '        "duration_ms": actual_duration_ms,\n        "frame_count": frame_count,\n        "vad": dict(_LAST_VAD),\n'),
]


def package_dir() -> Path:
    tool_dir = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, check=True).stdout.strip()
    return Path(tool_dir) / "stackchan-mcp" / "Lib" / "site-packages" / "stackchan_mcp"


def apply(path: Path, marker: str, replacements: list[tuple[str, str]]) -> None:
    source = path.read_text(encoding="utf-8")
    if marker in source:
        print(f"already patched: {path}")
        return
    if any(source.count(old) != 1 for old, _ in replacements):
        sys.exit(f"gateway code changed; patch not applied, check {path} by hand")
    for old, new in replacements:
        source = source.replace(old, new)
    path.write_text(source, encoding="utf-8")
    print(f"patched: {path}")


def main() -> None:
    root = package_dir()
    apply(root / "tts" / "orchestrator.py", MARKER, [(OLD_HEADER, NEW_HEADER), (OLD_PACING, NEW_PACING)])
    apply(root / "stt" / "openai_whisper.py", STT_MARKER, [(OLD_STT, NEW_STT)])
    apply(root / "stt" / "orchestrator.py", VAD_MARKER, [(OLD_VAD_ANCHOR, NEW_VAD_ANCHOR), (OLD_VAD_WAIT, NEW_VAD_WAIT)])
    apply(root / "stt" / "orchestrator.py", DUMP_MARKER, [(OLD_DUMP, NEW_DUMP)])
    apply(root / "stt" / "openai_whisper.py", AUTO_MARKER, [(OLD_AUTO, NEW_AUTO)])
    apply(root / "stt" / "orchestrator.py", SKIP_MARKER, [(OLD_SKIP_TRIM, NEW_SKIP_TRIM), (OLD_SKIP_VAD, NEW_SKIP_VAD)])
    apply(root / "stt" / "orchestrator.py", VADINFO_MARKER, VADINFO_REPLACEMENTS)


if __name__ == "__main__":
    main()
