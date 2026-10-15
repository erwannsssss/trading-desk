"""One-off: generate AI headshots (all in suits) for everyone at the firm, into docs/faces/<slug>.jpg.

Runs on GitHub Actions (the runner has open internet). Uses Pollinations' free image API (no key).
Existing files are kept, so re-running only fills in missing people. Delete a file to regenerate it.
Usage: python scripts/make_faces.py [--force name-slug ...]
"""
from __future__ import annotations

import io
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "docs" / "faces"
STYLE = ("full color corporate headshot photograph, head and shoulders, dressed in a formal business suit: {suit} "
         "with a suit jacket and lapels clearly visible, plain dark gray studio backdrop, soft studio lighting, "
         "looking at camera, photorealistic, sharp focus, 85mm")

PEOPLE = {
    # leadership
    "Victor Ashford": "distinguished 58-year-old white man, silver gray swept-back hair, rimless glasses, confident slight smile",
    "Ashley Brennan": "friendly 34-year-old white woman, long auburn hair, warm smile",
    "Riley Park": "upbeat 27-year-old Korean American woman, shoulder-length black hair, bright smile",
    # research
    "Elena Castillo": "sharp 41-year-old Latina woman, dark brown hair pulled back, confident expression",
    "Darius Webb": "47-year-old Black man, short buzzed hair, neatly trimmed beard, serious thoughtful look",
    "Maya Chen": "26-year-old Chinese American woman, black hair in a high bun, focused friendly expression",
    "Ben Carter": "24-year-old white man, short light brown hair, clean shaven, eager smile",
    "Priya Natarajan": "31-year-old South Indian woman, long straight black hair, small smile",
    "Marcus Hale": "45-year-old white man, short dark hair graying at the temples, stubble, analytical look",
    "Sofia Reyes": "29-year-old Latina woman, wavy dark brown shoulder-length hair, warm smile",
    "Theo Okafor": "33-year-old Nigerian man, short cropped hair, clean shaven, calm confident look",
    "Jake Morrison": "30-year-old white man, side-parted blond hair, big optimistic grin",
    "Nadia Volkov": "36-year-old Russian woman, straight blonde bob haircut, skeptical serious expression",
    "Sam Brooks": "38-year-old white man, short red hair, glasses, neutral precise expression",
    "Iris Lindqvist": "35-year-old Swedish woman, platinum blonde hair in a low ponytail, calm expression",
    # trading
    "Kenji Watanabe": "40-year-old Japanese man, short spiky black hair, intense focused look",
    "Leo Moretti": "32-year-old Italian man, dark wavy hair, light stubble, relaxed smile",
    "Ava Sinclair": "28-year-old white woman, long dark brown hair, confident smirk",
    "Grace Adeyemi": "44-year-old Nigerian woman, natural hair in a neat updo, composed serious expression",
    "Hugo Brandt": "52-year-old German man, receding gray hair, tortoiseshell glasses, stern expression",
    # learning
    "Omar Haddad": "39-year-old Lebanese man, short dark curly hair, trimmed beard, glasses, thoughtful look",
    "Ruth Klein": "61-year-old white woman, short silver hair, reading glasses, wise slight smile",
    "Nina Petrova": "30-year-old Bulgarian woman, dark hair in a sleek ponytail, intelligent calm look",
    "Diane Foster": "50-year-old white woman, short dark blonde hair, pearl earrings, stern no-nonsense expression",
    # unlockable desks
    "Lena Hoffmann": "33-year-old German woman, long straight light brown hair, sharp focused look",
    "Marisol Vega": "36-year-old Puerto Rican woman, dark curly shoulder-length hair, confident smile",
    "Callum Reid": "40-year-old Scottish man, short sandy hair, light beard, calm expression",
    "Arjun Mehta": "29-year-old Indian man, short black hair, thin glasses, quietly confident smile",
    # interns (younger, a bit less polished)
    "Tyler Brooks": "21-year-old white man, messy brown hair, excited grin, slightly loose tie",
    "Zoe Kim": "20-year-old Korean American woman, black hair with bangs, mischievous smile",
    "Dev Patel": "21-year-old Indian American man, short black hair, playful confident grin",
    "Lucia Romero": "22-year-old Spanish woman, long curly dark hair, studious expression",
    "Hannah Cole": "21-year-old white woman, straight light brown hair, energetic smile",
    "Marco Bianchi": "22-year-old Italian man, slicked-back dark hair, smug smile",
    "Kai Nakamura": "21-year-old Japanese American man, neat short black hair, glasses, serious look",
    "Sienna Clarke": "22-year-old Black British woman, long box braids, skeptical raised eyebrow",
    "Jordan Ellis": "21-year-old mixed-race man, short curly hair, bold daring grin",
    # future hires (used if Ashley fires someone)
    "Carmen Diaz": "37-year-old Mexican woman, dark hair in a side part, confident smile",
    "Owen Fitzgerald": "34-year-old Irish man, short ginger hair, beard, friendly look",
    "Hana Sato": "29-year-old Japanese woman, shoulder-length black hair, poised expression",
    "Rafael Mendes": "42-year-old Brazilian man, short dark hair, light beard, warm smile",
    "Imani Brooks": "31-year-old Black American woman, natural curly hair, bright smile",
    "Lukas Becker": "36-year-old German man, short blond hair, clean shaven, serious look",
    "Amira Haddad": "33-year-old Arab woman, long dark hair, elegant composed expression",
    "Connor Walsh": "28-year-old white American man, short brown hair, athletic build, confident grin",
    "Mei Lin": "34-year-old Taiwanese woman, chin-length black hair, glasses, focused look",
    "Andre Laurent": "45-year-old Black French man, shaved head, goatee, calm expression",
    "Freya Nilsson": "27-year-old Norwegian woman, long light blonde hair, friendly smile",
    "Tariq Aziz": "38-year-old Pakistani man, short black hair, full trimmed beard, thoughtful look",
    "Bianca Rossi": "30-year-old Italian woman, long wavy chestnut hair, confident expression",
    "Ethan Cole": "26-year-old white man, short dark blond hair, clean shaven, eager look",
    "Yara Saleh": "32-year-old Egyptian woman, dark hair in a low bun, warm smile",
    "Felix Wagner": "49-year-old Austrian man, gray beard, round glasses, scholarly look",
    "Noor Khan": "28-year-old Pakistani woman, long dark hair, gentle smile",
    "Diego Herrera": "35-year-old Colombian man, short dark hair, stubble, charismatic smile",
}
SUITS = ["navy blue suit and white shirt", "charcoal gray suit with a tie", "black suit and light blue shirt",
         "dark navy suit with a subtle pinstripe", "tailored gray suit and white shirt"]


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def make(name: str, desc: str, i: int) -> bool:
    prompt = f"{desc}, " + STYLE.format(suit=SUITS[i % len(SUITS)]) + ", not black and white, no casual clothes"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + f"?width=512&height=512&seed={1000 + i}&nologo=true&model=flux")
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=90)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                im = Image.open(io.BytesIO(r.content)).convert("RGB").resize((320, 320), Image.LANCZOS)
                im.save(OUT / f"{slug(name)}.jpg", quality=86)
                return True
            print(f"{name}: HTTP {r.status_code} {r.text[:120]}")
        except Exception as exc:
            print(f"{name}: {exc}")
        time.sleep(8)
    return False


def main() -> None:
    """--limit N: stop after N attempts (the workflow runs batches and saves between them).
    Exits with code 3 when there is nothing left to make, 2 when the service keeps refusing."""
    OUT.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 999
    force = set(args[args.index("--force") + 1:]) if "--force" in args else set()
    made = failed = streak = tried = 0
    for i, (name, desc) in enumerate(PEOPLE.items()):
        path = OUT / f"{slug(name)}.jpg"
        if path.exists() and slug(name) not in force:
            continue
        if tried >= limit:
            break
        tried += 1
        force.discard(slug(name))
        if make(name, desc, i):
            made += 1; streak = 0
            print(f"made {name}", flush=True)
        else:
            failed += 1; streak += 1
            if streak >= 3:
                print("image service keeps refusing; stopping", flush=True)
                sys.exit(2)
        time.sleep(3)
    print(f"batch: {made} made, {failed} failed", flush=True)
    if tried == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
