# Modèle « ok nao » (openWakeWord)

`WAKE_MODE=local` charge `models/ok_nao.onnx` (`WAKE_MODEL=ok_nao`). Ce
fichier n'est pas encore dans le dépôt : il faut l'entraîner une fois avec ce
dossier, puis le copier ici (et le commiter, il pèse ~200 ko).

| Fichier | Rôle |
|---|---|
| `train_ok_nao.ipynb` | Notebook Colab : tout l'entraînement, du début au `.onnx` |
| `generate_clips_fr.py` | Voix de synthèse françaises (edge-tts + Piper), positives et négatives |
| `ok_nao.yml` | Configuration de `openwakeword.train` |

## En attendant : essayer avec un modèle existant

openWakeWord fournit des modèles anglais. Pour tester toute la chaîne tout de
suite, dans le `.env` du drone :

```
WAKE_MODE=local
WAKE_MODEL=hey_jarvis
```

et dire « hey Jarvis » (ou `alexa`, `hey_mycroft`). Le modèle est téléchargé
au premier démarrage. body-gateway ne regarde pas le mot : il reçoit la
phrase qui suit, sans filtre.

## Entraîner « ok nao »

Le plus simple : ouvrir `train_ok_nao.ipynb` dans
[Google Colab](https://colab.research.google.com/) (*Fichier > Importer le
notebook > GitHub*, dépôt `Monolithoid/ia-agent`), choisir un GPU T4 et tout
exécuter. Compter 1 à 2 h. Le dernier bloc télécharge `ok_nao.onnx`.

Ce que fait le notebook, pour le refaire ailleurs (PC Linux avec GPU) :

1. **Voix françaises** : le générateur d'openWakeWord ne parle qu'anglais,
   `generate_clips_fr.py` le remplace :

   ```bash
   pip install edge-tts piper-tts scipy numpy     # + ffmpeg (apt)
   python generate_clips_fr.py --out sortie/ok_nao --n-train 3000 --n-test 300
   ```

   - **positifs** : « ok nao » écrit de plusieurs façons (« OK Nao. »,
     « Ok, Nao ! », « okay nao »…) ;
   - **négatifs** : mots proches (« ok », « nao », « le Nao », « ok non »,
     « cacao », « chaos », « ok Google »…) et phrases courantes : le modèle
     apprend à ne pas se déclencher dessus ;
   - **voix** : toutes les voix fr-* d'edge-tts (France, Canada, Belgique,
     Suisse) avec débit et hauteur tirés au hasard, et les voix Piper
     françaises (dont `fr_FR-mls-medium` et ses 125 locuteurs) avec durée et
     bruit variés. Les clips de test viennent de voix absentes de
     l'entraînement. `--no-edge` pour travailler hors ligne (Piper seul).

2. **Augmentation** : réverbération (réponses impulsionnelles MIT), bruits de
   fond (AudioSet, musique FMA), puis calcul des caractéristiques :
   `python -m openwakeword.train --training_config ok_nao.yml --augment_clips`.
   `train.py` importe toujours `generate_samples` : un module vide dans
   `./stub` suffit (ne jamais passer `--generate_clips`).

3. **Entraînement** : `--train_model`, avec ~2000 h de négatifs précalculés
   (ACAV100M) et ~11 h de validation des faux déclenchements, fournis par
   openWakeWord sur Hugging Face. Sortie : `sortie/ok_nao.onnx`. La conversion
   en `.tflite` qui suit peut échouer sans TensorFlow : le drone n'utilise que
   l'ONNX.

## Améliorer le modèle

- **Trop de réveils intempestifs** : monter `WAKE_THRESHOLD` sur le drone
  (0,6, 0,7…) ; à l'entraînement, baisser `target_false_positives_per_hour`
  ou ajouter les mots fautifs à `NEGATIVE` dans `generate_clips_fr.py`.
- **« ok nao » manqué** : regarder le score avec `LOG_LEVEL=DEBUG` (ligne
  `Score « ok_nao » max sur 1 s`) et baisser `WAKE_THRESHOLD` ; à
  l'entraînement, plus de clips (`--n-train 6000`) ou `layer_size: 64`.
- **Vraies voix** : quelques dizaines d'enregistrements de « ok nao » par les
  personnes de la maison, dans la pièce, en WAV 16 kHz mono, ajoutés à
  `sortie/ok_nao/positive_train` avant `--augment_clips`, aident beaucoup.
