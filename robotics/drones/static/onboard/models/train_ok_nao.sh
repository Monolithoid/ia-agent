#!/usr/bin/env bash
# train_ok_nao.sh — Entraîne le modèle « ok nao » (openWakeWord) sans Jupyter,
# sur Ubuntu ou WSL2. Mêmes étapes que train_ok_nao.ipynb, dans le terminal.
#
# Usage :
#   bash train_ok_nao.sh [dossier_de_travail]      (défaut : ~/ok-nao)
#
# Relançable : chaque étape déjà faite est sautée. Compter ~30 Go de disque
# et 1 à 2 h avec un GPU NVIDIA (beaucoup plus sur processeur seul).
# Résultat : <dossier>/sortie/ok_nao.onnx, à copier dans models/ sur le drone.

set -euo pipefail

WORK="${1:-$HOME/ok-nao}"
REPO_URL="https://github.com/Monolithoid/ia-agent.git"
HF="https://huggingface.co/datasets"

step() { echo; echo "==== $* ===="; }
mkdir -p "$WORK" && cd "$WORK"
echo "Dossier de travail : $WORK"

step "1/7 Paquets système"
missing=()
for p in git python3-venv ffmpeg wget; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    sudo apt-get update -qq && sudo apt-get install -y "${missing[@]}"
fi
echo "ok"

step "2/7 Code et environnement Python (.venv)"
[[ -d ia-agent ]] || git clone --depth 1 "$REPO_URL"
[[ -d openWakeWord ]] || git clone --depth 1 https://github.com/dscripka/openWakeWord.git
[[ -x .venv/bin/python ]] || python3 -m venv .venv
PY="$WORK/.venv/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q "torch<2.9" "torchaudio<2.9" soundfile   # 2.9 exige torchcodec pour lire les WAV
"$PY" -m pip install -q --no-deps ./openWakeWord
"$PY" -m pip install -q onnxruntime onnx onnxscript "scipy<1.15" scikit-learn tqdm requests pyyaml \
    mutagen torchinfo torchmetrics speechbrain==0.5.14 audiomentations torch-audiomentations \
    acoustics pronouncing "datasets<3" soundfile edge-tts piper-tts
mkdir -p stub
echo "def generate_samples(*a, **k): raise SystemExit('clips : generate_clips_fr.py')" > stub/generate_samples.py
cp ia-agent/robotics/drones/static/onboard/models/ok_nao.yml .
"$PY" -c "import torch; print('GPU :', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'aucun (processeur seul, ce sera long)')"

step "3/7 Voix de synthèse françaises (« ok nao » + mots proches)"
if [[ $(ls sortie/ok_nao/positive_train 2>/dev/null | wc -l) -lt 2800 ]]; then
    "$PY" -u ia-agent/robotics/drones/static/onboard/models/generate_clips_fr.py \
        --out sortie/ok_nao --n-train 3000 --n-test 300
else
    echo "déjà fait"
fi

step "4/7 Réverbération de pièces (MIT)"
if [[ ! -f mit_rirs/.fait ]]; then
    mkdir -p mit_rirs
    "$PY" - <<'EOF'
import numpy as np, scipy.io.wavfile, datasets
from tqdm import tqdm
ds = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True)
for i, row in enumerate(tqdm(ds, desc="RIR")):
    scipy.io.wavfile.write(f"mit_rirs/{i}.wav", 16000, (row["audio"]["array"] * 32767).astype(np.int16))
EOF
    touch mit_rirs/.fait
else
    echo "déjà fait"
fi

