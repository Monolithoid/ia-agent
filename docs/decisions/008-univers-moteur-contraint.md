# ADR 008 — Univers moteur contraint : profil moteur calibré depuis la régie

**Date :** 2026-10-29  
**Statut :** Accepté

## Contexte

La gestion motrice de NAOqi plante dès qu'un mouvement sort de l'ordinaire,
sans raison claire. Jusqu'ici un geste était une liste d'angles cibles : presets
(`wave` mettait les deux épaules à -1.0), angles libres de `motion_engine` à
20 Hz, poses de bras de `cognitive_renderer`, retour à la pose de lancement.
Rien ne garantissait que ces angles et ces trajets étaient sûrs pour ce robot.

## Décision

On définit arbitrairement un univers moteur stable et fortement contraint.

1. **NAO reste assis.** Pas de changement de posture, pas de marche.
2. **Trois chaînes** : la tête, le bras gauche et le bras droit (épaule et coude,
   ni poignet ni main). Pas de danse, pas de coucou avec la main.
3. **Un profil moteur par robot**, `nao_motor_profile.json` sur le robot :
   pour chaque chaîne, une position de repos, une position de départ, une plage
   par articulation (qui contient le départ) et une vitesse.
4. **Chaque geste va repos → départ → cible → départ → repos.** Un nouvel ordre
   pendant un geste enchaîne cible → départ → nouvelle cible. Le trajet départ →
   cible reste dans la plage, puisque celle-ci est une boîte qui contient le départ.
5. **Le LLM donne un ordre bas niveau** : la position d'une ou plusieurs
   articulations, dans les plages qu'on lui montre. Il ne connaît ni le repos ni
   le départ.
6. **Le calibrage se fait depuis la régie** (page `/robot`), chaîne par chaîne :
   raideur coupée pour placer la chaîne à la main, capture du repos et du
   départ, enregistrement des plages, vitesse, rejeu de validation, puis écriture.
   Il tourne sur le robot, sans la latence du VPS.
7. **Le plus de composants NAOqi possible sont coupés** (fall manager,
   anti-collision, respiration, posture d'attente, awareness…), et chacun est
   réactivable depuis la régie. Le fall manager et l'anti-collision des bras
   sont probablement une source de crash.

Répartition, dans la continuité de « le robot est une marionnette » :

| Où | Quoi |
|---|---|
| driver NAO (`motion_policy.py`) | séquences par chaîne, regroupement des ordres, retour au repos, chaleur |
| pont sur le robot (`nao_ws_bridge.py`) | profil, bornes de dernier filet (boîte du profil), repos au démarrage, calibrage, composants |
| `body-gateway` | plages dans le prompt, ordres bas niveau |
| régie | page Robot : calibrage et composants (seule page qui écrit) |

## Conséquences

- Sans profil, NAO ne bouge pas : il faut calibrer chaque chaîne une fois.
- `motion_engine` et les bras de `cognitive_renderer` sont coupés par défaut
  (`MOTION_ENGINE=1`, `RENDERER_ARMS=1` pour les relancer).
- Moins de spontanéité : la variété vient du choix des cibles par le LLM.
  Le retour tactile (débloquer un mouvement, dire « bien » ou « pas bien ») et
  l'apprentissage par démonstration pourront s'ajouter plus tard sur ce socle.
