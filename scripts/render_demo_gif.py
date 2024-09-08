"""Render docs/demo.gif: a paced replay of the real `incidentpilot simulate` output.

    .venv/bin/pip install pillow
    .venv/bin/python scripts/render_demo_gif.py [scenario]

The text is captured by actually running the simulation, not hand-written.
"""
import subprocess
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

scenario = sys.argv[1] if len(sys.argv) > 1 else "bad_deploy"
cmd = f"incidentpilot simulate --scenario {scenario}"
out = subprocess.run([sys.executable, "-m", "incidentpilot.cli", "simulate", "--scenario", scenario],
                     capture_output=True, text=True, check=True).stdout
COLS, ROWS, W, H, PAD = 104, 27, 1000, 560, 14
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 15)
except OSError:
    font = ImageFont.load_default()
lines = []
for raw in out.splitlines():
    lines += textwrap.wrap(raw, COLS, replace_whitespace=False, drop_whitespace=False) or [""]

def color(s):
    if s.startswith("#"):
        return (255, 123, 114)
    if s.startswith("**Root cause") or s.startswith("##"):
        return (121, 192, 255)
    if s.lstrip().startswith("- **[E") or s.startswith("injected"):
        return (126, 231, 135)
    return (201, 209, 217)

def frame(shown, typed=None, cursor=False):
    img = Image.new("RGB", (W, H), (13, 17, 23))
    d = ImageDraw.Draw(img)
    rows = ([("$ " + typed + ("_" if cursor else ""), (240, 246, 252))] if typed is not None else [])
    rows += [(s, color(s)) for s in shown]
    for i, (s, c) in enumerate(rows[-ROWS:]):
        d.text((PAD, PAD + i * 19), s, font=font, fill=c)
    return img.convert("P", palette=Image.ADAPTIVE, colors=32)

frames, durs = [], []
for n in range(0, len(cmd) + 1, 2):
    frames.append(frame([], cmd[:n], True)); durs.append(70)
frames.append(frame([], cmd, False)); durs.append(1500)
body = []
for s in lines:
    body.append(s)
    frames.append(frame(body, cmd)); durs.append(1500 if s.startswith(("-", "1.", "2.", "3.", "*")) else 900)
durs[-1] = 8000
frames[0].save("docs/demo.gif", save_all=True, append_images=frames[1:], duration=durs, loop=0,
               optimize=True)
print(f"docs/demo.gif: {len(frames)} frames, {sum(durs) / 1000:.0f}s")
