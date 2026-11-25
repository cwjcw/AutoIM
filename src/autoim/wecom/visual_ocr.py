"""Local OCR. Frame crops are passed as in-memory BGR arrays."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from math import atan2, degrees
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box


@dataclass(frozen=True, slots=True)
class TextSpan:
    text: str
    box: Box
    score: float
    angle: float | None = None


class TextRecognizer(Protocol):
    def read(self, pixels: np.ndarray, box: Box, *, single_line: bool = False) -> tuple[TextSpan, ...]: ...


class OcrUnavailable(RuntimeError):
    pass


class LocalTextRecognizer:
    def __init__(self) -> None:
        spec = find_spec("rapidocr")
        if spec is None or spec.origin is None:
            raise OcrUnavailable("本地 OCR 依赖未安装，请运行 uv sync。")
        models = Path(spec.origin).parent / "models"
        files = {kind: sorted(models.glob(pattern)) for kind, pattern in (
            ("Det", "*det*.onnx"), ("Cls", "*cls*.onnx"), ("Rec", "*rec*.onnx"))}
        dictionary = models / "ppocr_keys_v1.txt"
        if not all(len(paths) == 1 for paths in files.values()) or not dictionary.is_file():
            raise OcrUnavailable("未找到 RapidOCR 随包提供的模型或中文字典。")
        try:
            from rapidocr import RapidOCR

            params = {f"{kind}.model_path": str(paths[0]) for kind, paths in files.items()}
            params.update({"Rec.rec_keys_path": str(dictionary),
                           "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                           "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                           "Global.text_score": .25, "Global.use_cls": False})
            self._engine = RapidOCR(params=params)
        except Exception as exc:
            raise OcrUnavailable("本地 OCR 初始化失败，请核对依赖和 CPU 运行库。") from exc

    def read(self, pixels: np.ndarray, box: Box, *, single_line: bool = False) -> tuple[TextSpan, ...]:
        crop = np.ascontiguousarray(box.crop(pixels))
        if min(crop.shape[:2]) < 2:
            return ()
        if single_line:
            # Red background has at least one dark channel. Keep light ink
            # inside the badge and present it as dark text with a quiet margin.
            tile = crop.astype(np.int16)
            blue, green, red = tile.transpose(2, 0, 1)
            red_mask = ((red > 143) & (red - np.maximum(blue, green) >= 55)).astype(np.uint8)
            outlines, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if outlines:
                mask = np.zeros(red_mask.shape, dtype=np.uint8)
                cv2.drawContours(mask, [max(outlines, key=cv2.contourArea)], -1, 255, cv2.FILLED)
                ink = (mask > 0) & (np.min(crop, axis=2) >= 155)
                if np.any(ink):
                    y, x = np.where(ink)
                    gray = np.where(ink[y.min():y.max()+1, x.min():x.max()+1], 0, 255).astype(np.uint8)
                    margin = max(3, gray.shape[0] // 3)
                    gray = np.pad(gray, margin, constant_values=255)
                    crop = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        scale = min(3.0, max(1.0, 48 / crop.shape[0]))
        if scale != 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        try:
            result = self._engine(crop, use_det=not single_line, use_cls=False, use_rec=True, text_score=.25)
        except Exception as exc:
            raise OcrUnavailable("本地 OCR 处理目标区域失败。") from exc
        spans = []
        for index, (raw_text, score) in enumerate(zip(result.txts or (), result.scores or ())):
            text = raw_text.strip()
            if not text:
                continue
            bounds = box
            angle = None
            if not single_line and result.boxes is not None:
                points = np.asarray(result.boxes[index]) / scale
                if len(points) >= 2:
                    dx, dy = points[1] - points[0]
                    angle = round(degrees(atan2(dy, dx)), 2)
                bounds = Box(box.left + max(0, int(points[:, 0].min())),
                             box.top + max(0, int(points[:, 1].min())),
                             box.left + min(box.width, int(np.ceil(points[:, 0].max()))),
                             box.top + min(box.height, int(np.ceil(points[:, 1].max()))))
            if bounds.width and bounds.height:
                spans.append(TextSpan(text, bounds, float(score), angle))
        return tuple(sorted(spans, key=lambda span: (span.box.top, span.box.left)))


def text_lines(spans: tuple[TextSpan, ...]) -> tuple[str, ...]:
    lines: list[list[TextSpan]] = []
    for span in sorted(spans, key=lambda item: item.box.center[1]):
        if lines:
            first = lines[-1][0]
            if abs(first.box.center[1] - span.box.center[1]) <= max(first.box.height, span.box.height) * .42:
                lines[-1].append(span)
                continue
        lines.append([span])
    return tuple(" ".join(span.text for span in sorted(line, key=lambda item: item.box.left)) for line in lines)
