#!/usr/bin/env python3
"""
render_daemon.py — Helix's inbuilt image generator. Local, private, free.

the user, 2026-09-03: "we will save a lot of money, it's a part of Helix, and we
need it to be just as good as the top of the line out there." Honest ceiling,
told to him before building: on this GTX 1070 (8 GB, Pascal) the best OPEN
models run — attest with paid APIs on art/style, a notch below on strict
instruction-following, slower per image. This daemon serves that ceiling.

Design:
- Separate process from Helix (app.py only proxies), so models load/unload
  and the daemon restarts without ever touching a live conversation.
- Binds 127.0.0.1 ONLY. Remote access rides Helix's existing tailnet gate.
- Models lazy-load on first use and stay resident; "sd15" (DreamShaper 8,
  fast, ~15-25 s at 512) is the default; "sdxl" (better, slower on Pascal)
  loads on request. fp32 on purpose: Pascal's fp16 is crippled (1/64 rate),
  so fp16 would SLOW this card down, not speed it up.
- Output lands in static/render/ with a timestamp name, and the response
  returns both the file path and the URL path Helix can serve.

API: POST /gen  {"prompt": ..., "negative": ..., "model": "sd15"|"sdxl"|"flux",
                 "width": 512, "height": 512, "steps": 28, "seed": null,
                 "count": 1}
     "flux" = Flux.1-schnell GGUF: prompt-adherence tier, defaults 768x768 at
     4 steps, ignores "negative" (schnell has no CFG), minutes per image.
     GET  /health  -> {"ok": true, "loaded": [...]}
"""
import os
import time

import torch
from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "static", "render")
os.makedirs(OUT, exist_ok=True)

# Single-file checkpoints, downloaded with curl: the HF hub's sharded-download
# CDN resolved only intermittently from this box (measured 2026-09-03), while
# plain resolve/main URLs work every time. Add models by dropping a
# .safetensors here and listing it.
MODEL_DIR = os.path.expanduser("~/render_models")
MODELS = {
    "sd15": (os.path.join(MODEL_DIR, "dreamshaper8.safetensors"), "sd"),
    "sdxl": (os.path.join(MODEL_DIR, "juggernautxl.safetensors"), "sdxl"),
    # Flux.1-schnell (Apache-2.0), Q4_K_S GGUF transformer + fp16 T5 storage.
    # Prompt-adherence tier: slow on Pascal, best instruction-following.
    "flux": (os.path.join(MODEL_DIR, "flux1-schnell-Q4_K_S.gguf"), "flux"),
    # SD 1.5 inpainting (runwayml lineage, fetched from the sd-legacy HF
    # mirror 2026-09-05, sha256-verified against the repo's LFS oid).
    "sd15-inpaint": (os.path.join(MODEL_DIR, "sd-v1-5-inpainting.ckpt"),
                     "sd-inpaint"),
    # Real-ESRGAN x4plus (xinntao release, sha256 pinned 2026-09-05 against
    # the lllyasviel/Annotators HF mirror's LFS oid). Loaded via spandrel;
    # counts as THE resident model — diffusion pipes are evicted first.
    "esrgan": (os.path.join(MODEL_DIR, "RealESRGAN_x4plus.pth"), "esrgan"),
}
# Diffusers-layout aux files for flux (scheduler/text encoders/tokenizers/VAE)
# mirrored from cocktailpeanut/xulf-s (ungated copy of FLUX.1-schnell, which
# went gated:auto on HF 2026; curl'd via resolve/main per standing workaround).
FLUX_DIR = os.path.join(MODEL_DIR, "flux-schnell")
DEFAULT_NEG = "text, watermark, signature, lowres, deformed, extra limbs"

app = Flask(__name__)
_pipes = {}


