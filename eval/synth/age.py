"""깨끗한 합성 쪽을 스캔본처럼 낡게 하고, 칸마다 글자가 얼마나 남았는지 잰다 (https://github.com/reddol18/docflow/issues/139).

단계 (낡음 정도 light / medium / heavy)
  인쇄   타자기 흔들림(render.Page typewriter), InkBleed(번짐), LowInkRandomLines(잉크 모자람), stroke_dropout(획 끊김), InkMottling
  종이   paper_tone(누렇게·고르지 않게·가장자리 어둡게), BrightnessTexturize·NoiseTexturize(결), Stains(얼룩)
  복사   BadPhotoCopy(토너 잡티), DirtyDrum(드럼 줄), heavy는 잡티를 한 번 더 (복사본의 복사)
  보관   BleedThrough(뒷면 비침), punch_holes(철끈 구멍), crease(접힌 자국)
  스캔   낮은 해상도로 줄였다 키우기, 기울기, JPEG(저장 단계)
Augraphy의 LightingGradient·ColorPaper·Folding·BindingsAndFasteners·Letterpress는 종이가 회색으로 바뀌거나 너무 세서
쓰지 않고 직접 한다 (2026-11-18 시제품).
단어 상자: Augraphy 단계는 모양을 바꾸지 않으므로 그대로, 기울기만 여기서 옮긴다.

판독 어려움 (legibility): 깨끗한 쪽의 잉크 화소를 그대로 두고, 낡은 쪽에서 그 화소와 주변 종이의 밝기 차를 단어마다 잰다.
칸의 값은 그 칸 단어들 중 가장 낮은 값이다. LEGIBLE_MIN 아래면 사람이 읽기 어렵다고 보고 정답에 표시한다 (채점에서 따로 셈).
Augraphy는 함수 안에서 불러온다 — 쪽 그리기·판독 잣대만 시험할 때는 없어도 된다.
"""
import math

import numpy as np
from PIL import Image, ImageDraw

LEVELS = ("light", "medium", "heavy")
LEGIBLE_MIN = 30  # 0~255 밝기 차. 2026-11-18 heavy 철 눈 확인: 12~24인 쪽은 읽을 수 없었고, 33인 제목은 흐리지만 읽혔다


def _pipeline(level, seed, boxes, copy_only=False):
    from augraphy import AugraphyPipeline
    from augraphy.augmentations import (
        BadPhotoCopy,
        BleedThrough,
        BrightnessTexturize,
        DirtyDrum,
        InkBleed,
        InkMottling,
        LowInkRandomLines,
        NoiseTexturize,
        Stains,
    )
    hv = level == "heavy"
    ink, paper, post = [], [], []
    if not copy_only:
        ink = [InkMottling(ink_mottling_alpha_range=(0.1, 0.25), p=0.7)]
        paper = [BrightnessTexturize(texturize_range=(0.92, 0.98), deviation=0.03)]
        if level != "light":
            ink += [InkBleed(intensity_range=(0.4, 0.7) if hv else (0.3, 0.5), kernel_size=(5, 5) if hv else (3, 3),
                             severity=(0.3, 0.5) if hv else (0.2, 0.35)),
                    LowInkRandomLines(count_range=(4, 10) if hv else (2, 5), use_consistent_lines=False, noise_probability=0.1, p=0.7)]
            paper += [NoiseTexturize(sigma_range=(2, 6) if hv else (2, 4), turbulence_range=(3, 5))]
            post += [BleedThrough(intensity_range=(0.1, 0.3), color_range=(150, 220), ksize=(31, 31), sigmaX=3,
                                  alpha=0.06 if hv else 0.04, offsets=(15, 25), p=0.8)]
        if hv:
            paper += [Stains(stains_type="random", stains_blend_alpha=0.2, p=0.6)]
    if level != "light":
        post += [BadPhotoCopy(noise_type=-1, noise_side="random", noise_iteration=(1, 1), noise_size=(1, 2),
                              noise_value=(90, 160) if hv else (140, 200), noise_sparsity=(0.6, 0.9),
                              noise_concentration=(0.03, 0.08) if hv else (0.02, 0.05), blur_noise=1, p=0.9)]
    if hv:
        post += [DirtyDrum(line_width_range=(1, 2), line_concentration=0.02, direction=1, noise_intensity=0.15,
                           noise_value=(120, 200), p=0.5)]
    return AugraphyPipeline(ink_phase=ink, paper_phase=paper, post_phase=post,
                            bounding_boxes=[list(b) for b in boxes] if boxes else None, random_seed=seed)


def _augraphy(bgr, boxes, level, seed, copy_only=False):
    out = _pipeline(level, seed, boxes, copy_only).augment(bgr)
    return out["output"], (out.get("bounding_boxes") or boxes)


def _lowfreq(rng, h, w, cells):
    g = rng.random((cells, max(2, int(cells * w / h))))
    return np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC), np.float32) / 255


def paper_tone(rgb, level, rng):
    """흰 바탕 → 누런 종이(고르지 않게), 가장자리 조금 어둡게. 잉크도 종이색을 곱해 같이 물든다."""
    h, w, _ = rgb.shape
    age = {"light": 0.4, "medium": 0.7, "heavy": 1.0}[level]
    base = np.array([244, 237, 220], np.float32) - age * np.array([4, 10, 26], np.float32)
    tone = base * (1 - age * 0.05 * _lowfreq(rng, h, w, 6)[..., None])
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    vig = 1 - age * 0.07 * (((xx / w - 0.5) ** 2 + (yy / h - 0.5) ** 2) * 2.5)
    return np.clip(rgb.astype(np.float32) / 255 * tone * vig[..., None], 0, 255).astype(np.uint8)


