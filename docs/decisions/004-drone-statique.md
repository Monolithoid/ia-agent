# ADR 004 — Drone statique Raspberry Pi pour les sens, NAO réduit au rendu émotif

**Date :** 2026-10-08  
**Statut :** Accepté

## Contexte

Le NAO est limité comme organe sensoriel : la capture vidéo, la capture audio
et la sortie audio sont de mauvaise qualité. Ses moteurs inférieurs (jambes)
sont déjà désactivés. Il reste en revanche expressif : tête, bras et LEDs
suffisent à transmettre un état émotionnel.

Il faut donc une autre source pour entendre, voir et parler dans la pièce, sans
alourdir l'architecture.

**Principe directeur : des solutions simples, même imparfaites.** Une brique qui
marche tout de suite vaut mieux qu'une brique précise qu'il faudra calibrer.

## Décision

### 1. Répartition des rôles

- **NAO** ne garde **que le rendu émotif moteur** : tête, bras, LEDs.
- **Nouveau type de drone `static`** : un **Raspberry Pi 5** placé dans la même
  pièce assure la **capture audio**, la **capture vidéo** et la **sortie audio**.
- Le Pi se connecte **en sortant** au VPS via rosbridge, sur le même principe
  que le NAO ([ADR 002](002-nao-websocket-bridge.md)), avec le jeton
  `ROSBRIDGE_TOKEN` (en-tête `Authorization: Bearer`, vérifié par
  `rosbridge-proxy`, PR #102).

### 2. Arborescence

```
robotics/
├── brain/            → commun à tous les drones (body_gateway, ros2_bridge, webui)
└── drones/
    ├── nao/          → rendu émotif moteur
    ├── static/       → Raspberry Pi 5 : micros, caméra, haut-parleur
    └── quadcopter/   → réservé
```

### 3. Partage du travail Pi / VPS

| Raspberry Pi (léger) | VPS (lourd) |
|---|---|
| Capture audio et vidéo | Transcription (Whisper STT) |
| Détection de parole | Synthèse vocale (edge-tts) |
| Direction du son | LLM |
| Lecture audio | Analyse d'image |

### 4. Direction du son

Origine commune : **l'axe de la caméra = angle 0**. La méthode est une règle
fixe, choisie selon le matériel branché :

```
si ReSpeaker USB 4 micros  → angle DOAANGLE lu sur l'appareil
                             (360°, précision ~10-20°, annulation d'écho si le
                              haut-parleur est branché sur la sortie jack du ReSpeaker)
sinon si n ≥ 2 micros      → angle du micro le plus fort
                             (angles déclarés en config, ex. MICS="gauche:-60,droite:60")
sinon (1 micro)            → angle du mouvement dans l'image
                             (différence avec l'image précédente, x moyen des pixels changés :
                              angle = (x / largeur − 0.5) × champ_de_vision)
```

Un décalage optionnel `DOA_OFFSET` corrige l'orientation réelle du capteur par
rapport à la caméra.

## Alternatives écartées

| Option | Pourquoi |
|---|---|
| Calcul GCC-PHAT / SRP-PHAT maison | Trop complexe pour le gain : le ReSpeaker fournit déjà un angle, et le cas « micro le plus fort » suffit sinon |
| Fusion audio-vision avec calibration apprise | Calibration à maintenir, gain marginal pour une seule pièce |
| Détection de locuteur actif par les lèvres | Modèle lourd, dépend de l'éclairage et du cadrage, trop complexe pour le gain |

## Ce qu'on perd

- **Capteurs du NAO** : ses micros, sa caméra et son haut-parleur ne sont plus
  utilisés. Les topics `/nao/audio` et `/nao/audio_out` deviennent obsolètes à
  terme au profit des topics du drone `static`.
- **Précision de la direction** : avec un seul micro, on suit le **mouvement**,
  pas le locuteur. Le champ de la caméra (~70°) borne l'angle mesurable, et une
  seule source est suivie à la fois.
- **Autonomie physique** : la perception dépend d'un second appareil dans la
  pièce, plus d'un seul robot.

## Ce qu'on garde

- Le **NAO comme corps expressif** : tête, bras, LEDs (`cognitive_renderer`,
  `motion_engine`).
- Le **pattern de connexion sortante** de l'ADR 002 : aucune règle firewall
  entrante côté robot ou Pi.
- Le **cerveau commun** (`robotics/brain/`) indépendant du type de drone.
- Les **traitements lourds sur le VPS** : le Pi reste remplaçable par n'importe
  quel petit ordinateur.

## Conséquences

- Un nouvel identifiant de corps `static` est à ajouter dans `BODY_REGISTRY` de
  `body-gateway`, avec ses propres topics (audio entrant, image, angle de
  direction, audio sortant).
- Le Pi a besoin de `ROSBRIDGE_TOKEN` dans sa configuration, comme le NAO.
- La configuration du Pi déclare le matériel : présence d'un ReSpeaker, liste
  `MICS`, champ de vision de la caméra, `DOA_OFFSET`.
- Aucun code n'est livré avec cet ADR ; `robotics/drones/static/` sera rempli
  dans un chantier suivant.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Architecture](../architecture.md) · [Robot NAO](../nao.md)
