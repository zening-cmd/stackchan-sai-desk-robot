"""Build Sai's robot face set from the Saibot cast page (the green pixel-art butterfly).

The page draws Sai with CSS pixels (24x18 grid, zoom 4) and two wings that rotate in 3D. For each robot expression
this captures the matching Sai pose at the device's 160x120 frame size:
  - "mouth" closed / half / open / e / u -> 5 real frames spread across one cycle of the pose's own wing flap (with
    its body sway and drifting effects). The brain plays them in a loop with set_mouth_sequence, so Sai keeps moving
    at the page's pace (sai_face_timing.json gives each face's frame time);
  - "eyes" open / half / closed -> the same frames with the wings folded further, used by the firmware's blink.
It writes PNG and animated GIF previews and the stackchan-mcp matrix payload (90 frames, RGB565 little-endian).

Usage (from this folder):
    uv run --no-project --with playwright --with pillow python build_sai_face.py [--light]
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFilter

URL = "https://sai-design-prototype-olive.vercel.app/docs/saibot-cast/cast.html#expressions/sai"
OUT = Path(__file__).resolve().parent / "build"
W, H = 160, 120  # device frame size (shown at 2x on the 320x240 screen)
CAPTURE = 240  # capture a larger square around the pose (jumps, tilts, effects), then fit it
MARGIN = 10  # keep the butterfly this many pixels inside the frame (room for the talking bob)

# Firmware face order (avatar_set.h): idle / happy / thinking / sad / surprised / embarrassed.
# (robot face, Sai pose, start moment in ms after settling, the pose's wing-flap period in ms from the page's motion
# data). The 5 animation frames are spread evenly across one wing period from the start moment.
# "happy" is the dance ("Wing waltz", a 4.8 s routine): its twirl, 2.6-3.6 s, at 200 ms per frame. The user
# chose to replace the sparkle pose with it; happy, dance requests and task-done all show it.
FACES = [("idle", "original", 250, 1260), ("happy", "dance", 2600, 1000), ("thinking", "thinking", 150, 3600),
         ("sad", "error", 250, 1600), ("surprised", "happy", 1500, 585), ("embarrassed", "original", 250, 750)]
# The user's choices: the surprised slot shows the page's "happy" pose (tiny jumps, hearts; also used when Sai needs
# approval), and "embarrassed" is a custom shy Sai made here rather than a page pose: the original butterfly with
# rosy blush spots, a bashful tilt, a quick flutter and pink hearts floating up (see make_shy).
SHY_FACE = "embarrassed"
SHY_WING_FOLD = 0  # extra wing fold for shy (38 folded the wings edge-on and Sai nearly vanished)
# Extra wing fold (degrees) for the blink: open / half / closed.
EYES = [0, 32, 62]
MOUTH_SLOTS = ["closed", "half", "open", "e", "u"]  # firmware names of the 5 animation frames, in order
SETTLE_MS = 3000
DANCE_ROUTINE_MS = 4800  # length of Sai's "Wing waltz" routine on the page
CLOCK_START_MS = 1_800_000_000_000

TRANSPARENT_CSS = """html, body, body * { background-color: transparent !important; background-image: none !important;
    box-shadow: none; border-color: transparent !important; }
    .stage, .stage * { box-shadow: revert-layer; }"""


async def capture(page, pose: str, wing_extra: int) -> Image.Image:
    """Screenshot a CAPTURE-sized square centred on the pose's butterfly with the wings folded by wing_extra degrees."""
    box = await page.evaluate("""([pose, extra]) => {
        const section = document.querySelector(`section.pose[data-pose="${pose}"]`);
        section.scrollIntoView({block: 'center'});  // the page scrolls inside a panel, not the window
        // Show only this pose's stage: hide titles, captions and the small preview butterflies.
        document.body.style.visibility = 'hidden';
        for (const s of document.querySelectorAll('.stage')) s.style.visibility = 'hidden';
        section.querySelector('.stage').style.visibility = 'visible';
        const actor = section.querySelector('div[data-character]');
        for (const wing of section.querySelectorAll('.office-butterfly-wing')) {
            if (wing.dataset.baseTransform === undefined) wing.dataset.baseTransform = wing.style.transform || '';
            const base = wing.dataset.baseTransform;
            const m = /rotateY\\((-?[\\d.]+)deg\\)/.exec(base);
            const angle = m ? parseFloat(m[1]) : 0;
            const sign = wing.classList.contains('office-butterfly-left') ? -1 : 1;
            const folded = sign * Math.min(80, Math.abs(angle) + extra);
            wing.style.transform = base.replace(/rotateY\\([^)]*\\)/, '') + ` rotateY(${folded}deg)`;
        }
        const r = actor.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""", [pose, wing_extra])
    path = OUT / "_shot.png"
    await page.screenshot(path=str(path), omit_background=True,
                          clip={"x": box["x"] - CAPTURE / 2, "y": box["y"] - CAPTURE / 2, "width": CAPTURE, "height": CAPTURE})
    return Image.open(path).convert("RGBA")


