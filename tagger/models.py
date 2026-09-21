from __future__ import annotations

import csv
from dataclasses import dataclass

import cv2
import numpy as np
import onnx
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from PIL import Image

# Short-name -> Hugging Face repo id, for the models published by SmilingWolf.
MODEL_REGISTRY = {
    "wd-v1-4-vit-tagger-v2": "SmilingWolf/wd-v1-4-vit-tagger-v2",
    "wd-v1-4-convnext-tagger-v2": "SmilingWolf/wd-v1-4-convnext-tagger-v2",
    "wd-v1-4-convnextv2-tagger-v2": "SmilingWolf/wd-v1-4-convnextv2-tagger-v2",
    "wd-v1-4-swinv2-tagger-v2": "SmilingWolf/wd-v1-4-swinv2-tagger-v2",
    "wd-v1-4-moat-tagger-v2": "SmilingWolf/wd-v1-4-moat-tagger-v2",
    "wd-vit-tagger-v3": "SmilingWolf/wd-vit-tagger-v3",
    "wd-vit-large-tagger-v3": "SmilingWolf/wd-vit-large-tagger-v3",
    "wd-convnext-tagger-v3": "SmilingWolf/wd-convnext-tagger-v3",
    "wd-swinv2-tagger-v3": "SmilingWolf/wd-swinv2-tagger-v3",
    "wd-eva02-large-tagger-v3": "SmilingWolf/wd-eva02-large-tagger-v3",
}

# Repo id -> (internal ONNX tensor name, dim) of a pooled feature vector taken
# from just before the classification head, usable as an image embedding for
# similarity search. Verified by inspecting the exported graph: for the EVA02
# backbone this is the LayerNorm right after global-average-pooling the patch
# tokens (`core_model.fc_norm`), immediately feeding `core_model.head` (Gemm)
# -> `final_act` (Sigmoid) -> the model's normal tag-probability output.
# Requesting this tensor alongside the normal output in the same
# session.run() call is free (same forward pass); requesting it alone lets
# ONNX Runtime prune the (comparatively cheap but nonzero) head+sigmoid nodes.
# Other registry entries use different backbones (ConvNext/SwinV2/MOAT/plain
# ViT) whose graphs haven't been checked for an equivalent tap point, so they
# deliberately have no entry here and embeddings degrade to None for them.
EMBEDDING_TAPS: dict[str, tuple[str, int]] = {
    "SmilingWolf/wd-eva02-large-tagger-v3": (
        "/core_model/fc_norm/LayerNormalization_output_0",
        1024,
    ),
}

# Tags where the underscore is part of the kaomoji and must be kept as-is.
KAOMOJI_TAGS = {
    "0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>",
    "=_=", ">_<", "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u",
    "x_x", "|_|", "||_||",
}


@dataclass
class Tag:
    name: str
    category: str  # "rating" | "general" | "character"
    confidence: float


@dataclass
class InferResult:
    tags: list[Tag]
    embedding: np.ndarray | None  # None for models with no entry in EMBEDDING_TAPS


def _make_square(img: np.ndarray, target_size: int) -> np.ndarray:
    old_h, old_w = img.shape[:2]
    desired_size = max(old_h, old_w, target_size)

    delta_w = desired_size - old_w
    delta_h = desired_size - old_h
    top, bottom = delta_h // 2, delta_h - delta_h // 2
    left, right = delta_w // 2, delta_w - delta_w // 2

    return cv2.copyMakeBorder(
        img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=[255, 255, 255]
    )


def _smart_resize(img: np.ndarray, size: int) -> np.ndarray:
    # img is already square (from _make_square), so a single dimension check suffices.
    if img.shape[0] > size:
        return cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    if img.shape[0] < size:
        return cv2.resize(img, (size, size), interpolation=cv2.INTER_CUBIC)
    return img


