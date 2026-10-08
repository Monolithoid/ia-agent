"""
direction.py — Direction du son pour le drone statique (ADR 004, §4).

Fonctions pures (numpy uniquement), testables sans matériel.

Convention commune : l'axe de la caméra vaut 0°, un angle positif est à
droite de la caméra, un angle négatif à gauche, dans l'intervalle ]-180, 180].
"""

import numpy as np

# Valeurs de direction_source publiées sur /drones/{id}/audio
SOURCE_RESPEAKER = "respeaker"
SOURCE_LOUDEST_MIC = "loudest_mic"
SOURCE_IMAGE = "image"
SOURCE_NONE = "none"


def choose_direction_source(has_respeaker, n_mics, has_camera):
    """Règle fixe de l'ADR 004 : ReSpeaker, sinon micro le plus fort, sinon image."""
    if has_respeaker:
        return SOURCE_RESPEAKER
    if n_mics >= 2:
        return SOURCE_LOUDEST_MIC
    if has_camera:
        return SOURCE_IMAGE
    return SOURCE_NONE


def normalize_angle(deg):
    """Ramène un angle dans ]-180, 180]."""
    a = (float(deg) + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def parse_mics(spec):
    """Lit MICS="gauche:-60,droite:60" → [("gauche", -60.0), ("droite", 60.0)].

    L'ordre de la liste est l'ordre des canaux d'entrée audio.
    """
    mics = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, angle = item.rpartition(":")
        if not sep or not name:
            raise ValueError("Entrée MICS invalide (attendu nom:angle) : {!r}".format(item))
        mics.append((name.strip(), float(angle)))
    return mics


def channel_rms(frames):
    """RMS par canal d'un bloc (échantillons × canaux), en float."""
    x = np.asarray(frames, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[0] == 0:
        return np.zeros(x.shape[1])
    return np.sqrt(np.mean(x * x, axis=0))


def loudest_mic(levels, angles):
    """Angle du micro au niveau (RMS ou énergie) le plus élevé.

    levels : un niveau par micro, dans l'ordre de `angles`.
    Renvoie None si tous les niveaux sont nuls.
    """
    levels = np.asarray(levels, dtype=np.float64)
    if len(levels) != len(angles):
        raise ValueError("{} niveaux pour {} micros".format(len(levels), len(angles)))
    if len(levels) == 0 or not np.any(levels > 0):
        return None
    return normalize_angle(angles[int(np.argmax(levels))])


def respeaker_angle(doa_deg, offset_deg=0.0, invert=False):
    """Convertit DOAANGLE (0..359, repère du ReSpeaker) vers le repère caméra."""
    a = -float(doa_deg) if invert else float(doa_deg)
    return normalize_angle(a + offset_deg)


def circular_mean_deg(angles):
    """Moyenne circulaire d'une liste d'angles en degrés (None si vide)."""
    if len(angles) == 0:
        return None
    r = np.radians(np.asarray(angles, dtype=np.float64))
    return normalize_angle(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))


def motion_angle(prev_gray, curr_gray, hfov_deg, threshold=25, min_fraction=0.002):
    """Angle du mouvement entre deux images en niveaux de gris (ADR 004).

    Différence avec l'image précédente, x moyen des pixels changés :
        angle = (x / largeur − 0.5) × champ_de_vision
    Renvoie None si moins de `min_fraction` des pixels ont changé.
    """
    prev = np.asarray(prev_gray, dtype=np.int16)
    curr = np.asarray(curr_gray, dtype=np.int16)
    if prev.shape != curr.shape:
        return None
    changed = np.abs(curr - prev) > threshold
    count = int(changed.sum())
    if count == 0 or count < min_fraction * changed.size:
        return None
    width = changed.shape[1]
    xs = np.nonzero(changed)[1]
    # Centre des pixels : (x + 0.5) pour qu'une image symétrique donne 0°.
    x_mean = xs.mean() + 0.5
    return float((x_mean / width - 0.5) * hfov_deg)
