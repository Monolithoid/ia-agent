"""
speaking_animation.py — Animation « je parle » du NAO, sans dépendance ROS 2.

Quand le drone statique parle (ADR 004), le NAO ne produit aucun son mais
anime ses yeux et fait de légers gestes pour donner l'illusion que c'est lui
qui parle.  Une image (« frame ») est calculée à chaque tick :

  - LEDs des yeux (FaceLeds) qui alternent clair / sombre, comme une bouche
  - petit hochement de tête (HeadPitch)
  - un tick sur deux, un geste de bras léger, alterné gauche / droite

Chaque commande de bras est un message séparé : nao_ws_bridge.py refuse un
mouvement multi-articulations dès qu'un groupe n'a pas de stiffness.
"""

SPEAKING_PERIOD = 1.5   # secondes entre deux images

# Yeux : blanc bleuté, alterné clair / sombre (fadeRGB 0.5 s côté robot)
_EYES_BRIGHT = (0.90, 0.90, 1.00)
_EYES_DIM    = (0.30, 0.30, 0.45)

# Hochement de tête léger (radians, HeadPitch positif = vers le bas)
_HEAD_PITCH = (0.06, -0.02)
_HEAD_SPEED = 0.08

_ARM_JOINTS = ("ShoulderPitch", "ShoulderRoll", "ElbowYaw", "ElbowRoll")
# Angles du bras gauche ; le bras droit est le miroir (roll et yaw opposés).
_ARM_REST = (1.50, 0.09, -1.20, -0.40)   # = _ARM_POSES["rest"] du renderer
_ARM_BEAT = (1.15, 0.20, -1.00, -0.75)   # avant-bras levé, coude plié
_ARM_SPEED = 0.10


def _arm(side: str, angles: tuple) -> dict:
    """Commande /naoqi/motion pour un bras ('L' ou 'R')."""
    pitch, roll, yaw, elbow = angles
    if side == "R":
        roll, yaw, elbow = -roll, -yaw, -elbow
    return {
        "joints": [side + j for j in _ARM_JOINTS],
        "angles": [pitch, roll, yaw, elbow],
        "speed": _ARM_SPEED,
    }


def speaking_frame(tick: int) -> tuple[dict, list[dict]]:
    """Image numéro `tick` : (commande /naoqi/leds, commandes /naoqi/motion).

    Cycle de 4 ticks :
      0 : yeux clairs, tête basse, bras gauche lève l'avant-bras
      1 : yeux sombres, tête haute
      2 : yeux clairs, tête basse, bras droit lève l'avant-bras
      3 : yeux sombres, tête haute
    """
    r, g, b = _EYES_BRIGHT if tick % 2 == 0 else _EYES_DIM
    leds = {"group": "FaceLeds", "r": r, "g": g, "b": b, "duration": SPEAKING_PERIOD}

    motions = [{"joint": "HeadPitch", "angle": _HEAD_PITCH[tick % 2], "speed": _HEAD_SPEED}]
    phase = tick % 4
    if phase == 0:
        motions += [_arm("L", _ARM_BEAT), _arm("R", _ARM_REST)]
    elif phase == 2:
        motions += [_arm("L", _ARM_REST), _arm("R", _ARM_BEAT)]
    return leds, motions


def speaking_expired(started_at: float, now: float, timeout_s: float) -> bool:
    """Arrêter l'animation si aucun `speaking=false` n'est arrivé à temps
    (drone débranché en pleine phrase).  timeout_s <= 0 : jamais."""
    return timeout_s > 0 and now - started_at > timeout_s