def stroke_dropout(rgb, clean_gray, level, rng):
    """획 끊김: 잉크 화소 중 작은 얼룩 모양 자리만 종이색 쪽으로 밝힌다 (리본 잉크가 모자란 글쇠).
    Augraphy Letterpress는 설정을 낮춰도 글자 전체가 옅어져(밝기 차 208→70, 2026-11-18) 쓰지 않는다."""
    if level == "light":
        return rgb
    h, w, _ = rgb.shape
    blobs = _lowfreq(rng, h, w, h // 7)  # 지름 7화소쯤의 얼룩
    hole = (blobs > (0.78 if level == "heavy" else 0.86)) & (clean_gray < 128)
    out = rgb.astype(np.float32)
    out[hole] = out[hole] * 0.35 + 235 * 0.65
    return out.astype(np.uint8)


def crease(rgb, rng):
    """가로로 접힌 자국: 선 근처만 한쪽 어둡게, 한쪽 밝게."""
    h, w, _ = rgb.shape
    y0 = int(h * rng.uniform(0.3, 0.7))
    dy = np.arange(h, dtype=np.float32)[:, None] - y0 - np.linspace(-6, 6, w)[None, :] * rng.uniform(-1, 1)
    shade = 1 - 0.10 * np.exp(-(dy / 6) ** 2) + 0.04 * np.exp(-((dy - 14) / 10) ** 2)
    return np.clip(rgb.astype(np.float32) * shade[..., None], 0, 255).astype(np.uint8)


def punch_holes(img, rng):
    """철끈 구멍 2개 (왼쪽 여백). 구멍으로 스캐너 덮개가 보이고 가장자리에 그림자."""
    d = ImageDraw.Draw(img)
    x, rad = int(rng.uniform(55, 80)), int(rng.uniform(28, 34))
    cy = img.size[1] / 2 + rng.uniform(-60, 60)
    for y in (cy - 400, cy + 400):
        d.ellipse((x - rad - 3, y - rad - 3, x + rad + 3, y + rad + 3), fill=(150, 145, 135))
        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=(222, 222, 218))


def rotate(img, boxes, ang, fill=(190, 185, 170)):
    w, h = img.size
    img = img.rotate(ang, resample=Image.BICUBIC, fillcolor=fill)
    t, cx, cy = math.radians(ang), w / 2, h / 2
    out = []
    for x0, y0, x1, y1 in boxes:
        pts = [(cx + (x - cx) * math.cos(t) + (y - cy) * math.sin(t), cy - (x - cx) * math.sin(t) + (y - cy) * math.cos(t))
               for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        out.append([int(min(q[0] for q in pts)), int(min(q[1] for q in pts)),
                    int(math.ceil(max(q[0] for q in pts))), int(math.ceil(max(q[1] for q in pts)))])
    return img, out


def contrast(clean_gray, aged_gray, box):
    """단어 상자 안: 깨끗한 쪽 잉크 화소(<128)와 종이 화소(>=200)의 낡은 쪽 밝기 차 (0~255). 잉크가 없으면 None."""
    x0, y0, x1, y1 = (max(0, int(v)) for v in box)
    c = clean_gray[y0:y1, x0:x1]
    a = aged_gray[y0:y1, x0:x1].astype(np.float32)
    ink, bg = c < 128, c >= 200
    if ink.sum() < 5 or bg.sum() < 5:
        return None
    return float(np.median(a[bg]) - np.mean(a[ink]))


def field_legibility(ann, clean_gray, aged_gray):
    """칸마다 가장 흐린 단어의 밝기 차."""
    out = {}
    for a in ann:
        f = a.get("field")
        if not f:
            continue
        v = contrast(clean_gray, aged_gray, a["box"])
        if v is not None:
            out[f] = min(out.get(f, 255.0), v)
    return {f: round(v, 1) for f, v in out.items()}


def age(page_img, ann, level, seed):
    """깨끗한 쪽 → (낡은 쪽 이미지, 옮긴 상자 목록, 칸별 밝기 차, 기울기)."""
    if level not in LEVELS:
        raise ValueError(level)
    rng = np.random.default_rng(seed)
    boxes = [a["box"] for a in ann]
    clean = np.asarray(page_img.convert("L"))
    out, boxes = _augraphy(np.ascontiguousarray(np.asarray(page_img)[:, :, ::-1]), boxes, level, seed)
    if level == "heavy":
        out, boxes = _augraphy(out, boxes, level, seed + 1, copy_only=True)
    rgb = stroke_dropout(np.ascontiguousarray(out[:, :, ::-1]), clean, level, rng)
    rgb = paper_tone(rgb, level, rng)
    if level == "heavy":
        rgb = crease(rgb, rng)
    img = Image.fromarray(rgb)
    if level != "light":
        punch_holes(img, rng)
        f = 0.62 if level == "heavy" else 0.72  # 낮은 해상도 스캔을 다시 키운 것처럼
        w, h = img.size
        img = img.resize((int(w * f), int(h * f)), Image.BILINEAR).resize((w, h), Image.BICUBIC)
    legib = field_legibility(ann, clean, np.asarray(img.convert("L")))  # 기울이기 전 (깨끗한 쪽과 좌표가 같을 때)
    ang = float(rng.uniform(-1.2, 1.2) if level == "heavy" else rng.uniform(-0.6, 0.6))
    img, boxes = rotate(img, boxes, ang)
    return img, boxes, legib, ang
