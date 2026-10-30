#!/usr/bin/env python3
"""
generate_clips_fr.py — Voix de synthèse françaises pour entraîner « ok nao ».

Le générateur d'openWakeWord (piper-sample-generator) ne parle qu'anglais : ce
script le remplace pour l'étape --generate_clips. Il écrit des WAV 16 kHz mono
dans les quatre dossiers attendus par openwakeword.train :

    <out>/positive_train  <out>/positive_test     « ok nao » sous toutes ses formes
    <out>/negative_train  <out>/negative_test     mots proches (« ok », « nao »,
                                                  « cacao », « ok non »…)

Deux sources, chacune facultative :
  - edge-tts : toutes les voix fr-* (France, Canada, Belgique, Suisse), débit
    et hauteur variés. Demande Internet.
  - Piper : voix françaises locales, dont fr_FR-mls-medium (125 locuteurs).
    Téléchargées dans --piper-dir au premier usage.

Les phrases tirées pour le test viennent de voix absentes de l'entraînement :
le score de test dit alors comment le modèle réagit à une voix inconnue.

Usage (voir models/README.md) :
    pip install edge-tts piper-tts scipy numpy
    python generate_clips_fr.py --out sortie/ok_nao --n-train 3000 --n-test 300

Dépendance système : ffmpeg (décodage des MP3 d'edge-tts).
"""

import argparse
import asyncio
import io
import os
import random
import subprocess
import sys
import uuid
import wave
from math import gcd

import numpy as np

RATE = 16000

# Façons d'écrire le mot de réveil : la ponctuation change l'intonation.
POSITIVE = ["ok nao", "OK Nao.", "Ok, Nao !", "Ok Nao ?", "okay nao", "Okay, Nao.",
            "Ok Nao,", "OK NAO !", "ok, nao..."]

# Ce qui ressemble à « ok nao » sans l'être, et quelques phrases courantes.
NEGATIVE = ["ok", "Nao.", "le Nao", "Nao, tu m'entends ?", "ok non", "Ok, no.", "ok nono",
            "ok néo", "ok noé", "ok nano", "ok Léo", "ok Marco", "ok Google", "OK Bruno",
            "cacao", "chaos", "Ciao !", "et oh", "oh non", "ah non", "au niveau",
            "en haut", "ok d'accord", "ok on y va", "ok maintenant", "ok merci",
            "d'accord", "bonne nuit", "bonjour", "Coco", "Nono", "un noyau",
            "Il fait beau aujourd'hui.", "Quelle heure est-il ?", "On mange quoi ce soir ?",
            "C'est l'heure du goûter.", "Tu peux répéter ?", "Allô ?"]

PIPER_VOICES = ["fr_FR-mls-medium", "fr_FR-upmc-medium", "fr_FR-siwis-medium",
                "fr_FR-tom-medium", "fr_FR-gilles-low"]


# ----------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------

def to_16k(pcm, rate):
    if rate == RATE:
        return pcm
    from scipy.signal import resample_poly
    g = gcd(rate, RATE)
    out = resample_poly(pcm.astype(np.float32), RATE // g, rate // g)
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())


def read_wav(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
        return pcm, w.getframerate()


def mp3_to_pcm(mp3):
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1",
                          "-ar", str(RATE), "-f", "s16le", "pipe:1"],
                         input=mp3, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.int16)


# ----------------------------------------------------------------------
# Plan : (texte, voix) répartis entre entraînement et test
# ----------------------------------------------------------------------

