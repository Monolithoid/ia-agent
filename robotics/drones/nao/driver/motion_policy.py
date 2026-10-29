"""
motion_policy.py — Décisions de mouvement du NAO, sans dépendance ROS 2.

Le robot est une marionnette : il plante trop souvent pour décider de sa
position.  Le driver NAO décide donc de tout ce qui part sur /nao/motion,
/nao/gesture et /nao/cmd_vel ; nao_ws_bridge.py, sur le robot, ne garde que
des bornes (angles, vitesse, stiffness, marche) en dernier filet.

Décisions prises ici :
  - rythme : les ordres sont fusionnés par articulation (le dernier gagne)
    et envoyés au plus une fois toutes les min_interval secondes ;
  - fin du geste : une articulation reste occupée max(min_move_time, durée
    estimée du geste) ; un ordre reçu pendant ce temps est perdu et compté
    (while_moving="drop") ou attend la fin du geste ("latest") ;
  - postures : liste autorisée, délai entre deux changements, articulations
    bloquées pendant posture_block secondes ;
  - marche : articulations bloquées tant que le robot marche ;
  - chaleur : un groupe trop chaud (motor_temp de /nao/status) refuse les ordres ;
  - repos : la pose de lancement annoncée par le robot (launch_pose) est la
    position de repos unique ; une articulation inactive depuis rest_delay
    après la fin de son geste y retourne à rest_speed.
"""

DEFAULT_LIMITS = {
    "min_interval":      0.1,    # s entre deux commandes /nao/motion (10 Hz max)
    "min_move_time":     0.3,    # s pendant lesquelles une articulation garde sa cible
    "velocity_scale":    1.0,    # part de JOINT_MAX_VELOCITY réellement atteinte (0 = pas d'estimation)
    "while_moving":      "drop",  # ordre pour une articulation en mouvement : "drop" ou "latest"
    "max_motor_temp":    70,     # °C ; au-delà le groupe refuse les ordres (0 = désactivé)
    "postures":          ["Stand", "StandInit", "Sit", "Crouch"],
    "posture_cooldown":  5.0,    # s entre deux changements de posture
    "posture_block":     5.0,    # s pendant lesquelles un changement de posture bloque les gestes
    "joints_while_busy": False,  # accepter les gestes pendant une posture ou une marche
    "rest_return":       True,   # retour à la pose de lancement
    "rest_delay":        3.0,    # s d'inactivité (après la fin du geste) avant le retour
    "rest_speed":        0.1,    # vitesse du retour au repos
    "rest_tolerance":    0.02,   # rad ; plus près que ça, l'articulation est au repos
}

# Vitesse max (rad/s) à la vitesse 1.0 : vitesse à vide du moteur tête/bras
# (RE-Max 17, 11 900 tr/min = 1246 rad/s) / rapport de réduction (doc
# Aldebaran « H25 - Motors » : A 150.27, B 173.22, C 50.61).  Les mains
# (rapport d'ouverture 0..1) n'y sont pas : min_move_time seulement.
JOINT_MAX_VELOCITY = {
    "HeadYaw": 8.29, "HeadPitch": 7.19,
    "ShoulderPitch": 8.29, "ShoulderRoll": 7.19,
    "ElbowYaw": 8.29, "ElbowRoll": 7.19,
    "WristYaw": 24.62,
}


def joint_group(joint: str) -> str:
    """Groupe de température (head / larm / rarm) d'une articulation, comme
    les clés motor_temp de /nao/status."""
    if joint.startswith("Head"):
        return "head"
    return "larm" if joint.startswith("L") else "rarm" if joint.startswith("R") else ""


