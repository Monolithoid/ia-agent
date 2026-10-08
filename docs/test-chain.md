# Test de la chaîne de déploiement

Script de diagnostic maillon par maillon pour valider la chaîne complète **WebUI → NAO**. Chaque test peut être exécuté avec le robot éteint : `naoqi-bridge` fonctionne en mode mock quand NAO n'est pas connecté.

---

## Lancer les tests

```bash
# Depuis la racine du dépôt, sur le VPS
bash tests/test-chain.sh
```

La sortie indique `✓ PASS`, `✗ FAIL` ou `⊘ SKIP` pour chaque vérification. Le script termine avec un code de sortie non nul si au moins un maillon est en échec.

---

## Chaîne testée

```
open-webui (nao_pipe.py)
  │  POST http://chat-relay:8095/chat     ← Maillon 1 (ai-network)
  ▼
chat-relay (Caddy)
  │  reverse-proxy → host-gateway:8095   ← Maillon 2
  ▼
body-gateway  (host network, :8095)
  │  POST http://localhost:11434/v1/chat/completions   ← Maillon 3
  ▼
ollama  (:11434, port exposé sur le host)
  │  → réponse JSON { thoughts, emotion, actions }     ← Maillon 3b
  ▼
body-gateway  (publie sur ROS 2 DDS domain 42)
  │  /bodies/nao_01/actuators/*          ← Maillon 4
  ▼
nao-core/naoqi_driver  (host network, DDS 42)
  │  POST http://localhost:8090/say|motion|leds|…     ← Maillon 5
  ▼
naoqi-bridge  (:8090, exposé sur 127.0.0.1)
  │  qi SDK → tcp://NAO_IP:9559          ← Maillon 6
  ▼
NAO  (éteint → réponse mock)
```

Maillon 9, drone statique simulé ([ADR 004](decisions/004-drone-statique.md)) :

```
tests/fake-static-drone.py  (faux drone, WebSocket ws://localhost:9090)
  │  /drones/static_01/status, image, speaking, audio
  ▼
ros2-bridge → nao-core/static_driver → whisper-asr
  ▼
body-gateway  (mot de réveil, direction, tête du NAO, routage de la parole)
  ▼
nao-core/static_driver → edge-tts → /drones/static_01/audio_out
```

> **n8n** est dans `ai-network` et peut appeler `body-gateway` directement, mais il n'est **pas** dans la chaîne de chat décrite ici — voir [Maillon 7](#maillon-7--n8n-hors-chaîne-directe).

---

## Prérequis

- `docker` disponible sur le VPS
- Les services cibles démarrés (`docker compose up -d`)
- `curl` et `python3` installés (présents par défaut sur Ubuntu)
- NAO peut être éteint pour tous les maillons sauf le 6 (mock attendu)

---

## Maillons

### Maillon 0 — Conteneurs Docker

Vérifie que les conteneurs requis sont dans l'état `running`.

| Conteneur | Rôle | Obligatoire |
|---|---|---|
| `open-webui` | Interface de chat | Oui |
| `chat-relay` | Proxy Caddy WebUI ↔ body-gateway | Oui |
| `body-gateway` | Cerveau ROS 2 + endpoint HTTP | Oui |
| `naoqi-bridge` | Bridge HTTP ↔ SDK NAOqi | Oui |
| `nao-core` | naoqi_driver + cognitive_renderer + motion_engine | Oui |
| `ollama` | Moteur LLM | Recommandé |
| `chromadb` | Mémoire vectorielle | Facultatif |
| `n8n` | Orchestration workflows | Facultatif |
| `whisper-asr` | Transcription vocale | Facultatif |
| `ros2-bridge` | WebSocket rosbridge | Facultatif |

> Voir [ia-stack.md](ia-stack.md) pour la description complète de chaque service IA et [bridge-ia-ros2-nao.md](bridge-ia-ros2-nao.md) pour les services robotiques.

---

### Maillon 1 — open-webui → chat-relay (ai-network)