def _load(name):
    if name in _pipes:
        return _pipes[name]
    # Only one model may own the GPU: evict everything else first (measured
    # 2026-09-03: sd15 resident + sdxl loading = CUDA OOM at 7.43/7.91 GiB).
    for other in list(_pipes):
        del _pipes[other]
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    path, kind = MODELS[name]
    if not os.path.exists(path):
        raise FileNotFoundError(f"model file missing: {path}")
    if kind == "flux":
        # Flux.1-schnell: 12B transformer as GGUF Q4_K_S (stays quantized in
        # RAM, dequantized per-layer to fp32 at compute time — fp32 because
        # Pascal fp16 is 1/64 rate). T5-xxl kept fp16 as STORAGE only (9.5 GB
        # vs 19 GB fp32); it runs once per prompt and its output embeds are
        # cast to fp32 before the transformer (GGUFLinear does NOT cast
        # inputs). Sequential offload streams layers over PCIe: ~8 GB VRAM
        # card runs a 12B model, minutes per image, that's the deal.
        from transformers import T5EncoderModel
        from diffusers import (FluxPipeline, FluxTransformer2DModel,
                               GGUFQuantizationConfig)
        # accelerate's offload hooks rebuild params as param_cls(new_value)
        # WITHOUT the quant_type kwarg, so GGUF layers lose their quant type
        # on device moves and forward() dies with KeyError: None (diffusers
        # 0.32.2 + accelerate 1.1.1, verified 2026-09-04). Inherit quant_type
        # from the source tensor, which __torch_function__ preserves.
        from diffusers.quantizers.gguf.utils import GGUFParameter
        if not getattr(GGUFParameter, "_helix_patched", False):
            _orig_new = GGUFParameter.__new__

            def _patched_new(cls, data=None, requires_grad=False,
                             quant_type=None):
                if quant_type is None:
                    quant_type = getattr(data, "quant_type", None)
                return _orig_new(cls, data, requires_grad, quant_type)

            GGUFParameter.__new__ = _patched_new
            GGUFParameter._helix_patched = True
        transformer = FluxTransformer2DModel.from_single_file(
            path,
            quantization_config=GGUFQuantizationConfig(
                compute_dtype=torch.float32),
            config=os.path.join(FLUX_DIR, "transformer"),
            torch_dtype=torch.float32)
        te2 = T5EncoderModel.from_pretrained(
            FLUX_DIR, subfolder="text_encoder_2", torch_dtype=torch.float16)
        pipe = FluxPipeline.from_pretrained(
            FLUX_DIR, transformer=transformer, text_encoder_2=te2,
            torch_dtype=torch.float32)
        pipe.enable_sequential_cpu_offload()
        pipe.enable_vae_slicing()
        pipe.enable_vae_tiling()
        _pipes[name] = pipe
        return pipe
    if kind == "esrgan":
        # spandrel loads the RRDBNet arch straight from the .pth; fp32 on
        # Pascal. The upscaler IS the resident model while loaded (diffusion
        # pipes were evicted above).
        from spandrel import ModelLoader
        model = ModelLoader().load_from_file(path)
        model.cuda().eval()
        _pipes[name] = model
        return model
    if kind == "sdxl":
        from diffusers import StableDiffusionXLPipeline as P
    elif kind == "sd-inpaint":
        from diffusers import StableDiffusionInpaintPipeline as P
    else:
        from diffusers import StableDiffusionPipeline as P
    kw = dict(torch_dtype=torch.float32)
    if kind in ("sd", "sd-inpaint"):
        kw["safety_checker"] = None
    pipe = P.from_single_file(path, **kw)
    pipe.enable_attention_slicing()
    if kind == "sdxl":
        # 8 GB cannot hold SDXL fp32 AT ALL — the UNet alone is ~10 GB, so
        # even whole-module offload OOMs (measured 2026-09-03). Sequential
        # offload streams LAYER by layer: slow (PCIe-bound) but fits.
        pipe.enable_sequential_cpu_offload()
        pipe.enable_vae_slicing()
        pipe.enable_vae_tiling()
    else:
        pipe = pipe.to("cuda")
    _pipes[name] = pipe
    return pipe


@app.get("/health")
def health():
    return jsonify(ok=True, loaded=list(_pipes),
                   gpu=torch.cuda.get_device_name(0))


