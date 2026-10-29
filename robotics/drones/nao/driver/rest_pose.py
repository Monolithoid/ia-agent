"""
rest_pose.py — Retour du NAO à sa pose de lancement, sans dépendance ROS 2.

Le robot est une marionnette : il ne décide pas de sa position.  Au
lancement, nao_ws_bridge.py lit les angles de la tête et des bras et les
annonce une fois par connexion dans /nao/capabilities (« launch_pose »).
Le driver NAO garde cette pose comme position de repos unique et, quand
une articulation n'a plus reçu d'ordre depuis REST_DELAY secondes, lui
envoie lui-même l'ordre d'y revenir.
"""

# Vitesse articulaire la plus lente de la tête et des bras (rad/s à la
# vitesse 1.0, cf. _JOINT_MAX_VELOCITY dans nao_ws_bridge.py).  Prendre la
# plus lente surestime la durée d'un geste : le retour ne part jamais
# pendant qu'un geste dure encore, sinon le pont le jetterait.
_SLOWEST_VELOCITY = 7.19


def parse_motion(data: dict) -> tuple[list[str], list[float], float]:
    """(articulations, angles, vitesse) d'une commande /nao/motion,
    multi-articulations {joints, angles, speed} ou simple {joint, angle, speed}."""
    speed = float(data.get("speed", 0.05))
    if "joints" in data:
        return [str(j) for j in data["joints"]], [float(a) for a in data["angles"]], speed
    if "joint" in data:
        return [str(data["joint"])], [float(data.get("angle", 0.0))], speed
    return [], [], speed


class RestTracker:
    """Suit le dernier angle commandé de chaque articulation et décide du retour au repos."""

    def __init__(self, delay: float = 3.0, speed: float = 0.1, tolerance: float = 0.02):
        self.delay = delay
        self.speed = speed
        self.tolerance = tolerance
        self.rest: dict[str, float] = {}
        self._sent: dict[str, float] = {}      # articulation -> dernier angle commandé
        self._free_at: dict[str, float] = {}   # articulation -> heure où elle peut revenir

    def set_rest(self, pose: dict) -> None:
        """Nouvelle pose de lancement : le robot vient de (re)démarrer son script."""
        self.rest = {str(j): float(a) for j, a in pose.items()}
        self._sent.clear()
        self._free_at.clear()

    def on_order(self, joints: list[str], angles: list[float], speed: float, now: float) -> None:
        for j, a in zip(joints, angles):
            prev = self._sent.get(j, self.rest.get(j, a))
            duration = abs(a - prev) / (max(speed, 0.01) * _SLOWEST_VELOCITY)
            self._sent[j] = a
            # Fin estimée du geste, puis REST_DELAY d'inactivité.
            self._free_at[j] = now + duration + self.delay

    def _away(self) -> list[str]:
        return sorted(j for j, a in self._sent.items()
                      if j in self.rest and abs(a - self.rest[j]) > self.tolerance)

    def _command(self, joints: list[str]) -> dict | None:
        if not joints:
            return None
        for j in joints:
            self._sent[j] = self.rest[j]
        return {"joints": joints, "angles": [self.rest[j] for j in joints], "speed": self.speed}

    def due(self, now: float) -> dict | None:
        """Commande /nao/motion de retour au repos pour les articulations inactives, ou None."""
        return self._command([j for j in self._away() if self._free_at.get(j, 0.0) <= now])

    def all_to_rest(self) -> dict | None:
        """Commande de retour immédiat de toutes les articulations déplacées."""
        return self._command(self._away())