**Ce qui est testé :** le Pipe `nao_pipe.py` installé dans Open WebUI appelle `POST http://chat-relay:8095/chat`. Ce test vérifie que la résolution DNS fonctionne et que le port TCP est accessible depuis le conteneur `open-webui` sur le réseau `ai-network`.

**Succès :** `chat-relay` répond HTTP 400 (`{"error":"missing text"}`) — body-gateway reçoit la requête et rejette un body vide. C'est la preuve que le proxy est traversé correctement.

**Référence :** [ia-stack.md — NAO Brain](ia-stack.md#nao-brain--chat-webui-comme-interface-du-cerveau-robotique)

```bash
# Test manuel équivalent (depuis le conteneur open-webui)
docker exec open-webui curl -sf -X POST http://chat-relay:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"","robot_id":"nao_01"}'
# Attendu : {"error":"missing text"} ou code 400
```

---

### Maillon 2 — chat-relay → body-gateway (host-gateway:8095)

**Ce qui est testé :** le Caddy de `chat-relay` proxifie les requêtes vers `host-gateway:8095`, l'alias Docker qui pointe vers le réseau hôte où `body-gateway` écoute. Ce maillon valide que le passage `ai-network → host network` fonctionne.

**Succès :** `POST http://localhost:8095/chat` répond depuis le host, et la même requête depuis `open-webui` via `chat-relay` donne le même résultat.

**Référence :** [ia-stack.md — Réseaux Docker](ia-stack.md#réseaux-docker)

```bash
# Test manuel depuis le host (body-gateway en network_mode: host)
curl -sf -X POST http://localhost:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"","robot_id":"nao_01"}'
# Attendu : {"error":"missing text"}
```

---

### Maillon 3 — body-gateway → Ollama (localhost:11434)

**Ce qui est testé :** `body-gateway` tourne en `network_mode: host` et appelle Ollama sur `http://localhost:11434` (Ollama expose son port 11434 sur l'hôte). Deux sous-tests :

1. `GET /api/tags` — Ollama répond et le modèle configuré dans `BRAIN_MODEL` est présent
2. `POST /v1/chat/completions` — l'endpoint OpenAI-compatible répond avec un champ `choices`

**Succès :** Ollama répond, le modèle est listé, et `/v1/chat/completions` retourne une réponse valide.

**Référence :** [ia-stack.md — Ollama](ia-stack.md#ollama--moteur-llm) · [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md)

```bash
# Vérifier les modèles disponibles
curl -sf http://localhost:11434/api/tags | python3 -c \
  "import json,sys; [print(m['name']) for m in json.load(sys.stdin)['models']]"

# Test inférence directe (adapter le modèle si besoin)
curl -sf -X POST http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3:1.7b","messages":[{"role":"user","content":"ok"}],"stream":false}'
```

---

### Maillon 3b — Inférence complète via body-gateway

**Ce qui est testé :** un vrai texte traversant `body-gateway` jusqu'à Ollama et retour — le même chemin qu'un message "ok nao" depuis le chat. La réponse doit contenir les champs `speech`, `emotion`, `thoughts` et `actions`.

**Succès :** `{"speech":"...","emotion":"...","thoughts":"...","actions":[...]}` — Ollama a généré un JSON structuré et body-gateway l'a parsé correctement.

**Durée :** jusqu'à 60 s (inférence LLM sur CPU).

```bash
curl -sf --max-time 60 -X POST http://localhost:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"bonjour","robot_id":"nao_01"}'
```

---

### Maillon 4 — body-gateway → ROS 2 (DDS domain 42)

**Ce qui est testé :** après une inférence, `body-gateway` publie sur les topics ROS 2 `/cognitive_state`, `/emotion_state` et `/bodies/nao_01/actuators/*`. Ce test vérifie que le bus DDS est actif et que les topics sont visibles depuis `naoqi-driver`.

**Succès :** `ros2 topic list` retourne `/cognitive_state` et au moins un topic `/bodies/…`.

**Référence :** [bridge-ia-ros2-nao.md — Topics ROS 2](bridge-ia-ros2-nao.md#topics-ros-2)

```bash
# Lister les topics actifs (DDS domain 42)
docker exec nao-core bash -c \
  "source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 ros2 topic list"

# Vérifier qu'il y a bien un publisher sur /cognitive_state
docker exec nao-core bash -c \
  "source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 ros2 topic info /cognitive_state"
```

---

### Maillon 5 — nao-core → naoqi-bridge (localhost:8090)

**Ce qui est testé :** `nao-core/naoqi_driver` reçoit les topics ROS 2 et les traduit en appels HTTP vers `naoqi-bridge` sur `localhost:8090`. Ce maillon valide que le bridge Flask est démarré, accessible, et répond aux commandes.

**Succès :** `/health` retourne `{"ok": true}`, `/say` et `/leds` retournent `{"ok": true}` (en mode mock ou réel).

**Référence :** [nao.md — Deux scripts, deux rôles](nao.md#deux-scripts-deux-rôles)

```bash
# Santé du bridge
curl -sf http://localhost:8090/health

# Test say (mock si NAO éteint)
curl -sf -X POST http://localhost:8090/say \
  -H "Content-Type: application/json" \
  -d '{"text":"test"}'

# Test LEDs (mock si NAO éteint)
curl -sf -X POST http://localhost:8090/leds \
  -H "Content-Type: application/json" \
  -d '{"group":"AllLeds","r":0.0,"g":1.0,"b":0.0,"duration":0.5}'
```

---

### Maillon 6 — naoqi-bridge en mode mock (NAO éteint)

**Ce qui est testé :** avec NAO éteint, `naoqi-bridge` doit tourner en **mode mock** : il répond `{"ok": true}` à toutes les commandes mais les imprime dans ses logs plutôt que de les envoyer au robot. Le champ `connected` dans `/health` doit être `false`.

**Succès :** `{"ok": true, "connected": false}` et les logs contiennent la mention `MOCK`.

**Référence :** [nao.md — Dépannage](nao.md#dépannage)

```bash
# Vérifier l'état de connexion
curl -sf http://localhost:8090/health
# Attendu : {"ok": true, "nao_ip": "...", "connected": false}

# Vérifier le mode mock dans les logs
docker logs --tail=50 naoqi-bridge 2>&1 | grep -i mock
# Attendu : "[naoqi_bridge] MOCK mode active — no NAO connection."
```

Quand NAO est rallumé et reconnecté, relancer ce maillon : `connected` passera à `true`.

---

### Maillon 7 — n8n (hors chaîne directe)

**Ce qui est testé :** n8n est dans `ai-network` et son port 5678 est lié à `127.0.0.1`. Ce maillon vérifie simplement qu'il est démarré.

> **Important :** n8n n'est **pas** dans la chaîne de chat directe. La route `nao_pipe.py → chat-relay → body-gateway` ne passe pas par n8n. N8N est un outil d'orchestration **parallèle** : il peut déclencher des actions sur `body-gateway` via ses propres workflows, indépendamment du chat WebUI.

**Référence :** [ia-stack.md — n8n](ia-stack.md#n8n--orchestrateur-de-workflows)

```bash
# Accès via tunnel SSH uniquement
ssh -L 5678:localhost:5678 $VPS_USER@$DOMAIN
# puis ouvrir http://localhost:5678
```

### Maillon 9 — Drone statique simulé (faux messages rosbridge)

Lance `tests/fake-static-drone.py`, qui joue le rôle du Raspberry Pi : il se
connecte à rosbridge comme `static_drone.py` et vérifie chaque relais. Aucun
matériel n'est nécessaire, mais il faut `whisper-asr`, Ollama et l'accès
internet d'edge-tts.

| Étape | Envoyé par le faux drone | Attendu |
|---|---|---|
| 1 | `/drones/static_01/status` | `/bodies/static_01/connected` = `true` |
| 2 | `/drones/static_01/speaking` = `true` | `/bodies/static_01/sensors/speaking` = `true` |
| 3 | `/drones/static_01/image` | `/bodies/static_01/sensors/image` (même JSON) |
| 4 | `/bodies/static_01/actuators/say` « OK Nao, bonjour… » | MP3 sur `/drones/static_01/audio_out` |
| 5 | `/drones/static_01/audio` (ce MP3, angle -30°) | `sound_direction`, `HeadYaw` sur `/bodies/nao_01/actuators/motion`, texte sur `/bodies/static_01/sensors/speech` |
| 6 | — | réponse du LLM prononcée sur `/drones/static_01/audio_out` |

```bash
# Seul, sans le reste de test-chain.sh
python3 tests/fake-static-drone.py
# Par le chemin public (Traefik + rosbridge-proxy), avec le jeton
python3 tests/fake-static-drone.py --url ws://rosbridge.$DOMAIN --token "$ROSBRIDGE_TOKEN"
```

Si un vrai drone `static_01` est branché pendant le test, il prononce les
phrases du test et le NAO tourne la tête.

---

## Résultats attendus (tout NAO éteint)

| Maillon | Résultat attendu |
|---|---|
| 0 | Tous les conteneurs requis en `running` |
| 1 | HTTP 400 `missing text` depuis open-webui vers chat-relay |
| 2 | HTTP 400 `missing text` depuis le host sur :8095 |
| 3 | Ollama répond, modèle `BRAIN_MODEL` listé, `/v1/chat/completions` OK |
| 3b | JSON `{speech, emotion, thoughts, actions}` retourné |
| 4 | `/cognitive_state` et `/bodies/nao_01/…` visibles sur DDS 42 |
| 5 | `naoqi-bridge /health` et `/say` retournent `{"ok": true}` |
| 6 | `connected: false` dans `/health`, logs `MOCK` présents |
| 7 | n8n accessible sur localhost:5678 (si démarré) |
| 9 | Toutes les étapes du faux drone statique en `PASS` (si edge-tts est injoignable, l'étape 4 échoue et les étapes 5 et 6 sont sautées) |

---

## Dépannage

| Symptôme | Maillon | Cause probable | Solution |
|---|---|---|---|
| `container X: NOT running` | 0 | Service non démarré | `docker compose up -d <service>` |
| `Could not resolve host: chat-relay` | 1 | open-webui hors de `ai-network` | Vérifier `networks:` dans `docker-compose.yml` |
| `Connection refused` sur :8095 depuis open-webui | 1–2 | chat-relay non démarré | `docker compose up -d chat-relay` |
| `Connection refused` sur localhost:8095 | 2 | body-gateway crashé | `docker logs body-gateway` |
| `model not found` dans Ollama | 3 | Modèle non téléchargé | `docker exec -it ollama ollama pull qwen3:1.7b` |
| Timeout sur `/v1/chat/completions` | 3 | Modèle non chargé ou CPU saturé | Attendre le chargement initial (~30 s) |
| JSON parse error dans body-gateway | 3b | Ollama ne retourne pas du JSON strict | Vérifier le system prompt `_AWAKE_SYSTEM` dans `body_gateway.py` |
| Topics ROS 2 absents | 4 | DDS domain incorrect ou body-gateway/nao-core crashé | `docker logs body-gateway` ; `docker logs nao-core` ; vérifier `ROS_DOMAIN_ID=42` |
| `Connection refused` sur localhost:8090 | 5 | naoqi-bridge non démarré | `docker compose up -d naoqi-bridge` |
| `connected: true` avec NAO éteint | 6 | Ancienne connexion TCP non fermée | Redémarrer `naoqi-bridge` |
| `/bodies/static_01/connected` jamais vu | 9 | static-driver non démarré | `docker logs nao-core` : ligne `static-driver(…) pour static_01` ? `STATIC_DRONE_ID` vide ? |
| Pas de `HeadYaw` | 9 | `static_01` absent de `BODY_REGISTRY` | Vérifier l'environnement de body-gateway |
| Pas de réponse prononcée | 9 | Mot de réveil absent de la transcription, ou LLM lent | `docker logs body-gateway` (`Sans mot de réveil, ignoré`) |
| Pas de ligne `MOCK` dans les logs | 6 | Le SDK qi a été trouvé et NAO_IP est défini | Normal si NAO était allumé lors du démarrage |
