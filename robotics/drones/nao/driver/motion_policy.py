"""
motion_policy.py — Décisions de mouvement du NAO, sans dépendance ROS 2.

Le robot est une marionnette : il plante trop souvent pour décider de sa
position.  Le driver NAO décide donc de tout ce qui part sur /nao/motion,
/nao/gesture et /nao/cmd_vel ; nao_ws_bridge.py, sur le robot, ne garde que
des bornes (angles, vitesse, stiffness, marche) en dernier filet.

Univers moteur contraint : NAO reste assis et ne bouge que trois chaînes
(tête, bras gauche, bras droit).  Le profil moteur, calibré depuis la régie
et annoncé par le robot (motor_profile dans /nao/capabilities), donne pour
chaque chaîne une position de repos, une position de départ, une plage par
articulation et une vitesse.  Le LLM ne connaît que les plages.

Décisions prises ici :
  - séquence : chaque geste va repos → départ → cible → départ → repos.
    La cible part de la position de départ, complétée par les articulations
    demandées et ramenées dans leur plage.  Un nouvel ordre pendant le geste
    enchaîne cible → départ → nouvelle cible, sans repasser par le repos ;
  - regroupement : les ordres d'une même chaîne reçus en merge_window
    secondes forment une seule cible (le LLM envoie souvent une action par
    articulation) ;
  - fin d'étape : une étape dure max(min_move_time, durée estimée d'après
    JOINT_MAX_VELOCITY) ; l'étape suivante part ensuite ;
  - repos : une chaîne inactive depuis rest_delay revient au repos par le
    départ ;
  - sans profil, ou pour une articulation hors chaîne, l'ordre est refusé ;
  - jambes : postures et marche bougent les jambes, que ce NAO ne bouge pas.
    Elles sont donc coupées par défaut (postures vides, walk=false).  Une
    fois réactivées : liste de postures, délai entre deux changements,
    gestes bloqués pendant posture_block secondes et pendant la marche, pas
    de marche pendant une posture ni de posture pendant la marche ;
  - chaleur : une chaîne trop chaude (motor_temp de /nao/status) refuse les
    nouveaux ordres, mais peut encore revenir au repos.
"""

DEFAULT_LIMITS = {
    "min_interval":      0.1,    # s entre deux envois /nao/motion (10 Hz max)
    "min_move_time":     0.3,    # s minimum par étape de la séquence
    "velocity_scale":    1.0,    # part de JOINT_MAX_VELOCITY réellement atteinte (0 = pas d'estimation)
    "merge_window":      0.3,    # s pendant lesquelles les ordres d'une chaîne forment une seule cible
    "max_motor_temp":    70,     # °C ; au-delà la chaîne refuse les ordres (0 = désactivé)
    "postures":          [],     # postures autorisées (ex. ["Stand", "Sit"]) ; vide = aucune
    "walk":              False,  # autoriser /nao/cmd_vel (marche)
    "posture_cooldown":  5.0,    # s entre deux changements de posture
    "posture_block":     5.0,    # s pendant lesquelles un changement de posture bloque les gestes
    "joints_while_busy": False,  # accepter les gestes pendant une posture ou une marche
    "rest_return":       True,   # retour au repos après rest_delay d'inactivité
    "rest_delay":        3.0,    # s d'inactivité (après la fin du geste) avant le retour
}

# Chaînes commandables et leurs articulations.  Poignets et mains n'en font
# pas partie : pas de mouvement de main dans cet univers.
CHAINS = {
    "head": ("HeadYaw", "HeadPitch"),
    "larm": ("LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll"),
    "rarm": ("RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll"),
}

# Vitesse max (rad/s) à la vitesse 1.0 : vitesse à vide du moteur tête/bras
# (RE-Max 17, 11 900 tr/min = 1246 rad/s) / rapport de réduction (doc
# Aldebaran « H25 - Motors » : A 150.27, B 173.22, C 50.61).
JOINT_MAX_VELOCITY = {
    "HeadYaw": 8.29, "HeadPitch": 7.19,
    "ShoulderPitch": 8.29, "ShoulderRoll": 7.19,
    "ElbowYaw": 8.29, "ElbowRoll": 7.19,
    "WristYaw": 24.62,
}


