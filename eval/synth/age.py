"""깨끗한 합성 쪽을 스캔본처럼 낡게 하고, 칸마다 글자가 얼마나 남았는지 잰다 (https://github.com/reddol18/docflow/issues/139).

단계 (낡음 정도 light / medium / heavy)
  인쇄   타자기 흔들림(render.Page typewriter), InkBleed(번짐), LowInkRandomLines(잉크 모자람), stroke_dropout(획 끊김), InkMottling
  종이   paper_tone(누렇게·고르지 않게·가장자리 어둡게), BrightnessTexturize·NoiseTexturize(결), Stains(얼룩)
  복사   BadPhotoCopy(토너 잡티), DirtyDrum(드럼 줄), heavy는 잡티를 한 번 더 (복사본의 복사)
  보관   BleedThrough(뒷면 비침), punch_holes(철끈 구멍), crease(접힌 자국)
  스캔   낮은 해상도로 줄였다 키우기, 기울기, JPEG(저장 단계, 품질 QUALITY)
Augraphy의 LightingGradient·ColorPaper·Folding·BindingsAndFasteners·Letterpress는 종이가 회색으로 바뀌거나 너무 세서
쓰지 않고 직접 한다 (2026-11-18 시제품).
단어 상자: Augraphy 단계는 모양을 바꾸지 않으므로 Augraphy에 넘기지 않고 원래 상자를 기울기(rotate)에서만 옮긴다.

판독 어려움 (legibility): 깨끗한 쪽의 잉크 화소를 그대로 두고, 낡은 쪽(기울이기 전, 저장 품질로 JPEG 왕복)에서
그 화소와 주변 종이의 밝기 차를 단어마다 잰다.
칸의 값은 그 칸 단어들 중 가장 낮은 값이다. LEGIBLE_MIN 아래면 사람이 읽기 어렵다고 보고 정답에 표시한다 (채점에서 따로 셈).
Augraphy는 함수 안에서 불러온다 — 쪽 그리기·판독 잣대만 시험할 때는 없어도 된다.
"""
import functools
import io
import math
import random

import numpy as np
from PIL import Image, ImageDraw

LEVELS = ("light", "medium", "heavy")
QUALITY = {"light": 80, "medium": 65, "heavy": 50}  # 저장 JPEG 품질 — 판독 밝기 차도 이 품질로 왕복한 그림에서 잰다
LEGIBLE_MIN = 30  # 0~255 밝기 차. 2026-11-18 heavy 철 눈 확인: 12~24인 쪽은 읽을 수 없었고, 33인 제목은 흐리지만 읽혔다


def _pipeline(level, seed, copy_only=False):
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
    return AugraphyPipeline(ink_phase=ink, paper_phase=paper, post_phase=post, random_seed=seed)


def _no_cache_image(random_image=0):
    return None


@functools.cache
def _numba_seeder():
    from numba import njit

    @njit(cache=False)
    def seed(x):  # numba는 random과 np.random 난수 상태를 따로 가진다
        random.seed(x)
        np.random.seed(x)

    return seed


def _augraphy(bgr, level, seed, copy_only=False):
    """Augraphy 단계. 위 효과는 모두 화소 값만 바꾸고 모양·크기를 바꾸지 않으므로 단어 상자는 넘기지 않는다.

    BleedThrough는 뒷면 그림을 실행 폴더 augraphy_cache/의 이전 출력 중 아무것이나 골라 쓰므로, 같은 시드라도
    앞서 무엇을 돌렸느냐에 따라 결과가 바뀐다. 캐시를 읽지 않게 해 뒷면은 이 쪽을 뒤집은 그림으로 고정한다.
    또 numba로 컴파일된 Augraphy 함수(BadPhotoCopy의 Worley 잡티 등)는 Python 쪽 random·np.random과 따로 난수 상태를
    가져서, AugraphyPipeline(random_seed)만으로는 앞 쪽들을 돌린 이력에 따라 바뀐다 (2026-11-19 heavy 복사 단계에서 확인)
    — numba 쪽도 같은 시드로 맞춘다. numba 시드는 32비트라 seed < 2**32 (bundle.SEED_MAX가 지킨다)."""
    from augraphy.augmentations import bleedthrough

    bleedthrough.load_image_from_cache = _no_cache_image
    _numba_seeder()(seed)
    return _pipeline(level, seed, copy_only).augment(bgr)["output"]


def jpeg_roundtrip(img, quality):
    """저장할 때와 같은 품질로 JPEG 인코딩·디코딩한 그림."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert(img.mode)


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
    """깨끗한 쪽 → (낡은 쪽 이미지, 옮긴 상자 목록, 칸별 밝기 차, 기울기).

    상자는 Augraphy 단계를 거치지 않고(모양을 바꾸는 효과가 없음) 원래 상자를 rotate()에서만 옮긴다.
    칸별 밝기 차는 기울이기 전 그림(깨끗한 쪽과 좌표가 같음)을 저장 품질 QUALITY[level]로 JPEG 왕복한 회색조에서 잰다.
    실제 저장은 기울인 뒤에 하므로 저장본과 화소까지 같지는 않다 — 같은 품질의 JPEG 손실을 넣은 근사다.
    돌려준 이미지는 호출하는 쪽이 QUALITY[level]로 저장한다.
    """
    if level not in LEVELS:
        raise ValueError(level)
    rng = np.random.default_rng(seed)
    clean = np.asarray(page_img.convert("L"))
    out = _augraphy(np.ascontiguousarray(np.asarray(page_img)[:, :, ::-1]), level, seed)
    if level == "heavy":
        out = _augraphy(out, level, seed + 1, copy_only=True)
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
    # 기울이기 전 (깨끗한 쪽과 좌표가 같을 때), 저장 품질로 JPEG 왕복한 뒤의 회색조에서
    legib = field_legibility(ann, clean, np.asarray(jpeg_roundtrip(img, QUALITY[level]).convert("L")))
    ang = float(rng.uniform(-1.2, 1.2) if level == "heavy" else rng.uniform(-0.6, 0.6))
    img, boxes = rotate(img, [a["box"] for a in ann], ang)
    return img, boxes, legib, ang
