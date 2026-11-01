"""Settings for Sai's brain. Values come from ../.env first, then the environment."""
import os
import socket
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_DIR / "runtime"


def _load_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


_FILE = _load_env_file()


def setting(name: str, default: str | None = None) -> str | None:
    return _FILE.get(name) or os.environ.get(name) or default


def lan_ip() -> str:
    """This PC's address on the local network (the robot uploads photos to it). No packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]


@dataclass(frozen=True)
class Config:
    openrouter_key: str
    model: str
    stt_model: str
    sai_key: str | None
    sai_url: str
    sai_machine_id: str | None
    stackchan_token: str
    pc_ip: str
    voice: str
    # Head calibration: stackchan-mcp assumes servo zero positions 460 / 620; robots differ by a few degrees.
    yaw_offset: int = 0
    pitch_offset: int = 0
    pitch_max: int = 80
    save_captures: bool = False  # keep each recording in runtime/captures (for debugging listening)
    listen_ms: int = 10000  # maximum; the gateway stops earlier once the user stops talking


def load() -> Config:
    openrouter_key = setting("OPENROUTER_API_KEY")
    token = setting("STACKCHAN_TOKEN")
    if not openrouter_key:
        raise SystemExit("OPENROUTER_API_KEY is missing (set it in .env or the environment)")
    if not token:
        raise SystemExit("STACKCHAN_TOKEN is missing from .env")
    return Config(
        openrouter_key=openrouter_key,
        model=setting("SAI_BRAIN_MODEL", "anthropic/claude-haiku-5.5"),
        stt_model=setting("SAI_STT_MODEL", "openai/gpt-4o-mini-transcribe"),
        sai_key=setting("SAI_API_KEY"),
        sai_url=setting("SAI_API_URL", "https://api.simular.ai"),
        sai_machine_id=setting("SAI_MACHINE_ID"),
        stackchan_token=token,
        pc_ip=setting("SAI_PC_IP") or lan_ip(),
        voice=setting("SAI_VOICE", "en-US-AnaNeural"),
        yaw_offset=int(setting("SAI_YAW_OFFSET", "0")),
        pitch_offset=int(setting("SAI_PITCH_OFFSET", "0")),
        pitch_max=int(setting("SAI_PITCH_MAX", "80")),
        save_captures=setting("SAI_SAVE_CAPTURES", "0") == "1",
    )