async def reset_wings(page, pose: str) -> None:
    await page.evaluate("""(pose) => {
        for (const wing of document.querySelectorAll(`section.pose[data-pose="${pose}"] .office-butterfly-wing`)) {
            delete wing.dataset.baseTransform;
        }
    }""", pose)


def background(light: bool) -> Image.Image:
    """Soft green glow, like the cast page, on a light or a dark screen."""
    base = (245, 247, 243) if light else (10, 22, 16)
    glow = (214, 238, 220) if light else (22, 58, 38)
    bg = Image.new("RGB", (W, H), base)
    halo = Image.new("L", (W, H), 0)
    ImageDraw.Draw(halo).ellipse((W / 2 - 52, H / 2 - 44, W / 2 + 52, H / 2 + 44), fill=255)
    halo = halo.filter(ImageFilter.GaussianBlur(18))
    return Image.composite(Image.new("RGB", (W, H), glow), bg, halo)


def fit_for(sprites: list[Image.Image]) -> tuple[float, int, int]:
    """Scale and offset that centre the butterfly (green pixels; effects ignored) across all the given frames and
    keep it inside the frame in every one of them."""
    boxes = []
    for sprite in sprites:
        mask = Image.new("L", sprite.size, 0)
        mask.putdata([255 if a > 200 and g > r and g > b else 0 for r, g, b, a in sprite.get_flattened_data()])
        if mask.getbbox():
            boxes.append(mask.getbbox())
    if not boxes:
        return 1.0, 0, 0
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    width, height = box[2] - box[0], box[3] - box[1]
    scale = min(1.0, (W - 2 * MARGIN) / width, (H - 2 * MARGIN) / height)
    centre_x, centre_y = (box[0] + box[2]) / 2 * scale, (box[1] + box[3]) / 2 * scale
    return scale, round(W / 2 - centre_x), round(H / 2 + 4 - centre_y)


def compose(sprite: Image.Image, fit: tuple[float, int, int], bob: int, bg: Image.Image) -> Image.Image:
    scale, dx, dy = fit
    if scale < 1.0:
        sprite = sprite.resize((round(sprite.width * scale), round(sprite.height * scale)), Image.NEAREST)
    frame = bg.copy()
    frame.paste(sprite, (dx, dy - bob), sprite)
    return frame


def to_rgb565_le(frame: Image.Image) -> bytes:
    out = bytearray()
    for r, g, b in frame.get_flattened_data():
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out += value.to_bytes(2, "little")
    return bytes(out)


def tilt(sprite: Image.Image) -> float:
    """How far the butterfly is from its normal wide pose (0 = wide, higher = tilted or upright mid-twirl)."""
    mask = Image.new("L", sprite.size, 0)
    mask.putdata([255 if a > 200 and g > r and g > b else 0 for r, g, b, a in sprite.get_flattened_data()])
    box = mask.getbbox()
    if not box:
        return 0.0
    aspect = (box[2] - box[0]) / max(1, box[3] - box[1])
    return max(0.0, 1.4 - aspect)


