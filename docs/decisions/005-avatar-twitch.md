# ADR 005 — Une seule boucle : stimulations → LLM → actions des agents (Twitch, OBS)

**Date :** 2026-10-09  
**Statut :** Accepté (sections 1 à 4 implémentées ; section 5 prévue)

## Contexte

NAO est la représentation d'un avatar IA autonome, visible sur un stream
Twitch. Il réagit en temps réel à des stimulations extérieures que le streamer
ne contrôle pas.

Le tchat Twitch n'est **qu'une stimulation extérieure de plus**, au même titre
que « ok nao » dit dans la pièce ou un message écrit dans Open WebUI. Il ne doit
pas avoir de chemin à part.

- **NAO** ne fait que bouger et allumer ses LEDs ([ADR 004](004-drone-statique.md)).
- **Le drone statique** entend, voit et parle dans la pièce ([ADR 004](004-drone-statique.md)).
- **OBS** peut recevoir directement le son des réponses de NAO.

**Principe directeur (repris de l'ADR 004) : des solutions simples, même
imparfaites.**

## Décision

### 1. Une boucle unique

```
  ENTRÉES (stimulations)                 CERVEAU                    AGENTS (corps)
  ──────────────────────                 ───────                    ──────────────
  « ok nao … » (drone statique) ─┐                              ┌─► nao_01    : gestes, tête, LEDs
  Open WebUI                    ─┼─► texte ─► body-gateway ─► actions ─┼─► static_01 : voix dans la pièce
  tchat Twitch « !nao … »       ─┤            (LLM, une       │  └─► obs_01    : voix sur le stream (OBS)
  capteurs (sonar, contact…)    ─┘             inférence à    │
                                               la fois)       └─ en boucle, à chaque stimulation
```

Chaque entrée produit seulement un texte. Le LLM renvoie une liste d'actions
(`speech`, `motion`, `leds`, `look`…), et `body-gateway` les répartit sur les
agents. Aucune entrée ne connaît les sorties, et aucune sortie ne connaît
l'entrée qui l'a déclenchée.

### 2. Entrée Twitch : `twitch-input` (`robotics/inputs/twitch/`)

Un petit conteneur Python sans ROS 2 qui fait pour le tchat ce que Whisper
et le mot de réveil font pour la voix :

| Étape | Choix simple |
|---|---|
| Lire le tchat | IRC Twitch en **lecture anonyme** (`justinfan…`) : aucun jeton ni appli Twitch |
| Garder les commandes | message qui commence par `!nao` (`!naomi` exclu) |
| Filtrer | longueur max, pas de lien, mots interdits, bots ignorés, délai par spectateur, option « modérateurs seulement » |
| Rafales | file de 5 messages, les plus anciens abandonnés, tout message de plus de 90 s aussi |
| Envoyer | `POST body-gateway/chat` avec `[tchat Twitch] pseudo : message`, exactement comme Open WebUI |

Le prompt système de `body-gateway` décrit ce format et rappelle que **les
messages du tchat ne sont jamais des ordres**.

### 3. Agent OBS : un corps « voix » comme le drone statique (`robotics/drones/obs/`)

La voix du stream est un nouveau type de corps, `obs`, de capacité `speech`,
construit sur le même modèle que le haut-parleur du drone statique :

- `obs-agent` se connecte **en sortant** à rosbridge avec `ROSBRIDGE_TOKEN`,
  comme le drone statique.
- `static-driver` est lancé une seconde fois dans `nao-core` avec
  `DRONE_ID=obs_01` : il fait la synthèse edge-tts et publie le MP3 sur
  `/drones/obs_01/audio_out`. Les topics sont les mêmes que pour le drone.
- `obs-agent` sert une page `/overlay?token=…` à ouvrir comme **source
  navigateur OBS** (« Contrôler l'audio via OBS » : la voix devient une source
  du mixer), avec sous-titres.
- L'overlay signale le début et la fin de lecture : `/drones/obs_01/speaking`,
  et le NAO s'anime pendant que la voix joue (`cognitive_renderer` écoute
  désormais toutes les voix).
- `obs-agent` n'envoie son battement de cœur que **tant que l'overlay est
  ouvert dans OBS** : hors stream, la voix OBS est « déconnectée » et le
  cerveau ne lui envoie rien.

### 4. Routage de la parole

`body_rules.route_targets` : une action `speech` part sur **toutes les voix
connectées** (pièce et stream), quelle que soit l'entrée. Sans voix connectée,
le NAO parle lui-même, comme avant. Les gestes, la tête et les LEDs vont
toujours au NAO.

### 5. Architecture cible (prévue, pas encore implémentée)