def chain_of(joint: str) -> str:
    """Chaîne (head / larm / rarm) d'une articulation commandable, '' sinon.
    Mêmes noms que les clés motor_temp de /nao/status."""
    for name, joints in CHAINS.items():
        if joint in joints:
            return name
    return ""


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
    multi-articulations {joints, angles, speed} ou simple {joint, angle, speed}.
    Vitesse 0 quand elle n'est pas donnée : celle du profil s'applique."""
    speed = float(data.get("speed", 0.0))
    if "joints" in data:
        return [str(j) for j in data["joints"]], [float(a) for a in data["angles"]], speed
    if "joint" in data:
        return [str(data["joint"])], [float(data.get("angle", 0.0))], speed
    return [], [], speed


def _pose(section, joints) -> dict | None:
    if not isinstance(section, dict):
        return None
    try:
        return {j: float(section[j]) for j in joints}
    except (KeyError, TypeError, ValueError):
        return None


def load_profile(profile) -> tuple[dict, list[str]]:
    """Chaînes valides d'un profil moteur annoncé par le robot.

    Retourne ({chaîne: {rest, start, range, speed}}, erreurs).  Une chaîne
    incomplète ou incohérente (départ hors plage) est écartée : elle ne
    bougera pas.  Le robot valide déjà le profil ; ceci protège d'un
    profil écrit à la main.
    """
    chains, errors = {}, []
    if not isinstance(profile, dict):
        return chains, errors
    section = profile.get("chains", {})
    if not isinstance(section, dict):
        return chains, ["chains n'est pas un objet"]
    for name, cfg in section.items():
        if name not in CHAINS or not isinstance(cfg, dict):
            errors.append(f"chaîne inconnue : {name}")
            continue
        joints = CHAINS[name]
        rest, start = _pose(cfg.get("rest"), joints), _pose(cfg.get("start"), joints)
        rng = {}
        for j in joints:
            try:
                lo, hi = (float(v) for v in cfg.get("range", {})[j])
            except (KeyError, TypeError, ValueError, AttributeError):
                break
            if lo > hi:
                break
            rng[j] = (lo, hi)
        try:
            speed = float(cfg.get("speed", 0.0))
        except (TypeError, ValueError):
            speed = 0.0
        if rest is None or start is None or len(rng) != len(joints) or not 0 < speed <= 1:
            errors.append(f"{name} : repos, départ, plage ou vitesse manquant")
            continue
        if any(not rng[j][0] - 1e-3 <= start[j] <= rng[j][1] + 1e-3 for j in joints):
            errors.append(f"{name} : position de départ hors plage")
            continue
        chains[name] = {"rest": rest, "start": start, "range": rng, "speed": speed}
    return chains, errors


class _Chain:
    """État d'une chaîne : dernière étape envoyée, étapes à venir, cible en formation."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.joints = CHAINS[name]
        self.rest = cfg["rest"]
        self.start = cfg["start"]
        self.range = cfg["range"]
        self.speed = cfg["speed"]
        self.pose = dict(self.rest)   # dernière position commandée (le robot démarre au repos)
        self.kind = "rest"            # rest | start | target : nature de la dernière étape
        self.hold_until = 0.0         # fin estimée de la dernière étape
        self.queue: list[tuple[str, dict, float]] = []   # (nature, position, vitesse)
        self.target: dict | None = None    # cible en formation (pas encore planifiée)
        self.target_speed = self.speed
        self.target_since = 0.0
        self.last_order = 0.0

    def clamp(self, joint: str, angle: float) -> float:
        lo, hi = self.range[joint]
        return max(lo, min(hi, angle))


