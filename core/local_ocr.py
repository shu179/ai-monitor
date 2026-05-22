"""
本地 OCR 运行时

识别模式默认走本地 OCR，不上传图片。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher
from pathlib import Path


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    for old, new in {
        " ": "",
        "\n": "",
        "\r": "",
        "\t": "",
        "-": "",
        "_": "",
        "/": "",
        "|": "",
        "｜": "",
        "(": "",
        ")": "",
        "（": "",
        "）": "",
        "[": "",
        "]": "",
        "【": "",
        "】": "",
        ",": "",
        "，": "",
        ":": "",
        "：": "",
        ".": "",
        "。": "",
        "·": "",
    }.items():
        text = text.replace(old, new)
    return text


def _normalize_text_relaxed(value: str) -> str:
    raw = str(value or "").strip().lower()
    for old, new in {
        "|": "i",
        "1": "i",
        "!": "i",
        "l": "i",
    }.items():
        raw = raw.replace(old, new)
    text = _normalize_text(raw)
    for old, new in {
        "0": "o",
        "5": "s",
        "8": "b",
    }.items():
        text = text.replace(old, new)
    return text


def _preferred_providers(config: dict) -> list[str]:
    recognition_cfg = (config or {}).get("recognition", {})
    configured = recognition_cfg.get("local_ocr_providers", [])
    if isinstance(configured, str):
        configured = [part.strip() for part in configured.split(",") if part.strip()]
    if configured:
        return configured

    if sys.platform == "win32":
        return ["windows_native", "tesseract"]
    if sys.platform == "darwin":
        return ["macos_vision_cli", "macos_vision", "tesseract"]
    return ["tesseract"]


def _save_temp_image(image, variant_name: str) -> str:
    digest = hashlib.md5(variant_name.encode("utf-8")).hexdigest()[:8]
    tmp = tempfile.NamedTemporaryFile(prefix=f"ocr_{digest}_", suffix=".png", delete=False)
    tmp.close()
    image.save(tmp.name, format="PNG", optimize=True)
    return tmp.name


def _find_content_bbox(image, white_threshold: int = 245):
    from PIL import ImageOps

    gray = image.convert("L")
    mask = gray.point(lambda p: 255 if p < white_threshold else 0)
    bbox = ImageOps.invert(mask).getbbox()
    if bbox:
        return bbox
    return mask.getbbox()


def _pad_bbox(bbox: tuple[int, int, int, int], width: int, height: int, padding: int):
    left, top, right, bottom = bbox
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def _build_image_variants(image_path: str, config: dict) -> list[tuple[str, str]]:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    variants: list[tuple[str, str]] = []

    src = Path(image_path)
    with Image.open(src) as img:
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")

        recognition_cfg = (config or {}).get("recognition", {})
        max_width = max(1800, int(recognition_cfg.get("local_ocr_max_width", 2400) or 2400))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, int(img.height * ratio))))
        elif img.width < 1400:
            ratio = 1400 / max(1, img.width)
            img = img.resize((1400, max(1, int(img.height * ratio))))

        gray = ImageOps.autocontrast(img.convert("L"))
        base = ImageEnhance.Contrast(gray).enhance(1.2)
        base = ImageEnhance.Sharpness(base).enhance(1.15)
        base = base.filter(ImageFilter.SHARPEN)
        variants.append((_save_temp_image(base, "base_gray"), "base_gray"))

        high_contrast = ImageEnhance.Contrast(base).enhance(1.45)
        variants.append((_save_temp_image(high_contrast, "high_contrast"), "high_contrast"))

        threshold = high_contrast.point(lambda p: 255 if p > 210 else 0)
        variants.append((_save_temp_image(threshold, "threshold"), "threshold"))

        bbox = _find_content_bbox(base)
        if bbox:
            padded = _pad_bbox(bbox, base.width, base.height, padding=36)
            crop = base.crop(padded)
            variants.append((_save_temp_image(crop, "content_crop"), "content_crop"))

            if crop.width < 1800:
                ratio = 1800 / max(1, crop.width)
                enlarged = crop.resize((1800, max(1, int(crop.height * ratio))))
            else:
                enlarged = crop
            variants.append((_save_temp_image(enlarged, "content_crop_enlarged"), "content_crop_enlarged"))

            if crop.width > 900 and crop.height > 400:
                thirds = 3 if crop.width >= crop.height * 1.2 else 2
                step = crop.height / thirds
                panel_h = int(step * 1.18)
                for idx in range(thirds):
                    top = int(max(0, min(crop.height - panel_h, idx * step - 18)))
                    bottom = min(crop.height, top + panel_h)
                    panel = crop.crop((0, top, crop.width, bottom))
                    panel = ImageEnhance.Contrast(panel).enhance(1.3)
                    variants.append((_save_temp_image(panel, f"content_panel_{idx + 1}"), f"content_panel_{idx + 1}"))

    unique: list[tuple[str, str]] = []
    seen_names = set()
    for path, name in variants:
        if name in seen_names:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
            continue
        seen_names.add(name)
        unique.append((path, name))
    return unique


def _cleanup_variants(variant_paths: list[tuple[str, str]]):
    for path, _ in variant_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def _is_meaningful_text(text: str) -> bool:
    stripped = "".join(ch for ch in str(text or "") if not ch.isspace())
    return len(stripped) >= 2


def _run_tesseract(image_path: str, config: dict) -> str:
    binary = shutil.which("tesseract")
    if not binary:
        return ""

    recognition_cfg = (config or {}).get("recognition", {})
    langs = str(recognition_cfg.get("tesseract_languages", "chi_sim+eng") or "chi_sim+eng")
    psm = str(recognition_cfg.get("tesseract_psm", 6) or 6)
    result = subprocess.run(
        [binary, image_path, "stdout", "-l", langs, "--psm", psm],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _run_windows_native_ocr(image_path: str) -> str:
    if sys.platform != "win32":
        return ""

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return ""

    script = r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeMarshal]

function Await([object] $Async, [type] $ResultType) {
    $taskFactory = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    $task = $taskFactory.MakeGenericMethod($ResultType).Invoke($null, @($Async))
    $task.Wait()
    return $task.Result
}

$imagePath = $args[0]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language("en-US")))
}
if ($null -eq $engine) {
  throw "No Windows OCR engine available"
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$result.Text
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, image_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=25,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _run_macos_vision(image_path: str) -> str:
    if sys.platform != "darwin":
        return ""

    try:
        import objc
        from Foundation import NSBundle, NSURL
        from Quartz import CGImageSourceCreateImageAtIndex, CGImageSourceCreateWithURL

        NSBundle.bundleWithPath_("/System/Library/Frameworks/Vision.framework").load()
        VNRecognizeTextRequest = objc.lookUpClass("VNRecognizeTextRequest")
        VNImageRequestHandler = objc.lookUpClass("VNImageRequestHandler")

        url = NSURL.fileURLWithPath_(image_path)
        source = CGImageSourceCreateWithURL(url, None)
        if source is None:
            return ""
        image = CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            return ""

        request = VNRecognizeTextRequest.alloc().init()
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        request.setRecognitionLevel_(1)

        handler = VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok = handler.performRequests_error_([request], None)
        if not ok:
            return ""

        lines = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates:
                text = str(candidates[0].string() or "").strip()
                if text:
                    lines.append(text)
        return "\n".join(lines).strip()
    except Exception:
        return ""


def _run_macos_vision_cli(image_path: str) -> str:
    if sys.platform != "darwin":
        return ""

    clang = shutil.which("clang")
    if not clang:
        return ""

    cache_dir = Path(tempfile.gettempdir()) / "ai_monitor_ocr"
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary_path = cache_dir / "macos_vision_ocr"
    source_path = cache_dir / "macos_vision_ocr.m"
    source = """#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>