@app.post("/gen")
def gen():
    q = request.get_json(force=True)
    name = q.get("model", "sd15")
    if name not in MODELS:
        return jsonify(error=f"unknown model {name}"), 400
    if name == "sd15-inpaint":
        return jsonify(error="use POST /inpaint for the inpainting model"), 400
    if name == "esrgan":
        return jsonify(error="use POST /upscale for the upscaler"), 400
    pipe = _load(name)
    seed = q.get("seed")
    g = torch.Generator("cuda")
    if seed is not None:
        g = g.manual_seed(int(seed))
    t0 = time.time()
    if name == "flux":
        # schnell: 4-step distilled, guidance 0, no negative prompt (no CFG),
        # max_sequence_length 256 (its T5 training length). Encode manually so
        # the fp16-T5 embeds can be cast to fp32 for the fp32 transformer.
        count = int(q.get("count", 1))
        # no_grad is REQUIRED here: unlike pipe.__call__, encode_prompt has
        # no @torch.no_grad decorator, and T5-xxl's autograd graph alone
        # OOMs the 8 GB card (measured 2026-09-04: 7.36 GiB retained).
        with torch.no_grad():
            emb, pooled, _ = pipe.encode_prompt(
                prompt=q["prompt"], prompt_2=None,
                max_sequence_length=256, num_images_per_prompt=count)
        out = pipe(prompt_embeds=emb.to(torch.float32),
                   pooled_prompt_embeds=pooled.to(torch.float32),
                   width=int(q.get("width", 768)),
                   height=int(q.get("height", 768)),
                   num_inference_steps=int(q.get("steps", 4)),
                   guidance_scale=0.0,
                   generator=g)
    else:
        out = pipe(q["prompt"],
                   negative_prompt=q.get("negative", DEFAULT_NEG),
                   width=int(q.get("width", 512)),
                   height=int(q.get("height", 512)),
                   num_inference_steps=int(q.get("steps", 28)),
                   num_images_per_prompt=int(q.get("count", 1)),
                   generator=g)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    files = []
    for i, im in enumerate(out.images):
        fn = f"{stamp}-{name}-{i}.png"
        im.save(os.path.join(OUT, fn))
        files.append({"file": os.path.join(OUT, fn),
                      "url": f"/static/render/{fn}"})
    return jsonify(ok=True, images=files, model=name,
                   seconds=round(time.time() - t0, 1))