step "5/7 Bruits de fond (AudioSet, bruits fabriqués, musique FMA facultative)"
# 2000 extraits AudioSet (version figée du dépôt Hugging Face : les fichiers
# ont changé de place depuis) et bruits blanc, rose et brun fabriqués ici.
# Des enregistrements de l'ambiance réelle de la pièce (WAV 16 kHz mono)
# ajoutés dans fma/ aident beaucoup.
if [[ ! -f audioset_16k/.fait ]]; then
    mkdir -p audioset audioset_16k
    if wget -q --show-progress -c -O audioset/bal_train01.tar \
        "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/196c0900867eff791b8f4d4be57db277e9a5b131/bal_train01.tar"; then
        tar -xf audioset/bal_train01.tar -C audioset
        find audioset -name "*.flac" | while read -r f; do
            ffmpeg -nostdin -loglevel error -y -i "$f" -ac 1 -ar 16000 "audioset_16k/$(basename "${f%.flac}").wav"
        done
    else
        echo "AudioSet indisponible : bruits fabriqués seulement"
    fi
    "$PY" - <<'PYEOF'
import numpy as np, scipy.io.wavfile, os
os.makedirs("audioset_16k", exist_ok=True)
rng = np.random.default_rng(0)
for i in range(150):
    n = rng.standard_normal(16000 * 30)
    f = np.fft.rfft(n); k = np.arange(1, len(f) + 1)
    f /= k ** rng.choice([0, 0.5, 1.0])          # bruit blanc, rose ou brun
    x = np.fft.irfft(f, len(n)); x = x / np.abs(x).max() * rng.uniform(0.05, 0.5)
    scipy.io.wavfile.write(f"audioset_16k/bruit_{i}.wav", 16000, (x * 32767).astype(np.int16))
PYEOF
    touch audioset_16k/.fait
fi
echo "Bruits : $(ls audioset_16k | wc -l) fichiers"
if [[ ! -f fma/.fait ]]; then
    mkdir -p fma
    # Musique facultative : sans elle (streaming FMA cassé selon la version de
    # datasets), les bruits ci-dessus suffisent.
    "$PY" - <<'EOF' || echo "FMA ignoré"
import numpy as np, scipy.io.wavfile, datasets
from tqdm import tqdm
ds = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
ds = iter(ds.cast_column("audio", datasets.Audio(sampling_rate=16000)))
for i in tqdm(range(120), desc="FMA"):
    row = next(ds)
    scipy.io.wavfile.write(f"fma/{i}.wav", 16000, (row["audio"]["array"] * 32767).astype(np.int16))
EOF
    touch fma/.fait
fi
echo "FMA : $(ls fma | wc -l) fichiers"

step "6/7 Négatifs précalculés (~17 Go) et validation"
for f in openwakeword_features_ACAV100M_2000_hrs_16bit.npy validation_set_features.npy; do
    wget -q --show-progress -c "$HF/davidscripka/openwakeword_features/resolve/main/$f"
done

step "7/7 Augmentation puis entraînement"
"$PY" -m openwakeword.train --training_config ok_nao.yml --augment_clips
# La conversion finale en .tflite échoue sans TensorFlow : seul le .onnx sert.
"$PY" -m openwakeword.train --training_config ok_nao.yml --train_model || true

if [[ -f sortie/ok_nao.onnx ]]; then
    step "Essai sur les clips de test"
    "$PY" - <<'EOF'
import glob, openwakeword.utils
from openwakeword.model import Model
openwakeword.utils.download_models(["ok_nao"])
m = Model(wakeword_models=["sortie/ok_nao.onnx"], inference_framework="onnx")
for kind in ("positive_test", "negative_test"):
    clips = glob.glob(f"sortie/ok_nao/{kind}/*.wav")[:200]
    hits = sum(max(p["ok_nao"] for p in m.predict_clip(c)) >= 0.5 for c in clips)
    print(f"{kind} : {hits}/{len(clips)} au-dessus de 0,5 (idéal : tous en positif, aucun en négatif)")
EOF
    echo
    echo "Modèle prêt : $WORK/sortie/ok_nao.onnx"
    echo "À copier sur le drone dans robotics/drones/static/onboard/models/ok_nao.onnx"
else
    echo "Pas de sortie/ok_nao.onnx : voir les erreurs ci-dessus." >&2
    exit 1
fi
