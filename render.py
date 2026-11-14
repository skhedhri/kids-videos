"""
Tiny render service used by n8n.

POST /render  {"title": str, "voice": str, "scenes": [{"narration": str, "image_prompt": str}]}
  -> returns an MP4 (video/mp4)

Per scene: Edge TTS narration (free) + AI image from Pollinations (free, no key)
+ slow zoom effect, then all scenes are concatenated. Optional background music
is mixed in from any .mp3 files found in /music.
"""
import base64
import glob
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SHORTS = os.environ.get("VIDEO_FORMAT", "shorts").lower() == "shorts"
W, H = (720, 1280) if SHORTS else (1280, 720)   # vertical 9:16 for Shorts
FPS = 30
VOICE_RATE = os.environ.get("VOICE_RATE", "+0%")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
MAX_SCENES = 20
MAX_NARRATION = 700


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ])
    return float(out.strip())


POLLINATIONS_KEY = os.environ.get("POLLINATIONS_API_KEY", "").strip()
_dead_endpoints = set()  # endpoints that answered 401/402/403: skipped for the rest of the run


CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()


def fetch_cloudflare(prompt, dest, seed):
    """FLUX.1 [schnell] on Cloudflare Workers AI (free daily allowance). Returns True on success."""
    if not (CF_ACCOUNT and CF_TOKEN) or "cloudflare" in _dead_endpoints:
        return False
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    body = json.dumps({"prompt": prompt[:2000], "steps": 6, "seed": seed}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            img = base64.b64decode(data["result"]["image"])
            if len(img) > 5000:
                with open(dest, "wb") as f:
                    f.write(img)
                return True
        except urllib.error.HTTPError as e:
            print(f"image [cloudflare] attempt {attempt + 1} failed: HTTP {e.code}", flush=True)
            if e.code in (401, 403):
                _dead_endpoints.add("cloudflare")  # wrong token / no permission
                return False
            if e.code == 429:
                time.sleep(15)
        except Exception as e:
            print(f"image [cloudflare] attempt {attempt + 1} failed: {e}", flush=True)
        time.sleep(4 * (attempt + 1))
    return False


def fetch_image(prompt, dest, seed):
    """Cloudflare FLUX first, then Pollinations as a backup. Returns True on success."""
    if fetch_cloudflare(prompt, dest, seed):
        return True
    q = urllib.parse.quote(prompt)
    params = f"width={W}&height={H}&seed={seed}&nologo=true&private=true"
    endpoints = []
    if POLLINATIONS_KEY:
        endpoints.append(("keyed", f"https://gen.pollinations.ai/image/{q}?model=flux&{params}",
                          {"Authorization": f"Bearer {POLLINATIONS_KEY}"}))
    endpoints.append(("keyless", f"https://gen.pollinations.ai/image/{q}?model=flux&{params}", {}))
    endpoints.append(("legacy", f"https://image.pollinations.ai/prompt/{q}?{params}", {}))

    for name, url, extra in endpoints:
        if name in _dead_endpoints:
            continue
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **extra})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                if len(data) > 5000:
                    with open(dest, "wb") as f:
                        f.write(data)
                    return True
            except urllib.error.HTTPError as e:
                print(f"image [{name}] attempt {attempt + 1} failed: HTTP {e.code}", flush=True)
                if e.code in (401, 402, 403):
                    _dead_endpoints.add(name)  # needs a key / no credit: do not retry
                    break
                if e.code == 429:
                    time.sleep(15)
            except Exception as e:
                print(f"image [{name}] attempt {attempt + 1} failed: {e}", flush=True)
            time.sleep(4 * (attempt + 1))
    return False


def fallback_image(dest):
    """Plain pastel background if the image service is down."""
    color = random.choice(["0x8ecae6", "0xffb703", "0xb5e48c", "0xffafcc", "0xcdb4db"])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}",
         "-frames:v", "1", dest])


def tts(text, voice, dest, workdir, idx):
    txt = os.path.join(workdir, f"narration_{idx}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    last = None
    for attempt in range(3):
        try:
            run(["edge-tts", "--voice", voice, "--file", txt,
                 f"--rate={VOICE_RATE}", "--write-media", dest])
            return
        except subprocess.CalledProcessError as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"TTS failed: {last.stderr.decode(errors='ignore')[:300] if last else ''}")


TRANSITION = 0.3   # seconds of cross-dissolve between scenes
PAD = 0.8          # pause after each narration (must be >= 2 * TRANSITION + 0.1)