def resolve_repo_id(model: str) -> str:
    if "/" in model:
        return model
    if model in MODEL_REGISTRY:
        return MODEL_REGISTRY[model]
    raise ValueError(
        f"Unknown model '{model}'. Use a known short name "
        f"({', '.join(MODEL_REGISTRY)}) or a full HF repo id (owner/name)."
    )


class WD14Tagger:
    def __init__(self, model: str, providers: list[str] | None = None):
        repo_id = resolve_repo_id(model)
        model_path = hf_hub_download(repo_id, "model.onnx")
        tags_path = hf_hub_download(repo_id, "selected_tags.csv")

        providers = providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]

        self.embedding_output_name: str | None = None
        self.embedding_dim: int | None = None
        tap = EMBEDDING_TAPS.get(repo_id)
        if tap is not None:
            # Expose the internal pooled-feature tensor as an extra graph
            # output so session.run() can fetch it alongside (or instead of)
            # the normal tag-probability output, without re-exporting the
            # model. Built from bytes rather than model_path so the original
            # downloaded .onnx file is left untouched.
            self.embedding_output_name, self.embedding_dim = tap
            onnx_model = onnx.load(model_path)
            onnx_model.graph.output.append(
                onnx.helper.make_tensor_value_info(
                    self.embedding_output_name, onnx.TensorProto.FLOAT, ["batch_size", self.embedding_dim]
                )
            )
            self.session = ort.InferenceSession(onnx_model.SerializeToString(), providers=providers)
        else:
            self.session = ort.InferenceSession(model_path, providers=providers)

        self.input_name = self.session.get_inputs()[0].name
        self.target_size = self.session.get_inputs()[0].shape[1]
        self.output_name = self.session.get_outputs()[0].name

        self.tag_names: list[str] = []
        self.tag_categories: list[str] = []
        with open(tags_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row["name"]
                if name not in KAOMOJI_TAGS:
                    name = name.replace("_", " ")
                self.tag_names.append(name)

                category = row["category"]
                if category == "9":
                    self.tag_categories.append("rating")
                elif category == "4":
                    self.tag_categories.append("character")
                else:
                    self.tag_categories.append("general")

    def preprocess(self, image: Image.Image) -> np.ndarray:
        # Mirrors SmilingWolf's reference preprocessing (as used by
        # wd14-tagger-standalone/tagger/dbimutils.py): flatten alpha onto
        # white, pad to square, then resize with direction-dependent
        # interpolation (AREA when shrinking, CUBIC when enlarging).
        image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, "WHITE")
        canvas.paste(image, mask=image)
        image = canvas.convert("RGB")

        arr = np.asarray(image)
        arr = arr[:, :, ::-1]  # RGB -> BGR

        arr = _make_square(arr, self.target_size)
        arr = _smart_resize(arr, self.target_size)
        return arr.astype(np.float32)

    def infer(self, image: Image.Image) -> InferResult:
        arr = self.preprocess(image)
        batch = arr[np.newaxis, ...]

        embedding = None
        if self.embedding_output_name is not None:
            probs, embedding = self.session.run(
                [self.output_name, self.embedding_output_name], {self.input_name: batch}
            )
            embedding = embedding[0]
        else:
            (probs,) = self.session.run([self.output_name], {self.input_name: batch})
        probs = probs[0]

        tags = [
            Tag(name=name, category=category, confidence=float(p))
            for name, category, p in zip(self.tag_names, self.tag_categories, probs)
        ]
        return InferResult(tags=tags, embedding=embedding)

    def embed(self, image: Image.Image) -> np.ndarray | None:
        # Lean path for embedding-only backfills: requesting just this output
        # lets ONNX Runtime prune the classification head/sigmoid nodes, since
        # they're not on the path to the requested tensor.
        if self.embedding_output_name is None:
            return None
        arr = self.preprocess(image)
        batch = arr[np.newaxis, ...]
        (embedding,) = self.session.run([self.embedding_output_name], {self.input_name: batch})
        return embedding[0]