def _save_images(images, tag):
    """Save PIL images into static/render with the daemon's naming scheme."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    files = []
    for i, im in enumerate(images):
        fn = f"{stamp}-{tag}-{i}.png"
        im.save(os.path.join(OUT, fn))
        files.append({"file": os.path.join(OUT, fn),
                      "url": f"/static/render/{fn}"})
    return files


@app.post("/img2img")
def img2img():
    """Variations: re-noise an existing image and re-denoise with a prompt.
    Built with .from_pipe(text2img pipe) — shares the resident model's weights,
    so no extra download/VRAM and the one-model-owns-the-GPU rule is untouched.
    POST {image_path, prompt, negative?, model:"sd15"|"sdxl", strength, steps,
          seed, count} -> {ok, images:[{file,url}], model, seconds}
    """
    q = request.get_json(force=True)
    name = q.get("model", "sd15")
    if name not in ("sd15", "sdxl"):
        return jsonify(error=f"img2img supports sd15/sdxl, got {name}"), 400
    path = q.get("image_path", "")
    if not os.path.isfile(path):
        return jsonify(error=f"image not found: {path}"), 400
    from PIL import Image as PILImage
    init = PILImage.open(path).convert("RGB")
    base = _load(name)
    if name == "sdxl":
        from diffusers import StableDiffusionXLImg2ImgPipeline as P
    else:
        from diffusers import StableDiffusionImg2ImgPipeline as P
    pipe = P.from_pipe(base)  # fp32 + offload settings inherited from parent
    seed = q.get("seed")
    g = torch.Generator("cuda")
    if seed is not None:
        g = g.manual_seed(int(seed))
    t0 = time.time()
    out = pipe(q["prompt"],
               image=init,
               negative_prompt=q.get("negative", DEFAULT_NEG),
               strength=float(q.get("strength", 0.55)),
               num_inference_steps=int(q.get("steps", 28)),
               num_images_per_prompt=int(q.get("count", 1)),
               generator=g)
    return jsonify(ok=True, images=_save_images(out.images, f"{name}-i2i"),
                   model=name, seconds=round(time.time() - t0, 1))


@app.post("/inpaint")
def inpaint():
    """Repaint the white region of a mask with what the prompt says.
    Model key "sd15-inpaint" (runwayml SD1.5 inpainting ckpt) — participates
    in the one-model-owns-the-GPU eviction like any other model.
    POST {image_path, mask_path (white=repaint), prompt, negative?, steps,
          seed, count} -> {ok, images:[{file,url}], model, seconds}
    """
    q = request.get_json(force=True)
    img_path = q.get("image_path", "")
    mask_path = q.get("mask_path", "")
    if not os.path.isfile(img_path):
        return jsonify(error=f"image not found: {img_path}"), 400
    if not os.path.isfile(mask_path):
        return jsonify(error=f"mask not found: {mask_path}"), 400
    from PIL import Image as PILImage
    init = PILImage.open(img_path).convert("RGB")
    mask = PILImage.open(mask_path).convert("L").resize(init.size)
    pipe = _load("sd15-inpaint")
    seed = q.get("seed")
    g = torch.Generator("cuda")
    if seed is not None:
        g = g.manual_seed(int(seed))
    t0 = time.time()
    out = pipe(q["prompt"],
               image=init,
               mask_image=mask,
               width=init.size[0],
               height=init.size[1],
               negative_prompt=q.get("negative", DEFAULT_NEG),
               num_inference_steps=int(q.get("steps", 28)),
               num_images_per_prompt=int(q.get("count", 1)),
               generator=g)
    return jsonify(ok=True, images=_save_images(out.images, "sd15-inpaint"),
                   model="sd15-inpaint", seconds=round(time.time() - t0, 1))


def _tile_starts(total, tile, stride):
    if total <= tile:
        return [0]
    s = list(range(0, total - tile + 1, stride))
    if s[-1] + tile < total:
        s.append(total - tile)
    return s


@app.post("/upscale")
def upscale():
    """Real-ESRGAN x4. Tiled (512-px tiles, 16-px overlap) so 8 GB VRAM holds
    any input size. POST {image_path, scale:4} -> {ok, images, model, seconds}
    """
    q = request.get_json(force=True)
    scale = int(q.get("scale", 4))
    if scale != 4:
        return jsonify(error="only scale=4 supported (RealESRGAN_x4plus)"), 400
    path = q.get("image_path", "")
    if not os.path.isfile(path):
        return jsonify(error=f"image not found: {path}"), 400
    from PIL import Image as PILImage
    import numpy as np
    img = PILImage.open(path).convert("RGB")
    model = _load("esrgan")
    t0 = time.time()
    arr = np.asarray(img, dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)  # 1,C,H,W fp32
    _, _, h, w = t.shape
    tile, overlap = 512, 16
    stride = tile - overlap
    out = torch.zeros(1, 3, h * scale, w * scale)
    with torch.no_grad():
        for y in _tile_starts(h, tile, stride):
            for x in _tile_starts(w, tile, stride):
                y1, x1 = min(y + tile, h), min(x + tile, w)
                patch = t[:, :, y:y1, x:x1].cuda()
                up = model(patch).cpu()
                out[:, :, y * scale:y1 * scale, x * scale:x1 * scale] = up
    res = (out.squeeze(0).permute(1, 2, 0).clamp(0, 1).numpy()
           * 255.0).round().astype("uint8")
    im = PILImage.fromarray(res)
    return jsonify(ok=True, images=_save_images([im], "esrgan-x4"),
                   model="esrgan", seconds=round(time.time() - t0, 1))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7900, threaded=False)
