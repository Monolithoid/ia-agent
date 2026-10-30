"""
wakeword.py — Détection locale du mot de réveil (« ok nao ») avec openWakeWord.

Utilisé par static_drone.py quand WAKE_MODE=local : le drone écoute en continu,
donne chaque bloc audio au détecteur et n'envoie au VPS que la phrase qui suit
le mot de réveil (voir README, « Mot de réveil local »).

openWakeWord travaille sur des blocs de 80 ms (1280 échantillons) à 16 kHz :
le détecteur rééchantillonne et redécoupe les blocs du micro. Pas d'import
d'openwakeword au chargement du module : les tests tournent sans lui.
"""

import logging
import os
import time
from math import gcd

import numpy as np

log = logging.getLogger("static_drone")

MODEL_RATE = 16000
CHUNK = 1280            # 80 ms à 16 kHz


def resolve_model(wanted, models_dir):
    """Chemin du modèle à charger, ou nom d'un modèle fourni par openWakeWord.

    - un chemin existant (.onnx) → tel quel ;
    - un nom (ex. « ok_nao ») dont models_dir/<nom>.onnx existe → ce fichier ;
    - sinon le nom tel quel : openWakeWord le cherche parmi ses modèles
      pré-entraînés (alexa, hey_jarvis, hey_mycroft…).
    """
    wanted = (wanted or "").strip()
    if os.path.isfile(wanted):
        return wanted
    path = os.path.join(models_dir, wanted + ".onnx")
    if os.path.isfile(path):
        return path
    return wanted


class Resampler:
    """int16 mono à `rate` Hz → int16 mono à `target` Hz, bloc après bloc.

    Rééchantillonner chaque bloc de 30 ms à part laisserait un défaut à chaque
    jointure : on garde un peu d'audio de part et d'autre (filtre de
    resample_poly), au prix de quelques millisecondes de retard.
    """

    def __init__(self, rate, target=MODEL_RATE):
        g = gcd(int(rate), int(target))
        self.up, self.down = int(target) // g, int(rate) // g
        # Demi-longueur du filtre de resample_poly, en échantillons d'entrée,
        # arrondie à un multiple de `down` pour que les sorties tombent juste.
        half = -(-10 * max(self.up, self.down) // self.up) + 1
        self.margin = -(-half // self.down) * self.down
        self._buf = np.zeros(self.margin, dtype=np.float32)    # contexte gauche : silence

    def __call__(self, pcm):
        pcm = np.asarray(pcm, dtype=np.int16)
        if self.up == self.down:
            return pcm
        from scipy.signal import resample_poly     # dépendance d'openWakeWord
        self._buf = np.concatenate([self._buf, pcm.astype(np.float32)])
        n = (len(self._buf) - 2 * self.margin) // self.down * self.down
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        out = resample_poly(self._buf, self.up, self.down)
        start = self.margin * self.up // self.down
        out = out[start:start + n * self.up // self.down]
        self._buf = self._buf[n:]
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def load_model(model, models_dir):
    """Charge un modèle openWakeWord (ONNX, onnxruntime) ; renvoie (Model, nom)."""
    import openwakeword.utils
    from openwakeword.model import Model

    path = resolve_model(model, models_dir)
    name = os.path.splitext(os.path.basename(path))[0]
    if not os.path.isfile(path) and name not in openwakeword.MODELS:
        raise RuntimeError(
            "Modèle de réveil {!r} introuvable : ni fichier, ni {}/{}.onnx, ni modèle "
            "openWakeWord ({}). Voir models/README.md pour entraîner « ok nao ».".format(
                model, models_dir, model, ", ".join(openwakeword.MODELS)))
    # Modèles communs (mel + embedding) et modèle pré-entraîné éventuel :
    # téléchargés une fois dans le paquet openwakeword, rien si déjà présents.
    # (Une liste vide téléchargerait tous les modèles pré-entraînés.)
    try:
        openwakeword.utils.download_models([name])
    except Exception as exc:
        log.warning("Téléchargement des modèles openWakeWord impossible : %s", exc)
    return Model(wakeword_models=[path], inference_framework="onnx"), name


class WakeWordDetector:
    """Donne un score à chaque bloc audio ; signale le mot de réveil au-delà du seuil.

    `model` suit l'interface d'openwakeword.model.Model : predict(int16[1280])
    → {nom: score}, reset().
    """

    def __init__(self, model, name, threshold, rate, debug_every_s=1.0):
        self.model = model
        self.name = name
        self.threshold = threshold
        self.rate = rate
        self.debug_every_s = debug_every_s
        self._resample = Resampler(rate)
        self._buf = np.zeros(0, dtype=np.int16)
        self._peak = 0.0
        self._logged = time.monotonic()

    def process(self, mono):
        """Bloc int16 mono à `rate` Hz → score si le mot de réveil est entendu, sinon None."""
        self._buf = np.concatenate([self._buf, self._resample(mono)])
        best = 0.0
        while len(self._buf) >= CHUNK:
            chunk, self._buf = self._buf[:CHUNK], self._buf[CHUNK:]
            scores = self.model.predict(chunk)
            best = max(best, max((float(s) for s in scores.values()), default=0.0))
        self._debug(best)
        return best if best >= self.threshold else None

    def reset(self):
        """Vide les tampons : évite un second déclenchement sur le même « ok nao »."""
        self._buf = np.zeros(0, dtype=np.int16)
        self.model.reset()

    def _debug(self, score):
        # En DEBUG, le score max de chaque seconde aide à régler WAKE_THRESHOLD.
        self._peak = max(self._peak, score)
        if time.monotonic() - self._logged >= self.debug_every_s:
            log.debug("Score « %s » max sur 1 s : %.2f (seuil %.2f)",
                      self.name, self._peak, self.threshold)
            self._peak, self._logged = 0.0, time.monotonic()