# Camera moves: z = zoom factor, x/y = crop position. F = frames, on = frame number.
MOVES = [
    ("zoom in",   "1+0.12*on/{F}",    "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("pan right", "1.12",             "(iw-iw/zoom)*on/{F}",     "ih/2-(ih/zoom/2)"),
    ("zoom out",  "1.12-0.12*on/{F}", "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("pan left",  "1.12",             "(iw-iw/zoom)*(1-on/{F})", "ih/2-(ih/zoom/2)"),
]


def make_clip(img, out, dur, move_idx):
    """Silent video clip of one image with a slow camera move."""
    frames = int(dur * FPS) + 1
    _, z, x, y = MOVES[move_idx % len(MOVES)]
    z, x, y = (t.replace("{F}", str(frames)) for t in (z, x, y))
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
        f"crop={W * 2}:{H * 2},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS), out,
    ])


def assemble(clips, audios, out):
    """Cross-dissolve the clips and place each narration when its scene is fully visible."""
    n = len(clips)
    durs = [duration(c) for c in clips]
    inputs, filters = [], []
    for c in clips:
        inputs += ["-i", c]
    for a in audios:
        inputs += ["-i", a]

    # video: chain of xfade transitions
    if n == 1:
        vlabel = "0:v"
        total = durs[0]
    else:
        prev = "0:v"
        acc = durs[0]
        for k in range(1, n):
            offset = acc - TRANSITION
            lab = f"v{k}"
            filters.append(
                f"[{prev}][{k}:v]xfade=transition=fade:duration={TRANSITION}:offset={offset:.3f}[{lab}]")
            prev = lab
            acc = offset + durs[k]
        vlabel, total = prev, acc

    # audio: each narration starts once its scene is fully visible
    scene_start = 0.0
    labels = []
    for i in range(n):
        if i > 0:
            scene_start += durs[i - 1] - TRANSITION
        delay_ms = int((scene_start + (TRANSITION if i > 0 else 0.3)) * 1000)
        lab = f"s{i}"
        filters.append(
            f"[{n + i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[{lab}]")
        labels.append(f"[{lab}]")
    filters.append("".join(labels) + f"amix=inputs={n}:duration=longest:normalize=0[aout]")

    run([
        "ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
        "-map", f"[{vlabel}]" if n > 1 else "0:v", "-map", "[aout]",
        "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", out,
    ])


def render(payload):
    scenes = payload.get("scenes") or []
    voice = payload.get("voice") or "en-US-AnaNeural"
    if not scenes or len(scenes) > MAX_SCENES:
        raise ValueError(f"scenes must contain 1-{MAX_SCENES} items")

    workdir = tempfile.mkdtemp(prefix="render_")
    try:
        clips, audios = [], []
        fallbacks = 0
        move0 = random.randint(0, 3)
        base_seed = random.randint(1, 10_000)
        for i, sc in enumerate(scenes):
            narration = str(sc.get("narration", "")).strip()[:MAX_NARRATION]
            prompt = str(sc.get("image_prompt", "")).strip() or narration
            if not narration:
                raise ValueError(f"scene {i} has no narration")

            audio = os.path.join(workdir, f"a_{i}.mp3")
            img = os.path.join(workdir, f"i_{i}.jpg")
            clip = os.path.join(workdir, f"c_{i}.mp4")

            tts(narration, voice, audio, workdir, i)
            if not fetch_image(prompt, img, base_seed + i):
                fallbacks += 1
                fallback_image(img)
            make_clip(img, clip, duration(audio) + PAD, move0 + i)
            clips.append(clip)
            audios.append(audio)
            print(f"scene {i + 1}/{len(scenes)} done", flush=True)

        if fallbacks > len(scenes) // 2:
            raise RuntimeError(
                f"Image service unavailable ({fallbacks}/{len(scenes)} images failed). "
                "Not uploading a video without pictures. Add a POLLINATIONS_API_KEY secret.")

        joined = os.path.join(workdir, "joined.mp4")
        assemble(clips, audios, joined)

        final = joined
        tracks = glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))
        if tracks:
            final = os.path.join(workdir, "final.mp4")
            run([
                "ffmpeg", "-y", "-i", joined, "-stream_loop", "-1",
                "-i", random.choice(tracks),
                "-filter_complex",
                "[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", final,
            ])

        with open(final, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        if self.path != "/render":
            return self._send(404, b"{}", "application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            video = render(payload)
            self._send(200, video, "video/mp4")
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")


if __name__ == "__main__":
    print("render worker listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
