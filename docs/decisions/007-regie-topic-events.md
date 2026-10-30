# ADR 007 — Régie : body-gateway publie ses flux sur le topic ROS `/events`

**Date :** 2026-10-26  
**Statut :** Accepté

## Contexte

Pendant un stream, on veut voir en direct ce qui passe entre les composants
de la boucle de l'[ADR 005](005-avatar-twitch.md) : quelle entrée est arrivée
(Open WebUI, tchat Twitch, « ok nao », capteur, rêve), ce que le LLM a décidé,
et vers quels corps chaque action est partie (NAO, drone statique, voix OBS).
Aujourd'hui, il faut lire les logs de `body-gateway`.

Contraintes : outils **open source et auto-hébergeables** uniquement, et
l'architecture de l'ADR 005 (`body-gateway` abstrait le LLM, ROS abstrait les
corps).

## Décision

### 1. Un topic ROS standard : `/events`

`body-gateway` est le seul composant qui voit toute la boucle. Il publie sur
`/events` (`std_msgs/String`, un objet JSON par message) :

| `kind` | Quand | Champs |
|---|---|---|
| `input` | une entrée déclenche le LLM | `source` (`webui`, `twitch`, `voice`, `sensor`, `dream`), `body`, `text`, `author` |
| `ignored` | une entrée est abandonnée | `source`, `body`, `text`, `reason` (`wake_word`, `busy`) |
| `wake` | « ok nao » seul entendu, la phrase suivante ira au LLM | `source`, `body`, `text` ; `detector` (`local`), `model` et `score` quand le drone statique l'a détecté lui-même |
| `decision` | le LLM a répondu | `thoughts`, `emotion`, `energy`, `actions`, `duration_s` |
| `action` | une action est routée | `type`, `requested`, `targets`, `connected`, `detail` |
| `error` | erreur du LLM ou du traitement | `message` |
| `bodies` | un corps se (dé)connecte, et toutes les 60 s | `connected` `{corps: bool}`, `state` |

Tous portent `v` (version du format), `t` (heure Unix) et `id` : l'entrée, la
décision et les actions d'une même interaction partagent le même `id`. Le
format est défini et testé dans `robotics/brain/body_gateway/events.py`.

Le topic est un topic ROS comme les autres : `ros2 topic echo /events`, rqt ou
[Lichtblick](https://github.com/lichtblick-suite/lichtblick) (open source, via
rosbridge) le lisent sans rien changer au cœur.

### 2. Une page de régie maison : `regie` (`robotics/regie/`)

Un petit conteneur Python, bâti sur le modèle d'`obs-agent` :

- il se connecte **en sortant** à `rosbridge-proxy` avec `ROSBRIDGE_TOKEN`,
  et ne fait que s'abonner (`/events`, `/cognitive_state`, `/emotion_state`) ;
- il garde les 300 derniers événements, pour une page ouverte en cours de route ;
- il sert `https://regie.$DOMAIN/?token=$REGIE_TOKEN` : un schéma
  entrées → body-gateway → corps animé à chaque événement, l'état du cerveau,
  et la liste des interactions (entrée, pensée du LLM, actions et corps visés,
  corps déconnectés en orange, entrées ignorées) avec un filtre par source ;
- la page reçoit les événements en Server-Sent Events, en HTML et JavaScript
  sans bibliothèque externe.

Le navigateur ne peut pas envoyer l'en-tête `Authorization` exigé par
`rosbridge-proxy` sur un WebSocket : c'est pourquoi la page passe par ce
conteneur plutôt que de parler directement à rosbridge.

## Alternatives écartées

| Option | Pourquoi |
|---|---|
| Foxglove Studio | Outil propriétaire depuis la v2 : écarté, uniquement de l'open source |
| Lichtblick seul | Voit les topics des corps, mais pas l'entrée ni la décision du LLM sans `/events` ; reste branchable en plus de la régie |
| Grafana + Loki / OpenTelemetry | Robuste pour l'historique, mais plusieurs services de plus sur le VPS pour un besoin de suivi en direct |
| Chaque composant publie ses propres événements | Les entrées texte ne passent pas par ROS (ADR 005) ; `body-gateway` voit déjà tout |
| La page parle directement à rosbridge | Pas d'en-tête `Authorization` possible depuis un navigateur ; il faudrait ouvrir rosbridge sans jeton |

## Ce qu'on perd

- **Historique** : seuls les 300 derniers événements sont gardés, en mémoire.
  Un redémarrage de `regie` les efface.
- **Vie privée de la pièce** : le drone statique entend tout ; les phrases sans
  mot de réveil apparaissent dans la régie (`ignored`). La page est protégée
  par `REGIE_TOKEN`.

## Ce qu'on garde

- Le **pattern de connexion sortante** des ADR 002 / 004 / 005.
- Un format **standard ROS** : l'outil de visualisation reste interchangeable.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [ADR 005](005-avatar-twitch.md)
