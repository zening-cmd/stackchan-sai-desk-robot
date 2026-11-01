"""Start the stackchan-mcp gateway, wait for the robot, and exercise a few tools.

Usage (with the gateway tool's own Python):
    python gateway_check.py                 # list tools + device status only
    python gateway_check.py --act           # also nod, light the LEDs and speak
    python gateway_check.py --set-url ws://<PC LAN IP>:8765/   # pin the gateway address on the robot
    python gateway_check.py --set-token     # save the ../.env token on the robot (applies on next reconnect)
    python gateway_check.py --use-token     # run the gateway requiring that token
"""
import argparse
import asyncio
import json
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GATEWAY_ENV = {
    "VISION_HOST": os.environ.get("SAI_PC_IP", ""),  # this PC's LAN IP, for take_photo uploads
    "STACKCHAN_TTS_ENGINE": "edge-tts",
    "STACKCHAN_EDGE_TTS_DEFAULT_VOICE": "en-US-AnaNeural",
    "STACKCHAN_LISTEN_LANGUAGE": "en",
    "STACKCHAN_FASTER_WHISPER_MODEL": "small.en",
}
# Touch events (--touch) use the notify config the brain writes on its first start.
NOTIFY_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "notify.yml")
if os.path.exists(NOTIFY_CONFIG):
    GATEWAY_ENV["STACKCHAN_NOTIFY_CONFIG"] = NOTIFY_CONFIG


def load_token() -> str:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            if line.startswith("STACKCHAN_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("STACKCHAN_TOKEN missing from .env")


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


async def main(act: bool, set_url: str | None, volume: int | None, set_token: bool, use_token: bool,
               touch_test: bool, listen_test: bool, say_text: str | None) -> None:
    env = {**os.environ, **GATEWAY_ENV}
    if use_token:
        env["STACKCHAN_TOKEN"] = load_token()
    params = StdioServerParameters(command=shutil.which("stackchan-mcp") or "stackchan-mcp", args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(sorted(t.name for t in tools.tools)))

            for attempt in range(75):
                status = text_of(await session.call_tool("get_status", {}))
                if '"connected": true' in status.replace("'", '"').lower() or "connected: true" in status.lower():
                    break
                await asyncio.sleep(2)
            print("status:", status)

            print("device:", text_of(await session.call_tool("get_device_info", {})))
            if set_url:
                print("config set:", text_of(await session.call_tool("gateway_config_set", {"url": set_url})))
                print("config now:", text_of(await session.call_tool("gateway_config_get", {})))
            if set_token:
                print("token set:", text_of(await session.call_tool("gateway_config_set", {"token": load_token()})))
            if volume is not None:
                print("volume:", text_of(await session.call_tool("set_volume", {"volume": volume})))
            if touch_test:
                print("touch on:", text_of(await session.call_tool("set_touch_sensor_enabled", {"enabled": True})))
                print(">>> TAP OR STROKE THE HEAD NOW (20 s)", flush=True)
                await session.call_tool("say", {"text": "Touch test. Tap my head, then stroke it."})
                await asyncio.sleep(20)
            if listen_test:
                print(">>> SAY SOMETHING NOW (6 s)", flush=True)
                await session.call_tool("say", {"text": "Now say something to me."})
                print("heard:", text_of(await session.call_tool(
                    "listen", {"duration_ms": 6000, "language": "en", "motion": "face-only"})))
            if say_text:
                print("say:", text_of(await session.call_tool("say", {"text": say_text}))[:200])
            if not act:
                return
            print("leds:", text_of(await session.call_tool("set_all_leds", {"r": 0, "g": 80, "b": 255})))
            print("face:", text_of(await session.call_tool("set_avatar", {"face": "happy"})))
            print("head:", text_of(await session.call_tool("move_head", {"yaw": 5, "pitch": 30})))
            await asyncio.sleep(1.5)
            print("head:", text_of(await session.call_tool("move_head", {"yaw": 5, "pitch": 45})))
            print("say:", text_of(await session.call_tool("say", {"text": "Hi! I'm Sai. Nice to meet you."})))
            await asyncio.sleep(1)
            print("leds:", text_of(await session.call_tool("clear_leds", {})))
            print("face:", text_of(await session.call_tool("set_avatar", {"face": "idle"})))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--act", action="store_true")
    parser.add_argument("--set-url", help="save this gateway WebSocket URL on the robot")
    parser.add_argument("--volume", type=int, help="speaker volume 0-100")
    parser.add_argument("--set-token", action="store_true", help="save the .env token on the robot")
    parser.add_argument("--use-token", action="store_true", help="run the gateway with the .env token")
    parser.add_argument("--touch", action="store_true", help="enable head touch and wait 20 s for taps")
    parser.add_argument("--listen", action="store_true", help="record 6 s from the robot's mic and transcribe")
    parser.add_argument("--say", help="speak this text")
    args = parser.parse_args()
    asyncio.run(main(args.act, args.set_url, args.volume, args.set_token, args.use_token, args.touch, args.listen,
                     args.say))
    sys.exit(0)