class Plan:
    """Répartit les voix entre entraînement (~90 %) et test (~10 %)."""

    def __init__(self, voices, seed):
        rnd = random.Random(seed)
        voices = list(voices)
        rnd.shuffle(voices)
        n_test = max(1, len(voices) // 10) if len(voices) > 1 else 0
        self.test, self.train = voices[:n_test], voices[n_test:] or voices
        self.test = self.test or voices


def jobs(plan, texts, n_train, n_test, rnd):
    """[(dossier, texte, voix)] : n_train clips d'entraînement, n_test de test."""
    out = [("train", rnd.choice(texts), rnd.choice(plan.train)) for _ in range(n_train)]
    out += [("test", rnd.choice(texts), rnd.choice(plan.test)) for _ in range(n_test)]
    return out


# ----------------------------------------------------------------------
# edge-tts
# ----------------------------------------------------------------------

async def edge_voices():
    import edge_tts
    return sorted(v["ShortName"] for v in await edge_tts.list_voices()
                  if v["Locale"].startswith("fr-"))


async def edge_generate(todo, out, kind, rnd, concurrency=8):
    import edge_tts
    sem = asyncio.Semaphore(concurrency)
    done = [0]

    async def one(split, text, voice):
        rate = "{:+d}%".format(rnd.randint(-25, 30))
        pitch = "{:+d}Hz".format(rnd.randint(-15, 15))
        async with sem:
            for attempt in range(3):
                try:
                    buf = io.BytesIO()
                    async for chunk in edge_tts.Communicate(text, voice, rate=rate,
                                                            pitch=pitch).stream():
                        if chunk["type"] == "audio":
                            buf.write(chunk["data"])
                    pcm = mp3_to_pcm(buf.getvalue())
                    break
                except Exception as exc:
                    if attempt == 2:
                        print("edge-tts {} {!r} : {}".format(voice, text, exc), file=sys.stderr)
                        return
                    await asyncio.sleep(2 ** attempt)
        write_wav(os.path.join(out, "{}_{}".format(kind, split), uuid.uuid4().hex + ".wav"), pcm)
        done[0] += 1
        if done[0] % 200 == 0:
            print("  edge-tts {} : {}/{}".format(kind, done[0], len(todo)))

    await asyncio.gather(*(one(*job) for job in todo))


# ----------------------------------------------------------------------
# Piper
# ----------------------------------------------------------------------

def piper_load(names, data_dir):
    """[(nom, PiperVoice)] ; télécharge les voix absentes."""
    from piper import PiperVoice
    os.makedirs(data_dir, exist_ok=True)
    loaded = []
    for name in names:
        path = os.path.join(data_dir, name + ".onnx")
        if not os.path.isfile(path):
            subprocess.run([sys.executable, "-m", "piper.download_voices",
                            "--data-dir", data_dir, name], check=False)
        if os.path.isfile(path):
            loaded.append((name, PiperVoice.load(path)))
        else:
            print("Voix Piper {} indisponible, ignorée".format(name), file=sys.stderr)
    return loaded


def piper_speakers(loaded):
    """Un « locuteur » = (indice de voix, speaker_id)."""
    return [(i, s) for i, (_, v) in enumerate(loaded)
            for s in range(max(1, v.config.num_speakers))]


def piper_generate(todo, loaded, out, kind, rnd):
    from piper import SynthesisConfig
    for n, (split, text, (i, speaker)) in enumerate(todo, 1):
        voice = loaded[i][1]
        cfg = SynthesisConfig(
            speaker_id=speaker if voice.config.num_speakers > 1 else None,
            length_scale=rnd.uniform(0.75, 1.35),
            noise_scale=rnd.uniform(0.4, 0.9),
            noise_w_scale=rnd.uniform(0.5, 1.1))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w, syn_config=cfg)
        pcm, rate = read_wav(buf.getvalue())
        write_wav(os.path.join(out, "{}_{}".format(kind, split), uuid.uuid4().hex + ".wav"),
                  to_16k(pcm, rate))
        if n % 200 == 0:
            print("  Piper {} : {}/{}".format(kind, n, len(todo)))


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="dossier <output_dir>/<model_name> du YAML")
    ap.add_argument("--n-train", type=int, default=3000, help="clips positifs (et négatifs) d'entraînement")
    ap.add_argument("--n-test", type=int, default=300, help="clips positifs (et négatifs) de test")
    ap.add_argument("--edge-share", type=float, default=0.5,
                    help="part des clips faite par edge-tts (le reste par Piper)")
    ap.add_argument("--no-edge", action="store_true", help="sans edge-tts (hors ligne)")
    ap.add_argument("--no-piper", action="store_true", help="sans Piper")
    ap.add_argument("--piper-dir", default="piper-voices")
    ap.add_argument("--piper-voices", default=",".join(PIPER_VOICES))
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    for d in ("positive_train", "positive_test", "negative_train", "negative_test"):
        os.makedirs(os.path.join(args.out, d), exist_ok=True)
    rnd = random.Random(args.seed)

    share = 0.0 if args.no_edge else (1.0 if args.no_piper else args.edge_share)
    loaded = [] if args.no_piper else piper_load(
        [v for v in args.piper_voices.split(",") if v], args.piper_dir)
    if not loaded:
        share = 0.0 if args.no_edge else 1.0
    if args.no_edge and not loaded:
        sys.exit("Aucune voix : ni edge-tts, ni Piper")

    n_edge = (round(args.n_train * share), round(args.n_test * share))
    n_piper = (args.n_train - n_edge[0], args.n_test - n_edge[1])

    if n_edge[0] + n_edge[1]:
        voices = asyncio.run(edge_voices())
        print("edge-tts : {} voix françaises".format(len(voices)))
        plan = Plan(voices, args.seed)
        for kind, texts in (("positive", POSITIVE), ("negative", NEGATIVE)):
            asyncio.run(edge_generate(jobs(plan, texts, *n_edge, rnd), args.out, kind, rnd))

    if n_piper[0] + n_piper[1]:
        speakers = piper_speakers(loaded)
        print("Piper : {} voix, {} locuteurs".format(len(loaded), len(speakers)))
        plan = Plan(speakers, args.seed)
        for kind, texts in (("positive", POSITIVE), ("negative", NEGATIVE)):
            piper_generate(jobs(plan, texts, *n_piper, rnd), loaded, args.out, kind, rnd)

    for d in ("positive_train", "positive_test", "negative_train", "negative_test"):
        print("{:15s} {} clips".format(d, len(os.listdir(os.path.join(args.out, d)))))


if __name__ == "__main__":
    main()