async def capture_dance_twirl(page, step_ms: float) -> dict:
    """The dance's twirl starts at a different point of its 4.8 s routine on each page load, so sample the whole
    routine and keep the 5 consecutive samples where Sai is most tilted (the twirl)."""
    samples = []
    for k in range(DANCE_ROUTINE_MS // round(step_ms)):
        if k:
            await page.clock.run_for(round(step_ms))
        await reset_wings(page, "dance")
        samples.append(await capture(page, "dance", 0))
    n, w = len(samples), len(MOUTH_SLOTS)
    start = max(range(n), key=lambda i: sum(tilt(samples[(i + j) % n]) for j in range(w)))
    window = [samples[(start + j) % n] for j in range(w)]
    print(f"  dance twirl found at {start * round(step_ms)} ms of the routine")
    # The robot keeps its loop going, so the blink rarely shows; reuse the same frames for the blink slots.
    return {(eye_index, m): window[m] for eye_index in range(len(EYES)) for m in range(w)}


HEART = [".XX.XX.", "XXXXXXX", "XXXXXXX", ".XXXXX.", "..XXX..", "...X..."]
PINK = (255, 110, 170)
DEEP_PINK = (214, 51, 120)


def make_shy(sprite: Image.Image, frame: int) -> Image.Image:
    """Custom shy Sai: blush the butterfly pink, tilt it bashfully, and float two pixel hearts up past it.
    frame (0-4) moves the hearts so the loop shows them rising."""
    sprite = sprite.copy()
    body = sprite.getchannel("A").point(lambda a: 255 if a > 200 else 0).getbbox()
    if body:
        # Two rosy blush spots on the inner wings, either side of the body (Sai stays green).
        cx, cy = (body[0] + body[2]) // 2, (body[1] + body[3]) // 2 - 2
        blush = ImageDraw.Draw(sprite)
        for side in (-1, 1):
            x = cx + side * 13
            blush.ellipse((x - 5, cy - 3, x + 5, cy + 3), fill=PINK + (255,))
            blush.ellipse((x - 3, cy - 2, x + 3, cy + 2), fill=(255, 150, 195, 255))
    sprite = sprite.rotate(-9, resample=Image.NEAREST, center=(sprite.width / 2, sprite.height / 2))
    mask = sprite.getchannel("A").point(lambda a: 255 if a > 200 else 0)
    box = mask.getbbox() or (0, 0, sprite.width, sprite.height)
    draw = ImageDraw.Draw(sprite)
    for heart, (dx, base_y, scale) in enumerate([(box[2] - 4, box[1] + 10, 2), (box[0] - 10, box[1] + 22, 1)]):
        rise = ((frame + heart * 2) % 5) * 5 * scale // 2
        x0, y0 = dx, base_y - rise
        for row, line in enumerate(HEART):
            for col, cell in enumerate(line):
                if cell == "X":
                    colour = DEEP_PINK if row == 0 or col in (0, 6) else PINK
                    draw.rectangle((x0 + col * scale, y0 + row * scale, x0 + col * scale + scale - 1,
                                    y0 + row * scale + scale - 1), fill=colour + (255,))
    return sprite


def save_gif(path: Path, frames: list[Image.Image], step_ms: float) -> None:
    """Animated preview of one face's loop at 2x, as the robot plays it."""
    big = [f.resize((W * 2, H * 2), Image.NEAREST) for f in frames]
    big[0].save(path, save_all=True, append_images=big[1:], duration=round(step_ms), loop=0)


def save_overview_gif(path: Path, frames: dict, timing: dict) -> None:
    """All six faces animating side by side (each at its own pace), 4 seconds."""
    tick = 40
    names = [f[0] for f in FACES]
    out = []
    for t in range(0, 4000, tick):
        sheet = Image.new("RGB", (W * 3, H * 2))
        for i, name in enumerate(names):
            m = int(t // timing[name]) % len(MOUTH_SLOTS)
            sheet.paste(frames[(i, 0, m)], ((i % 3) * W, (i // 3) * H))
        out.append(sheet.resize((W * 6, H * 4), Image.NEAREST))
    out[0].save(path, save_all=True, append_images=out[1:], duration=tick, loop=0)


async def main(light: bool) -> None:
    OUT.mkdir(exist_ok=True)
    bg = background(light)
    frames: dict[tuple[int, int, int], Image.Image] = {}
    timing: dict[str, int] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 1800})
        await page.clock.install(time=datetime.fromtimestamp(CLOCK_START_MS / 1000, tz=timezone.utc))
        for face_index, (face, pose, moment_ms, period_ms) in enumerate(FACES):
            # Load fresh with the page clock frozen, then step it through this pose's animation, so every frame is
            # repeatable and nothing moves while a frame's wing variants are captured.
            # Each face pauses an hour after the previous pause point, which is always still in the future.
            # Freeze the clock BEFORE loading, so the page's animations start at the same frozen instant every time.
            pause_ms = CLOCK_START_MS + (face_index + 1) * 3_600_000
            await page.clock.pause_at(datetime.fromtimestamp(pause_ms / 1000, tz=timezone.utc))
            await page.goto(URL)
            await page.clock.run_for(SETTLE_MS + moment_ms)
            await page.add_style_tag(content=TRANSPARENT_CSS)
            step_ms = period_ms / len(MOUTH_SLOTS)
            if pose == "dance":
                sprites = await capture_dance_twirl(page, step_ms)
            else:
                sprites = {}
                for mouth_index in range(len(MOUTH_SLOTS)):
                    if mouth_index:
                        await page.clock.run_for(round(step_ms))
                    await reset_wings(page, pose)
                    extra = SHY_WING_FOLD if face == SHY_FACE else 0
                    for eye_index, eye_fold in enumerate(EYES):
                        sprites[(eye_index, mouth_index)] = await capture(page, pose, eye_fold + extra)
            # One fit for the whole face, covering where the butterfly goes in every frame (jumps, sways), so the
            # loop stays on screen and nothing shifts between frames.
            fit = fit_for([sprites[(0, m)] for m in range(len(MOUTH_SLOTS))])
            for (eye_index, mouth_index), sprite in sprites.items():
                if face == SHY_FACE:
                    sprite = make_shy(sprite, mouth_index)
                frames[(face_index, eye_index, mouth_index)] = compose(sprite, fit, 0, bg)
            timing[face] = round(step_ms)
            save_gif(OUT / f"{face}.gif", [frames[(face_index, 0, m)] for m in range(len(MOUTH_SLOTS))], step_ms)
            frames[(face_index, 0, 0)].resize((W * 2, H * 2), Image.NEAREST).save(OUT / f"{face}.png")
            print(f"{face:12} <- pose '{pose}', 5 frames x {round(step_ms)} ms")
        await browser.close()
    (OUT.parent / "sai_face_timing.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    save_overview_gif(OUT / "faces_overview.gif", frames, timing)

    payload = bytearray()
    for face_index in range(len(FACES)):
        for eye_index in range(len(EYES)):
            for mouth_index in range(len(MOUTH_SLOTS)):
                payload += to_rgb565_le(frames[(face_index, eye_index, mouth_index)])
    assert len(payload) == 90 * W * H * 2
    (OUT / "sai_face_matrix.rgb565").write_bytes(payload)

    sheet = Image.new("RGB", (W * len(MOUTH_SLOTS) * 2, H * len(EYES) * 2 * len(FACES)), (0, 0, 0))
    for face_index in range(len(FACES)):
        for eye_index in range(len(EYES)):
            for mouth_index in range(len(MOUTH_SLOTS)):
                frame = frames[(face_index, eye_index, mouth_index)].resize((W * 2, H * 2), Image.NEAREST)
                sheet.paste(frame, (mouth_index * W * 2, (face_index * len(EYES) + eye_index) * H * 2))
    sheet.save(OUT / "all_frames.png")
    overview = Image.new("RGB", (W * 2 * 3, H * 2 * 2), (0, 0, 0))
    for face_index in range(len(FACES)):
        overview.paste(frames[(face_index, 0, 0)].resize((W * 2, H * 2), Image.NEAREST),
                       ((face_index % 3) * W * 2, (face_index // 3) * H * 2))
    overview.save(OUT / "faces_overview.png")
    print(f"wrote {OUT / 'sai_face_matrix.rgb565'} ({len(payload)} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--light", action="store_true", help="light screen like the cast page (default: dark)")
    asyncio.run(main(parser.parse_args().light))
