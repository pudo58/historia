"""Shared generation/export geometry. Limits are budgets, not GPU qualification."""
import math

RATIOS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5)}

def resolve_format(quality="final", project_settings=None, **overrides):
    settings = dict(project_settings or {})
    settings.update({k: v for k, v in overrides.items() if v is not None})
    if quality not in {"draft", "final"}:
        raise ValueError("Chất lượng phải là draft hoặc final.")
    modern = any(settings.get(k) is not None for k in ("render_profile", "output_resolution", "aspect_ratio"))
    profile = settings.get("render_profile") or ("draft" if quality == "draft" else "standard")
    resolution = settings.get("output_resolution") or "720p"
    ratio = settings.get("aspect_ratio") or "16:9"
    transition = settings.get("transition") or "none"
    method = settings.get("upscale_method") or "lanczos"
    if profile not in {"draft", "standard"} or resolution not in {"720p", "1080p", "1440p"} or ratio not in RATIOS:
        raise ValueError("Cấu hình kích thước không hợp lệ.")
    if transition not in {"none", "natural", "dissolve"} or method not in {"lanczos", "ai"}:
        raise ValueError("Cấu hình chuyển cảnh/upscale không hợp lệ.")
    legacy = (832, 480) if quality == "draft" else (1280, 720)
    a, b = RATIOS[ratio]
    base = int(resolution[:-1])
    output = (base * 16 // 9, base) if ratio == "16:9" else ((base, base * 16 // 9) if ratio == "9:16" else (base, base * b // a))
    # Qwen and Wan need multiples of 16. Exact 1080 is not on that lattice.
    native_1080 = modern and profile == "standard" and resolution == "1080p"
    if native_1080:
        render = (align16(output[0]), align16(output[1]))
    else:
        budget = 832 * 480 if profile == "draft" else 1280 * 720
        unit = max(1, math.isqrt(budget // (256 * a * b)))
        render = (16 * a * unit, 16 * b * unit)
    return {"render_size": render if modern else legacy, "output_size": output if modern else legacy,
            "render_profile": profile, "output_resolution": resolution, "aspect_ratio": ratio,
            "transition": transition, "upscale_method": method, "legacy": not modern,
            "native_1080": native_1080, "gpu_benchmarked": False}


def align16(value):
    return (int(value) + 15) // 16 * 16
