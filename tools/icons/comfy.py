"""Génération des icônes du manager avec ComfyUI : Krea 2 Turbo + LoRA de référence de style (les icônes
Lucide de l'interface, style_ref.png). Modèles : krea2_turbo_fp8_scaled, qwen3vl_4b_fp8_scaled, qwen_image_vae,
krea2_style_reference. C : adresse de ComfyUI (variable COMFY_URL)."""
import json, os, sys, time, uuid, urllib.request, urllib.parse, pathlib, random

C = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
OUT = pathlib.Path(__file__).parent / "gen"
OUT.mkdir(exist_ok=True)



def post(path, data=None, raw=None, headers=None):
    req = urllib.request.Request(C + path, data=raw if raw is not None else (json.dumps(data).encode() if data is not None else None),
                                 headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def upload(path):
    """Envoie une image de référence dans input/ de ComfyUI ; renvoie son nom."""
    b = uuid.uuid4().hex
    p = pathlib.Path(path)
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{p.name}\"\r\nContent-Type: image/png\r\n\r\n").encode() \
        + p.read_bytes() + f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{b}--\r\n".encode()
    return post("/upload/image", raw=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})["name"]


def run(graph, tag):
    pid = post("/prompt", {"prompt": graph, "client_id": "wsq-icons"})["prompt_id"]
    while True:
        time.sleep(2)
        with urllib.request.urlopen(f"{C}/history/{pid}", timeout=30) as r:
            h = json.loads(r.read())
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                raise SystemExit(json.dumps(st)[:2000])
            files = []
            for node in h[pid]["outputs"].values():
                for im in node.get("images", []):
                    q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im["subfolder"], "type": im["type"]})
                    dst = OUT / f"{tag}_{len(files)}.png"
                    urllib.request.urlretrieve(f"{C}/view?{q}", dst)
                    files.append(str(dst))
            return files


KSTYLE = ("A single icon, centered, in exactly the same style as the reference icons: uniform thick black rounded strokes, "
          "outline only, white interior, plain white background, no shading, no gray, no text, no frame.")


def krea_ref(prompt, tag, ref_name, batch=4, seed=None, size=1024, lora=1.0):
    seed = seed if seed is not None else random.randint(1, 2**31)
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_fp8_scaled.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "krea2_style_reference.safetensors", "strength_model": lora}},
        "5": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "image1": ["5", 0], "prompt": f"{prompt}. {KSTYLE}"}},
        "7": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["6", 0], "reference_latents_method": "index_timestep_zero"}},
        "8": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
        "9": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["4", 0], "max_shift": 1.15, "base_shift": 0.5, "width": size, "height": size}},
        "10": {"class_type": "CFGGuider", "inputs": {"model": ["9", 0], "positive": ["7", 0], "negative": ["8", 0], "cfg": 1}},
        "11": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "13": {"class_type": "BasicScheduler", "inputs": {"model": ["9", 0], "scheduler": "simple", "steps": 8, "denoise": 1}},
        "14": {"class_type": "EmptyLatentImage", "inputs": {"width": size, "height": size, "batch_size": batch}},
        "15": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["11", 0], "guider": ["10", 0], "sampler": ["12", 0], "sigmas": ["13", 0], "latent_image": ["14", 0]}},
        "16": {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["3", 0]}},
        "17": {"class_type": "SaveImage", "inputs": {"images": ["16", 0], "filename_prefix": f"wsq-icons/k_{tag}"}},
    }
    return run(g, "k_" + tag)


if __name__ == "__main__":
    # comfy.py kjobs.json style_ref.png : 4 propositions par icône dans gen/ (à choisir à l'œil)
    ref = upload(sys.argv[2])
    for tag, prompt in json.load(open(sys.argv[1])):
        print(tag, krea_ref(prompt, tag, ref, batch=4), flush=True)
