"""One-time setup for the animal tracker (re-run after editing species.json).

Downloads the two models, converts them to OpenVINO so the tracker can run them on
the otherwise idle Intel iGPU without PyTorch, and precomputes the text embeddings
for every label in species.json:

  * Community Fish Detector (RF-DETR Nano, Apache-2.0) - finds fish in a frame.
  * BioCLIP 2.5 (imageomics/bioclip-2.5-vith14, MIT) - names each detected animal by
    comparing its image embedding with the embeddings of the species names. Chosen over
    BioCLIP 2 after a test on photos degraded to look like this camera (see the README).

Run with the tracker venv:  .venv\\Scripts\\python.exe setup_models.py
"""

import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import open_clip
import openvino as ov
import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
DETECTOR_URL = (
    "https://github.com/filippovarini/community-fish-detector/releases/download/"
    "2026.07.06-release/cfd-rf-detr-nano-640-2026.02.02.cp-011.20260706-release.pth"
)
BIOCLIP = "hf-hub:imageomics/bioclip-2.5-vith14"

# BioCLIP works best with taxonomic + common names; averaging a few phrasings helps.
SPECIES_TEMPLATES = [
    "a photo of {scientific} with common name {common}.",
    "a photo of {common}.",
    "an underwater photo of a {common}.",
]
NEGATIVE_TEMPLATES = ["a photo of {text}.", "an underwater photo of {text}."]


def export_detector():
    checkpoint = MODELS / "cfd-rf-detr-nano-640.pth"
    if not checkpoint.exists():
        print("downloading fish detector ...")
        urllib.request.urlretrieve(DETECTOR_URL, checkpoint)

    from rfdetr import from_checkpoint

    model = from_checkpoint(str(checkpoint))
    resolution = int(model.model.resolution)
    onnx_dir = MODELS / "detector-onnx"
    model.export(output_dir=str(onnx_dir))
    onnx_path = next(onnx_dir.glob("*.onnx"))

    ov_model = ov.convert_model(str(onnx_path))
    ov.save_model(ov_model, MODELS / "detector.xml", compress_to_fp16=True)
    print(f"detector exported ({resolution}px): {[o.get_any_name() for o in ov_model.outputs]}")
    return model, resolution


class NormalizedImageEncoder(torch.nn.Module):
    def __init__(self, clip):
        super().__init__()
        self.clip = clip

    def forward(self, pixels):
        return F.normalize(self.clip.encode_image(pixels), dim=-1)


def export_classifier():
    clip, _, preprocess = open_clip.create_model_and_transforms(BIOCLIP)
    tokenizer = open_clip.get_tokenizer(BIOCLIP)
    clip.eval()

    size = clip.visual.image_size
    size = size[0] if isinstance(size, (tuple, list)) else int(size)
    normalize = next(t for t in preprocess.transforms if type(t).__name__ == "Normalize")

    with torch.no_grad():
        encoder = NormalizedImageEncoder(clip)
        ov_model = ov.convert_model(encoder, example_input=torch.zeros(1, 3, size, size))
        ov.save_model(ov_model, MODELS / "classifier.xml", compress_to_fp16=True)

        spec = json.loads((ROOT / "species.json").read_text(encoding="utf-8"))
        labels, embeddings = [], []

        def embed(prompts):
            e = F.normalize(clip.encode_text(tokenizer(prompts)), dim=-1).mean(dim=0)
            return F.normalize(e, dim=0).numpy()

        for s in spec["species"]:
            templates = SPECIES_TEMPLATES if s["scientific"] else SPECIES_TEMPLATES[1:]
            embeddings.append(embed([t.format(**s) for t in templates]))
            labels.append({"common": s["common"], "scientific": s["scientific"], "category": s["category"],
                           "scene": bool(s.get("scene")), "negative": False})
        for text in spec["negative"]:
            embeddings.append(embed([t.format(text=text) for t in NEGATIVE_TEMPLATES]))
            labels.append({"common": text, "scientific": "", "category": "", "scene": True, "negative": True})

    np.save(MODELS / "label_embeddings.npy", np.stack(embeddings).astype(np.float32))
    meta = {
        "model": BIOCLIP,
        "size": size,
        "mean": [float(v) for v in normalize.mean],
        "std": [float(v) for v in normalize.std],
        "logit_scale": float(clip.logit_scale.exp()),
        "labels": labels,
    }
    (MODELS / "classifier.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"classifier exported ({size}px), {len(labels)} labels")
    return clip, preprocess


if __name__ == "__main__":
    MODELS.mkdir(exist_ok=True)
    if "--classifier-only" not in sys.argv:
        export_detector()
    export_classifier()
    print("done")
