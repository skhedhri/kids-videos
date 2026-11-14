"""Daily kids video: Gemini script -> safety check -> render -> YouTube upload."""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

import render  # render.py: edge-tts + Pollinations images + FFmpeg

# Tried in order; if Google retires one (HTTP 404) the next one is used.
# You can override with a GEMINI_MODELS env value, comma-separated.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.1-flash-lite,gemini-3.8-flash,gemini-4-hydrogen"
).split(",") if m.strip()]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
LANGUAGE = os.environ.get("LANGUAGE", "English")
VOICE = os.environ.get("VOICE", "en-US-AnaNeural")
PRIVACY = os.environ.get("PRIVACY", "private")
SCENES = int(os.environ.get("SCENE_COUNT", "6"))
SHORTS = os.environ.get("VIDEO_FORMAT", "shorts").lower() == "shorts"
IMAGE_STYLE = os.environ.get(
    "IMAGE_STYLE",
    "3D animated kids cartoon, soft studio lighting, glossy pastel colors, big friendly eyes, "
    "clean simple background, high quality, sharp")

CATEGORIES = [
    "Animals and their babies", "Colors and shapes", "Counting numbers 1 to 10",
    "Space and planets", "Bedtime story with a gentle ending", "Ocean life",
    "Fruits and vegetables", "Weather and seasons", "Vehicles and machines",
    "Dinosaurs", "Kindness and sharing", "The alphabet", "Farm animals",
    "Insects and bugs", "Healthy habits like brushing teeth", "Musical instruments",
]


def gemini_json(prompt):
    last = None
    for model in list(GEMINI_MODELS):
        low_thinking = True  # keep answers fast; dropped automatically if not accepted
        attempt = 0
        while attempt < 2:
            cfg = {"responseMimeType": "application/json", "temperature": 1}
            if low_thinking:
                cfg["thinkingConfig"] = {"thinkingLevel": "low"}
            body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": cfg}).encode()
            try:
                req = urllib.request.Request(
                    f"{GEMINI_BASE}{model}:generateContent", data=body,
                    headers={"Content-Type": "application/json",
                             "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                return json.loads(text.replace("```json", "").replace("```", "").strip())
            except urllib.error.HTTPError as e:
                last = e
                print(f"Gemini {model} failed: HTTP {e.code}", flush=True)
                if e.code == 400 and low_thinking:
                    low_thinking = False  # retry same model without thinking setting
                    continue
                if e.code in (400, 403, 404):
                    break  # model unavailable for this key: next model
                attempt += 1
                time.sleep(10)
            except Exception as e:
                last = e
                print(f"Gemini {model} failed: {e}", flush=True)
                attempt += 1
                time.sleep(5)
    raise RuntimeError(f"Gemini failed on all models: {last}")


def write_script(category, recent):
    style = (
        "This is a vertical YouTube Short of about 40 seconds. Scene 1 is a curiosity HOOK that grabs "
        "attention in the first 3 seconds (for example a surprising question or 'Did you know...?'). "
        "Every narration is ONE or TWO very short, energetic sentences (maximum 14 words each). "
        "The LAST scene ends with a fun question for the kids and a friendly goodbye like 'See you tomorrow for another fun fact!'. "
        "Never ask kids to follow, subscribe, like, click or visit anything. "
        "Image prompts must describe a VERTICAL portrait composition with the subject centered, bold bright colors."
        if SHORTS else
        "Each narration is 2 or 3 short, simple, cheerful sentences a child can follow."
    )
    return gemini_json(
        f"You write short, safe, educational videos for children aged 3 to 7.\n"
        f"Language of the narration: {LANGUAGE}.\nCategory: {category}.\n"
        f"Choose ONE fresh, specific topic in this category that is NOT in this list: "
        f"{json.dumps(recent)}.\n"
        f"Write exactly {SCENES} scenes. {style}\n"
        f"Rules: nothing scary, violent, romantic, commercial or branded; no real people; "
        f"no existing cartoon characters; no medical advice.\n"
        'Return ONLY valid JSON: {"topic":"","title":"catchy title under 60 characters with one emoji",'
        '"description":"2-3 sentences for parents","tags":["8 to 12 tags"],'
        '"character":"ONE sentence, in English, describing the single cute recurring cartoon character or main '
        'subject that appears in EVERY scene (species, colors, shape, outfit)",'
        '"scenes":[{"narration":"","image_prompt":"English, describe ONLY what happens in this scene: '
        'action, pose and setting, vertical composition, subject centered"}]}'
    )


def is_safe(script):
    verdict = gemini_json(
        "You are a strict child-safety reviewer for videos aimed at ages 3 to 7. "
        "A friendly goodbye such as 'see you tomorrow' is fine and is NOT commercial. "
        "It is UNSAFE if anything is scary, violent, sexual, hateful, political, "
        "commercial, brand-related, medical advice, mentions a real person or an "
        "existing copyrighted character, gives false facts, or is not age-appropriate. "
        'Return ONLY JSON: {"safe":true or false,"reason":"short reason"}.\n\nSCRIPT:\n'
        + json.dumps({"title": script["title"], "scenes": script["scenes"]})
    )
    return verdict.get("safe") is True, verdict.get("reason", "")


def upload(path, script):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    request = yt.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": (script["title"][:88] + (" #Shorts" if SHORTS else "")),
                "description": script["description"] + "\n\nMade with AI for kids."
                               + ("\n\n#Shorts #kids #learning" if SHORTS else ""),
                "tags": (script["tags"][:11] + (["shorts"] if SHORTS else [])),
                "categoryId": "27",
            },
            "status": {
                "privacyStatus": PRIVACY,
                "selfDeclaredMadeForKids": True,
            },
        },
        media_body=MediaFileUpload(path, mimetype="video/mp4", resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def main():
    history = json.load(open("history.json"))
    recent = [h["topic"] for h in history[-80:]]

    for attempt in range(3):
        category = random.choice(CATEGORIES)
        script = write_script(category, recent)
        script["scenes"] = [
            {"narration": str(s["narration"]), "image_prompt": str(s["image_prompt"])}
            for s in script["scenes"]
        ]
        if len(script["scenes"]) < 3:
            continue
        character = str(script.get("character", "")).strip()
        for sc in script["scenes"]:
            sc["image_prompt"] = (f"{sc['image_prompt']}. Main character in every scene: {character}. "
                                  f"Style: {IMAGE_STYLE}. No text, no letters.")
        safe, reason = is_safe(script)
        topic = str(script.get("topic", category))
        if not safe:
            print(f"Rejected '{topic}': {reason}", flush=True)
            history.append({"topic": topic, "status": "rejected", "reason": reason})
            recent.append(topic)
            continue

        print(f"Rendering '{topic}'...", flush=True)
        video = render.render({"voice": VOICE, "scenes": script["scenes"]})
        with open("video.mp4", "wb") as f:
            f.write(video)
        video_id = upload("video.mp4", script)
        print(f"Uploaded: https://youtu.be/{video_id}", flush=True)
        history.append({"topic": topic, "status": "uploaded", "videoId": video_id})
        break
    else:
        print("No safe script after 3 tries; nothing uploaded today.", flush=True)

    json.dump(history[-300:], open("history.json", "w"), indent=2)


if __name__ == "__main__":
    sys.exit(main())