class MotionPolicy:
    """Toutes les décisions de mouvement du NAO.  Les heures (now) sont
    passées par l'appelant, ce qui rend la classe testable sans horloge."""

    def __init__(self, limits: dict | None = None):
        self.limits = limits or dict(DEFAULT_LIMITS)
        self.chains: dict[str, _Chain] = {}
        self._last_send = float("-inf")
        self._hot: set[str] = set()
        self._walking = False
        self._posture_until = 0.0
        self._last_posture = float("-inf")
        self.refused = 0

    # -- état venu du robot ------------------------------------------------

    def set_profile(self, chains: dict) -> None:
        """Profil moteur (déjà validé par load_profile) : le robot vient de
        (re)lancer son script ou de finir un calibrage, il est au repos."""
        self.chains = {name: _Chain(name, cfg) for name, cfg in chains.items()}

    def ranges(self) -> dict:
        """Plages autorisées par chaîne, pour le LLM : {chaîne: {art.: [min, max]}}."""
        return {name: {j: [round(lo, 3), round(hi, 3)] for j, (lo, hi) in c.range.items()}
                for name, c in sorted(self.chains.items())}

    def set_motor_temps(self, temps: dict) -> set[str]:
        """temps : {"head"|"larm"|"rarm": °C}.  Retourne les chaînes trop chaudes."""
        max_temp = self.limits["max_motor_temp"]
        self._hot = {g for g, t in temps.items() if max_temp and t >= max_temp}
        return self._hot

    def set_walking(self, walking: bool) -> None:
        self._walking = walking
        if walking:
            for c in self.chains.values():
                c.target = None

    def _busy(self, now: float) -> bool:
        return self._walking or now < self._posture_until

    # -- ordres ------------------------------------------------------------

    def posture_allowed(self, name: str, now: float) -> tuple[bool, str]:
        """(autorisée ?, raison du refus).  Une posture autorisée bloque les gestes."""
        if not self.limits["postures"]:
            return False, "postures désactivées (jambes)"
        if name not in self.limits["postures"]:
            return False, "hors liste"
        if self._walking:
            return False, "marche en cours"
        if now - self._last_posture < float(self.limits["posture_cooldown"]):
            return False, "délai entre deux postures"
        self._last_posture = now
        self._posture_until = now + float(self.limits["posture_block"])
        for c in self.chains.values():
            c.target = None
        return True, ""

    def move_decision(self, walking: bool, now: float) -> tuple[bool, str]:
        """(envoyer ?, raison du refus) pour un ordre de marche.

        Un ordre à vitesse nulle n'est envoyé que pour arrêter une marche en
        cours : sinon moveToward(0, 0, 0) réveille le moteur de marche pour
        rien, et pendant un changement de posture il fait planter NAOqi.
        """
        if not self.limits["walk"]:
            return False, "marche désactivée (jambes)"
        if now < self._posture_until:
            return False, "changement de posture en cours"
        if not walking and not self._walking:
            return False, "vitesse nulle, le robot ne marche pas"
        self.set_walking(walking)
        return True, ""

    def submit(self, joints, angles, speed: float, now: float) -> int:
        """Ajoute des articulations à la cible de leur chaîne ; retourne le
        nombre d'articulations acceptées (les autres sont comptées dans refused)."""
        if self._busy(now) and not self.limits["joints_while_busy"]:
            self.refused += len(joints)
            return 0
        accepted = 0
        for j, a in zip(joints, angles):
            c = self.chains.get(chain_of(j))
            if c is None or c.name in self._hot:
                self.refused += 1
                continue
            if c.target is None:
                c.target = dict(c.start)
                c.target_speed = c.speed
                c.target_since = now
            c.target[j] = c.clamp(j, float(a))
            if speed > 0:
                c.target_speed = min(c.target_speed, float(speed))
            c.last_order = now
            accepted += 1
        return accepted

    def take_refused(self) -> int:
        n, self.refused = self.refused, 0
        return n

    # -- envoi -------------------------------------------------------------

    def _duration(self, c: _Chain, pose: dict, speed: float) -> float:
        duration = float(self.limits["min_move_time"])
        for j, a in pose.items():
            vmax = JOINT_MAX_VELOCITY.get(j[1:] if j[:1] in ("L", "R") else j, 0.0) \
                * float(self.limits["velocity_scale"])
            if vmax > 0:
                duration = max(duration, abs(a - c.pose[j]) / (max(speed, 0.01) * vmax))
        return duration

    def _plan(self, c: _Chain, now: float) -> None:
        """Transforme la cible en formation en étapes, ou planifie le retour au repos."""
        if c.target is not None:
            if now - c.target_since < float(self.limits["merge_window"]):
                return
            # Déjà au départ (ou en route) : directement la cible.  Sinon, par le départ.
            steps = [] if c.kind == "start" else [("start", c.start, c.speed)]
            c.queue = steps + [("target", c.target, c.target_speed)]
            c.target = None
            return
        if c.queue or c.kind == "rest" or not self.limits["rest_return"]:
            return
        idle_since = max(c.last_order, c.hold_until)
        if now - idle_since >= float(self.limits["rest_delay"]):
            back = [("rest", c.rest, c.speed)]
            c.queue = back if c.kind == "start" else [("start", c.start, c.speed)] + back

    def take(self, now: float) -> list[dict]:
        """Commandes /nao/motion à envoyer maintenant, une par chaîne prête."""
        if now - self._last_send < float(self.limits["min_interval"]):
            return []
        cmds = []
        for name in sorted(self.chains):
            c = self.chains[name]
            if self._busy(now):
                continue
            self._plan(c, now)
            if not c.queue or c.hold_until > now:
                continue
            kind, pose, speed = c.queue.pop(0)
            c.hold_until = now + self._duration(c, pose, speed)
            c.pose = dict(pose)
            c.kind = kind
            cmds.append({"joints": list(c.joints),
                         "angles": [round(pose[j], 4) for j in c.joints],
                         "speed": round(speed, 4)})
        if cmds:
            self._last_send = now
        return cmds
