from dataclasses import dataclass
from pathlib import Path

import cv2
from rapidocr import RapidOCR


@dataclass
class OcrOutput:
    text: str
    mean_score: float


class OcrEngine:
    def __init__(self):
        self.engine = RapidOCR()

    def scan(self, image_path: str, rotation_degrees: int = 0, crop=None) -> OcrOutput:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(image_path)

        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f'Could not open image: {image_path}')

        rotation = rotation_degrees % 360
        if rotation == 90:
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 180:
            image = cv2.rotate(image, cv2.ROTATE_180)
        elif rotation == 270:
            image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif rotation != 0:
            raise ValueError('Rotation must be 0, 90, 180, or 270 degrees.')

        if crop:
            x, y, w, h = crop
            ih, iw = image.shape[:2]
            x1 = max(0, min(iw - 1, round(x * iw)))
            y1 = max(0, min(ih - 1, round(y * ih)))
            x2 = max(x1 + 1, min(iw, round((x + w) * iw)))
            y2 = max(y1 + 1, min(ih, round((y + h) * ih)))
            image = image[y1:y2, x1:x2]
            if image.size == 0:
                raise ValueError('Crop area is empty.')

        result = self.engine(image)
        txts = list(result.txts or []) if result is not None else []
        scores = list(result.scores or []) if result is not None else []
        text = '\n'.join(str(x) for x in txts)
        mean_score = (sum(float(s) for s in scores) / len(scores)) if scores else 0.0
        return OcrOutput(text=text, mean_score=mean_score)