#import <CoreGraphics/CoreGraphics.h>

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "need image path\\n");
            return 1;
        }
        NSString *path = [NSString stringWithUTF8String:argv[1]];
        NSURL *url = [NSURL fileURLWithPath:path];
        CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)url, NULL);
        if (!source) {
            fprintf(stderr, "load source failed\\n");
            return 2;
        }
        CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
        CFRelease(source);
        if (!image) {
            fprintf(stderr, "load image failed\\n");
            return 3;
        }
        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = NO;
        request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
        NSError *error = nil;
        BOOL ok = [handler performRequests:@[request] error:&error];
        CGImageRelease(image);
        if (!ok) {
            fprintf(stderr, "perform failed: %s\\n", error.localizedDescription.UTF8String ?: "unknown");
            return 4;
        }
        NSMutableArray<NSString *> *lines = [NSMutableArray array];
        for (VNRecognizedTextObservation *obs in request.results) {
            VNRecognizedText *top = [[obs topCandidates:1] firstObject];
            if (top.string.length > 0) {
                [lines addObject:top.string];
            }
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:lines options:0 error:nil];
        NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        printf("%s\\n", json.UTF8String ?: "[]");
    }
    return 0;
}
"""
    try:
        if not binary_path.exists():
            source_path.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [
                    clang,
                    "-fobjc-arc",
                    "-framework", "Foundation",
                    "-framework", "Vision",
                    "-framework", "ImageIO",
                    "-framework", "CoreGraphics",
                    str(source_path),
                    "-o", str(binary_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return ""
        result = subprocess.run(
            [str(binary_path), image_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return ""
        payload = json.loads((result.stdout or "").strip() or "[]")
        if isinstance(payload, list):
            return "\n".join(str(item).strip() for item in payload if str(item).strip()).strip()
        return ""
    except Exception:
        return ""


def _ocr_text_score(text: str, candidate_brands: list[str] | None = None) -> tuple[int, list[str]]:
    stripped = str(text or "").strip()
    if not stripped:
        return 0, []

    matched = match_candidate_brands(stripped, candidate_brands or [])
    score = len("".join(ch for ch in stripped if not ch.isspace()))
    if matched:
        score += 10000 + 500 * len(matched)
    if any("\u4e00" <= ch <= "\u9fff" for ch in stripped):
        score += 200
    return score, matched


def extract_text_from_image(config: dict, image_path: str, candidate_brands: list[str] | None = None) -> tuple[str, dict]:
    variant_paths = _build_image_variants(image_path, config)
    providers = _preferred_providers(config)
    best_text = ""
    best_meta = {"provider": "", "variant": "", "matched_brands": []}
    try:
        for provider in providers:
            for variant_path, variant_name in variant_paths:
                text = ""
                if provider == "tesseract":
                    text = _run_tesseract(variant_path, config)
                elif provider == "windows_native":
                    text = _run_windows_native_ocr(variant_path)
                elif provider == "macos_vision":
                    text = _run_macos_vision(variant_path)
                elif provider == "macos_vision_cli":
                    text = _run_macos_vision_cli(variant_path)

                text = (text or "").strip()
                if not _is_meaningful_text(text):
                    continue

                score, matched = _ocr_text_score(text, candidate_brands)
                if score > int(best_meta.get("score", 0) or 0):
                    best_text = text
                    best_meta = {
                        "provider": provider,
                        "variant": variant_name,
                        "matched_brands": matched,
                        "score": score,
                    }
                if matched:
                    return text, best_meta
        return best_text, best_meta
    finally:
        _cleanup_variants(variant_paths)


def _best_fuzzy_score(text_key: str, brand_key: str) -> float:
    if not text_key or not brand_key:
        return 0.0

    if brand_key in text_key:
        return 1.0

    best = 0.0
    window = max(len(brand_key) + 2, int(len(brand_key) * 1.4))
    if len(text_key) <= window:
        return SequenceMatcher(None, text_key, brand_key).ratio()

    for start in range(0, max(1, len(text_key) - window + 1), max(1, len(brand_key) // 2)):
        chunk = text_key[start:start + window]
        best = max(best, SequenceMatcher(None, chunk, brand_key).ratio())
        if best >= 0.92:
            return best
    return best


def match_candidate_brands(text: str, candidate_brands: list[str]) -> list[str]:
    text_key = _normalize_text(text)
    relaxed_text_key = _normalize_text_relaxed(text)
    if not text_key and not relaxed_text_key:
        return []

    matched = []
    for brand in candidate_brands:
        brand_key = _normalize_text(brand)
        relaxed_brand_key = _normalize_text_relaxed(brand)
        if not brand_key:
            continue
        if brand_key in text_key or (relaxed_brand_key and relaxed_brand_key in relaxed_text_key):
            if brand not in matched:
                matched.append(brand)
            continue

        relaxed_score = _best_fuzzy_score(relaxed_text_key, relaxed_brand_key) if relaxed_brand_key else 0.0
        strict_score = _best_fuzzy_score(text_key, brand_key)
        score = max(strict_score, relaxed_score)
        if len(brand_key) >= 3 and score >= 0.88:
            if brand not in matched:
                matched.append(brand)
    return matched


def build_match_summary(text: str, brands: list[str]) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    compact = " ".join(part.strip() for part in text.splitlines() if part.strip())
    compact = compact[:140]
    if brands:
        return f"本地OCR命中 {', '.join(brands)}；文本片段：{compact}"
    return f"本地OCR文本片段：{compact}"


def is_ai_fallback_enabled(config: dict) -> bool:
    return False


def dump_provider_status(config: dict) -> str:
    return json.dumps(
        {
            "providers": _preferred_providers(config),
        },
        ensure_ascii=False,
    )
