"""Deterministic CPU checks; metrics are heuristics, not agency approval."""
from pathlib import Path
from io import BytesIO
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

class QualityRules(BaseModel):
    min_sharpness: float = Field(default=100, ge=0)
    max_noise: float = Field(default=12, ge=0)
    max_black_percent: float = Field(default=20, ge=0, le=100)
    max_white_percent: float = Field(default=55, ge=0, le=100)
    black_level: int = Field(default=5, ge=0, le=127)
    white_level: int = Field(default=250, ge=128, le=255)


def inspect_image(path: Path | bytes, rules: QualityRules, export_rules) -> dict:
    warnings, blocks = [], []
    try:
        with Image.open(BytesIO(path) if isinstance(path, bytes) else path) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            width, height = image.size
            # Native pixels: no interpolation masking blur or noise. Row chunks bound memory.
            lap_sum = lap_square = noise_sum = count = black = white = 0
            for start in range(1, height - 1, 256):
                end = min(start + 256, height - 1)
                a = np.asarray(image.crop((0, start-1, width, end+1)), dtype=np.float32)
                c = a[1:-1, 1:-1]
                lap = a[:-2,1:-1]+a[2:,1:-1]+a[1:-1,:-2]+a[1:-1,2:]-4*c
                # Diagonal high-pass noise estimator (also responds to fine texture).
                residual = (a[:-2,:-2]-2*a[:-2,1:-1]+a[:-2,2:]
                            -2*a[1:-1,:-2]+4*c-2*a[1:-1,2:]
                            +a[2:,:-2]-2*a[2:,1:-1]+a[2:,2:])
                lap_sum += float(lap.sum(dtype=np.float64))
                lap_square += float(np.square(lap).sum(dtype=np.float64))
                noise_sum += float(np.abs(residual).sum(dtype=np.float64))
                count += c.size
            # Exposure includes border pixels too.
            histogram = image.histogram()
            black = sum(histogram[:rules.black_level+1])
            white = sum(histogram[rules.white_level:])
            if not count:
                raise ValueError("Imagem pequena demais para análise técnica.")
            metrics = dict(width=width, height=height, megapixels=width*height/1e6,
                           size_bytes=len(path) if isinstance(path, bytes) else path.stat().st_size,
                           sharpness=max(0, lap_square/count-(lap_sum/count)**2),
                           noise=noise_sum/count*np.sqrt(np.pi/2)/6,
                           black_percent=100*black/(width*height), white_percent=100*white/(width*height))
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        return dict(status="blocked", reasons=[f"Imagem ilegível: {error}"], metrics={})
    for key, threshold, low, label in (
        ("sharpness", rules.min_sharpness, True, "Nitidez baixa"),
        ("noise", rules.max_noise, False, "Ruído ou textura fina elevados"),
        ("black_percent", rules.max_black_percent, False, "Excesso de pixels pretos"),
        ("white_percent", rules.max_white_percent, False, "Excesso de pixels estourados"),
    ):
        if (metrics[key] < threshold if low else metrics[key] > threshold):
            warnings.append(f"{label}: {metrics[key]:.2f} (limiar {threshold:g}).")
    if export_rules.min_megapixels and metrics['megapixels'] < export_rules.min_megapixels:
        blocks.append("Resolução abaixo do mínimo de megapixels configurado.")
    if export_rules.max_megapixels and metrics['megapixels'] > export_rules.max_megapixels:
        blocks.append("Resolução acima do máximo de megapixels configurado.")
    return dict(status="blocked" if blocks else "warning" if warnings else "approved",
                reasons=blocks+warnings, metrics=metrics)