def load_limits(section) -> tuple[dict, list[str]]:
    """Fusionne une configuration (dict) avec DEFAULT_LIMITS.

    Clés inconnues et valeurs du mauvais type sont ignorées : une faute de
    frappe ne désactive jamais une limite.  Retourne (limites, avertissements).
    """
    limits = dict(DEFAULT_LIMITS)
    warnings = []
    if not isinstance(section, dict):
        return limits, warnings
    for key, value in section.items():
        if key not in limits:
            warnings.append(f"clé inconnue : {key}")
            continue
        default = limits[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif key == "while_moving":
            ok = value in ("drop", "latest")
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if not ok:
            warnings.append(f"valeur invalide : {key}={value!r}")
            continue
        limits[key] = value
    return limits, warnings


def parse_motion(data: dict) -> tuple[list[str], list[float], float]:
    """(articulations, angles, vitesse) d'une commande /nao/motion,
    multi-articulations {joints, angles, speed} ou simple {joint, angle, speed}."""
    speed = float(data.get("speed", 0.05))
    if "joints" in data:
        return [str(j) for j in data["joints"]], [float(a) for a in data["angles"]], speed
    if "joint" in data:
        return [str(data["joint"])], [float(data.get("angle", 0.0))], speed
    return [], [], speed


class MotionPolicy:
    """Toutes les décisions de mouvement du NAO.  Les heures (now) sont
    passées par l'appelant, ce qui rend la classe testable sans horloge."""

    def __init__(self, limits: dict | None = None):
        self.limits = limits or dict(DEFAULT_LIMITS)
        self.rest: dict[str, float] = {}
        self._pending: dict[str, tuple[float, float, bool]] = {}  # j -> (angle, vitesse, repos ?)
        self._sent: dict[str, float] = {}        # dernier angle envoyé
        self._hold_until: dict[str, float] = {}  # fin estimée du geste en cours
        self._last_order: dict[str, float] = {}  # heure du dernier ordre accepté
        self._resting: set[str] = set()          # geste en cours = retour au repos
        self._last_send = float("-inf")
        self._hot: set[str] = set()
        self._walking = False
        self._posture_until = 0.0
        self._last_posture = float("-inf")
        self.dropped = 0

    # -- état venu du robot ------------------------------------------------

    def set_rest(self, pose: dict) -> None:
        """Pose de lancement annoncée par le robot : il vient de (re)lancer son script."""
        self.rest = {str(j): float(a) for j, a in pose.items()}
        self._pending.clear()
        self._sent.clear()
        self._hold_until.clear()
        self._last_order.clear()
        self._resting.clear()

    def set_motor_temps(self, temps: dict) -> set[str]:
        """temps : {"head"|"larm"|"rarm": °C}.  Retourne les groupes trop chauds."""
        max_temp = self.limits["max_motor_temp"]
        self._hot = {g for g, t in temps.items() if max_temp and t >= max_temp}
        return self._hot

    def set_walking(self, walking: bool) -> None:
        self._walking = walking
        if walking:
            self._pending.clear()

    def _busy(self, now: float) -> bool:
        return self._walking or now < self._posture_until

    # -- ordres ------------------------------------------------------------

    def posture_allowed(self, name: str, now: float) -> tuple[bool, str]:
        """(autorisée ?, raison du refus).  Une posture autorisée bloque les gestes."""
        if name not in self.limits["postures"]:
            return False, "hors liste"
        if self._walking:
            return False, "marche en cours"
        if now - self._last_posture < float(self.limits["posture_cooldown"]):
            return False, "délai entre deux postures"
        self._last_posture = now
        self._posture_until = now + float(self.limits["posture_block"])
        self._pending.clear()
        return True, ""

    def walk_allowed(self, now: float) -> bool:
        return now >= self._posture_until

    def submit(self, joints, angles, speed: float, now: float) -> int:
        """Retient un geste ; retourne le nombre d'articulations acceptées."""
        if self._busy(now) and not self.limits["joints_while_busy"]:
            return 0
        drop_moving = self.limits["while_moving"] == "drop"
        accepted = 0
        for j, a in zip(joints, angles):
            if joint_group(j) in self._hot:
                continue
            if (drop_moving and j not in self._resting
                    and self._hold_until.get(j, 0.0) > now):
                self.dropped += 1   # l'articulation finit son geste : ordre perdu exprès
                continue
            self._pending[j] = (float(a), float(speed), False)
            self._last_order[j] = now
            accepted += 1
        return accepted

    def take_dropped(self) -> int:
        n, self.dropped = self.dropped, 0
        return n

    # -- envoi -------------------------------------------------------------

    def _duration(self, joint: str, angle: float, speed: float) -> float:
        duration = float(self.limits["min_move_time"])
        key = joint[1:] if joint[:1] in ("L", "R") else joint
        vmax = JOINT_MAX_VELOCITY.get(key, 0.0) * float(self.limits["velocity_scale"])
        prev = self._sent.get(joint, self.rest.get(joint))
        if vmax > 0 and prev is not None:
            duration = max(duration, abs(angle - prev) / (max(speed, 0.01) * vmax))
        return duration

    def _away_from_rest(self) -> list[str]:
        tol = float(self.limits["rest_tolerance"])
        return sorted(j for j, a in self._sent.items()
                      if j in self.rest and abs(a - self.rest[j]) > tol)

    def _queue_due_rest(self, now: float) -> None:
        if not self.limits["rest_return"] or not self.rest or self._busy(now):
            return
        for j in self._away_from_rest():
            if j in self._pending:
                continue
            due = max(self._last_order.get(j, 0.0), self._hold_until.get(j, 0.0)) \
                + float(self.limits["rest_delay"])
            if due <= now:
                self._pending[j] = (self.rest[j], float(self.limits["rest_speed"]), True)

    def take(self, now: float) -> dict | None:
        """Commande /nao/motion à envoyer maintenant, ou None."""
        if now - self._last_send < float(self.limits["min_interval"]):
            return None
        self._queue_due_rest(now)
        ready = sorted(j for j, (_, _, is_rest) in self._pending.items()
                       if self._hold_until.get(j, 0.0) <= now
                       or (j in self._resting and not is_rest))
        if not ready:
            return None
        batch = {j: self._pending.pop(j) for j in ready}
        # Une seule vitesse par commande : la plus lente gagne.
        speed = min(v[1] for v in batch.values())
        for j, (a, _, is_rest) in batch.items():
            self._hold_until[j] = now + self._duration(j, a, speed)
            self._sent[j] = a
            (self._resting.add if is_rest else self._resting.discard)(j)
        self._last_send = now
        return {"joints": ready, "angles": [round(batch[j][0], 4) for j in ready],
                "speed": round(speed, 4)}

    def all_to_rest(self) -> dict | None:
        """Commande de retour immédiat de toutes les articulations déplacées."""
        self._pending.clear()
        joints = self._away_from_rest()
        if not joints:
            return None
        for j in joints:
            self._sent[j] = self.rest[j]
            self._resting.add(j)
        return {"joints": joints, "angles": [self.rest[j] for j in joints],
                "speed": float(self.limits["rest_speed"])}