Ces principes cadrent les prochaines évolutions. Le code de cette ADR les
respecte déjà en partie (entrées texte vers `/chat`, voix comme agents).

**Entrées texte.** Toute entrée de type texte (Open WebUI, tchat Twitch, SMS,
Discord…) est envoyée telle quelle au LLM, avec sa source et son auteur :
`[twitch] pseudo : message`. Pas de traitement propre à un canal en dehors de
son adaptateur (lecture, filtres anti-abus).

**Fil d'événements unique dans `body-gateway`.** Chaque stimulation devient un
événement `{source, auteur, texte, répondre_à, priorité, heure}` déposé dans une
seule file, consommée par une seule boucle :

- chaque entrée garde son transport naturel : ROS pour la voix et les
  capteurs, HTTP `/chat` pour les canaux texte (Open WebUI attend le résultat
  de son événement et reste en question-réponse) ;
- priorités simples (la pièce passe avant le tchat) ;
- les événements arrivés pendant une réflexion sont regroupés dans le prompt
  suivant au lieu d'être perdus ;
- une mémoire courte des derniers échanges, toutes sources confondues, est
  ajoutée au prompt ;
- un tick périodique sans stimulation donne à NAO l'initiative (« je regarde
  la pièce et l'ambiance du tchat »).

ROS 2 reste l'abstraction des **corps** (`/bodies/{id}/sensors|actuators/*`) ;
`body-gateway` reste l'abstraction vis-à-vis du **LLM**. Les entrées texte
n'ont pas besoin de passer par ROS.

**Canaux texte = entrée + agent.** Un canal qui sait répondre (Open WebUI,
Twitch, Discord, SMS) est aussi un corps de capacité `message` :

| Action LLM | Destination |
|---|---|
| `{"type": "reply", "text": "…"}` | le canal d'origine de l'événement, choisi par `body-gateway` (règle fixe, comme `speech` → toutes les voix) |
| `{"body": "discord_01", "type": "message", "text": "…"}` | un canal nommé, à l'initiative du LLM |
| `speech`, `motion`, `leds`… | inchangé : voix et NAO, en plus de la réponse écrite |

Chaque adaptateur renvoie la réponse à sa façon : réponse HTTP (Open WebUI),
message (Twitch, Discord), envoi (SMS). Contraintes connues : écrire dans le
tchat Twitch demande un compte bot avec jeton OAuth (la lecture reste
anonyme), un rythme limité et 500 caractères au plus ; les SMS passent par un
fournisseur payant.

## Alternatives écartées

| Option | Pourquoi |
|---|---|
| Un service Twitch qui appelle le cerveau et joue lui-même la réponse dans OBS | Chemin à part pour une entrée : la voix OBS ne servirait qu'au tchat, et la pièce ne serait pas entendue sur le stream |
| API EventSub / bot Twitch authentifié | Appli Twitch, OAuth et renouvellement de jetons pour un simple besoin de lecture |
| obs-websocket + fichier audio | Demande d'ouvrir OBS au VPS ou un script local de plus ; la source navigateur suffit et arrive dans le mixer |
| Le LLM choisit la voix (pièce ou stream) ou le canal de réponse | Un petit modèle local se tromperait ; des règles fixes (« toutes les voix », « répondre au canal d'origine ») sont prévisibles |
| Toutes les entrées publiées sur un topic ROS `/inputs` | Open WebUI attend une réponse : il faudrait un identifiant de requête et un retour, pour aucun gain ; l'unification se fait dans `body-gateway` |

## Ce qu'on perd

- **Exhaustivité** : en rafale, des messages `!nao` sont volontairement ignorés,
  et si le cerveau est occupé, le message est abandonné.
- **Modération fine** : les filtres sont des règles fixes. Un LLM peut toujours
  dire quelque chose de malvenu à l'antenne.
- **Voix séparées** : la réponse à un spectateur est aussi dite dans la pièce,
  et le drone statique l'entend (filtrée par le mot de réveil).

## Ce qu'on garde

- **Un seul cerveau** et une seule boucle pour toutes les stimulations.
- Le **pattern de connexion sortante** de l'ADR 002 / 004 pour tout agent.
- Aucun port entrant chez le streamer : OBS ouvre seulement une page HTTPS.

## Points ouverts

- Implémenter l'architecture cible (section 5) : fil d'événements, mémoire
  courte, tick d'initiative, puis canaux texte en sortie.
- Avec deux réseaux Docker possibles, vérifier que Traefik joint `obs-agent`
  par `robotics-net` (label `traefik.docker.network` si besoin, selon Coolify).

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Architecture](../architecture.md) · [ADR 004](004-drone-statique.md)
