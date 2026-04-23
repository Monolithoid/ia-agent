# ADR 003 — Ollama direct sans Bitterbot pour body-gateway

## Statut

Adopté

## Contexte

`body-gateway` appelait Bitterbot (`/v1/chat/completions` sur port 19001) pour
générer les actions JSON envoyées aux corps robotiques. En pratique, Bitterbot
retournait une 404 sur cet endpoint — son API n'était pas compatible avec le
format OpenAI attendu.

Bitterbot est une dépendance tierce (image Docker Hub upstream) dont on ne
maîtrise pas le cycle de release ni la surface d'API interne. Le faire pivoter
de la couche "cerveau" au rôle de simple relais vers Ollama introduisait une
brique instable sans valeur ajoutée immédiate.

## Décision

`body-gateway` appelle **Ollama directement** via son API OpenAI-compatible
(`/v1/chat/completions` sur port 11434). Le service `bitterbot-gateway` est
commenté dans `docker-compose.yml` et peut être réactivé à tout moment.

```
Avant : body-gateway → Bitterbot :19001 → Ollama :11434
Après : body-gateway → Ollama :11434
```

Le modèle est configurable via `BRAIN_MODEL` (défaut `qwen2.5:7b`).
L'authentification utilise `BRAIN_API_KEY` (même valeur que `OLLAMA_API_KEYS`).

## Ce qu'on perd

- **Mémoire long-terme** : Bitterbot persiste les conversations en SQLite et
  construit un contexte sur la durée. Sans lui, chaque inférence repart d'un
  contexte vide.
- **Dream Engine** : consolidation mémorielle autonome toutes les 2h. Les ticks
  oniriques (`_dream_loop`) continuent de s'exécuter mais sans mémoire
  persistante entre les cycles.

## Ce qu'on garde

- Le **pipeline complet** de contrôle physique (ROS 2 → NAO / drones).
- Le **chat WebUI** avec le mot de réveil "ok nao".
- La **cohérence du system prompt** JSON — Ollama reçoit exactement le même
  prompt structuré que Bitterbot recevait.
- La possibilité de **réintégrer Bitterbot** ultérieurement quand son API sera
  stabilisée : décommenter `bitterbot-gateway` dans `docker-compose.yml` et
  changer `BRAIN_URL=http://localhost:19001`.

## Conséquences

- `bitterbot-net` reste déclaré dans `docker-compose.yml` mais aucun service
  ne l'utilise activement. Il peut être supprimé si Bitterbot est retiré
  définitivement.
- La variable `BITTERBOT_GATEWAY_TOKEN` n'est plus nécessaire en production
  mais est conservée dans les commentaires pour faciliter la réactivation.
