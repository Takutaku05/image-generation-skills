#!/usr/bin/env python3
"""generate_image.py — Google AI Studio (Gemini API) で画像を生成する。

段階 (tier):
  proto  … *-flash-lite-image  最速・最安。試作とプロンプト調整用
  final  … *-flash-image       汎用。ユーザーに見せる本番用
  ultra  … *-pro-image         最高品質。--confirm-ultra が無いと実行を拒否する（許可制）

モデル ID は固定しない。キーで利用できる各系統の最新安定版を毎回自動選択し、
結果を 24 時間キャッシュする（新バージョンが出れば自動で追従する）。
API キーは GEMINI_API_KEY（環境変数 or ローカルの .env）。
課金される生成には --confirm-spend が必要。

使い方の例:
  generate_image.py "A flat illustration" --tier proto --confirm-spend
  generate_image.py "..." --tier final --aspect 16:9 --size 2K -n 2 --name hero --confirm-spend
  generate_image.py "Repaint this in watercolor" --ref input.png --tier final -o out.png --confirm-spend
  generate_image.py --list-models
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn

SKILL_DIR = Path(__file__).resolve().parent.parent

MODEL_CACHE_TTL = 24 * 3600
DEFAULT_OUTPUT_DIRNAME = "generated-images"

ENV_KEYS = {
    "GEMINI_API_KEY",
    "IMAGE_GEN_ALLOW_PREVIEW",
    "IMAGE_GEN_MODEL_PROTO",
    "IMAGE_GEN_MODEL_FINAL",
    "IMAGE_GEN_MODEL_ULTRA",
    "IMAGE_GEN_DEFAULT_ASPECT",
    "IMAGE_GEN_JOBS",
    "IMAGE_GEN_OUTPUT_ROOT",
    "IMAGE_GEN_TIMEOUT",
}

# 料金は https://ai.google.dev/gemini-api/docs/pricing の 2026-09 時点の値（USD/枚）。
# 画像出力分のみの概算。入力・その他の課金や変更後の料金は含まない。
TIERS: dict[str, dict] = {
    "proto": {
        "family": "flash-lite-image",
        "label": "Nano Banana Lite (prototype)",
        "fallback": "gemini-3.1-flash-lite-image",
        "prices": {"1K": 0.0336},
    },
    "final": {
        "family": "flash-image",
        "label": "Nano Banana (final)",
        "fallback": "gemini-3.1-flash-image",
        "prices": {"0.5K": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
    },
    "ultra": {
        "family": "pro-image",
        "label": "Nano Banana Pro (ultra, permission required)",
        "fallback": "gemini-3-pro-image",
        "prices": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    },
}
MODEL_RE = re.compile(
    r"^gemini-(?P<ver>\d+(?:\.\d+)?)-(?P<family>flash-lite-image|flash-image|pro-image)"
    r"(?P<suffix>-.*)?$"
)
ASPECT_RE = re.compile(r"^\d{1,2}:\d{1,2}$")
# Google API keys appear as AIza... (legacy) or AQ.... (current). Both must be
# scrubbed from anything we print, whichever key the caller happens to use.
SECRET_RE = re.compile(r"AIza[0-9A-Za-z_-]{25,}|AQ\.[A-Za-z0-9_.-]{20,}")
EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- env


def load_env() -> None:
    """Populate os.environ from the first .env found. Real env vars win."""
    candidates = [
        os.environ.get("GEMINI_ENV_FILE"),
        Path.cwd() / ".env",
        SKILL_DIR / ".env",
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand).expanduser()
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            if key not in ENV_KEYS or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                os.environ[key] = value
        return


# ------------------------------------------------------------------------- models


def redact_secret(value: str) -> str:
    """Remove the configured key and recognizable Google API keys from diagnostics."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        value = value.replace(key, "[REDACTED]")
    return SECRET_RE.sub("[REDACTED]", value)


def api_message(exc: Exception) -> str:
    """Condense and redact a google-genai error into one readable line."""
    message = getattr(exc, "message", None)
    status = getattr(exc, "code", None)
    if message:
        result = f"{status} {message}" if status else str(message)
    else:
        result = f"{type(exc).__name__}: {exc}"
    return redact_secret(result)


def rank_model(name: str) -> tuple | None:
    """(version, stable, date) sort key for an image model id; None if not one."""
    match = MODEL_RE.match(name)
    if not match:
        return None
    version = tuple(int(p) for p in match.group("ver").split("."))
    version = version + (0,) * (2 - len(version))  # gemini-4 sorts as 4.0
    suffix = match.group("suffix") or ""
    stable = 0 if ("preview" in suffix or "exp" in suffix) else 1
    date = re.search(r"(\d{2})-(\d{4})$", suffix)
    date_key = (int(date.group(2)), int(date.group(1))) if date else (0, 0)
    return (version, stable, date_key)


def model_family(name: str) -> str | None:
    match = MODEL_RE.match(name)
    return match.group("family") if match else None


def validate_tier_model(tier: str, model: str) -> None:
    """A pinned model must not bypass the selected tier's billing gate."""
    family = model_family(model)
    if family != TIERS[tier]["family"]:
        die(f"model {model!r} does not belong to tier {tier!r}")


def model_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "gemini-image" / "models.json"


def list_image_models(client) -> list[str]:
    """All generateContent-capable image model ids the key can reach."""
    names = []
    for model in client.models.list():
        name = (model.name or "").removeprefix("models/")
        actions = model.supported_actions or []
        if actions and "generateContent" not in actions:
            continue
        if rank_model(name):
            names.append(name)
    return names


def resolve_models(client, allow_preview: bool) -> dict[str, str]:
    """Newest model per tier. Cached for a day; falls back to known ids on failure."""
    cache = model_cache_path()
    cache_key = f"preview={allow_preview}"
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < MODEL_CACHE_TTL:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key and set(cached.get("models", {})) == set(TIERS):
                return cached["models"]
    except Exception:  # a corrupt cache must never block generation
        pass

    chosen: dict[str, str] = {}
    try:
        names = list_image_models(client)
    except Exception as exc:
        print(f"warning: could not list models ({api_message(exc)}); using fallback ids",
              file=sys.stderr)
        return {tier: spec["fallback"] for tier, spec in TIERS.items()}

    for tier, spec in TIERS.items():
        ranked = [(rank_model(n), n) for n in names if model_family(n) == spec["family"]]
        stable = [r for r in ranked if r[0][1] == 1]
        pool = ranked if allow_preview else stable
        if not pool and ranked:
            print(f"warning: no stable {spec['family']} model; using a preview build for {tier}",
                  file=sys.stderr)
            pool = ranked
        if not pool:
            print(f"warning: no {spec['family']} model reachable; using fallback "
                  f"{spec['fallback']} for {tier}", file=sys.stderr)
            chosen[tier] = spec["fallback"]
            continue
        chosen[tier] = max(pool)[1]

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": cache_key, "models": chosen,
                                     "resolved_at": dt.datetime.now().isoformat(timespec="seconds")}),
                         encoding="utf-8")
    except Exception:  # caching is an optimisation, not a requirement
        pass
    return chosen


# ---------------------------------------------------------------------------- api


def rate_limit_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if this is not a transient error."""
    text = str(getattr(exc, "message", "") or "") + str(exc)
    code = getattr(exc, "code", None)
    transient = code in (429, 500, 503) or any(
        k in text for k in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "overloaded"))
    if not transient:
        return None
    match = re.search(r"retry in ([\d.]+)s", text)
    if match:
        return min(float(match.group(1)) + 1.0, 120.0)
    return min(10.0 * (2 ** attempt), 120.0)


def with_retry(call, label: str, attempts: int = 5):
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            delay = rate_limit_delay(exc, attempt)
            if delay is None or attempt == attempts - 1:
                raise
            print(f"  transient error on {label} ({api_message(exc)}); retrying in "
                  f"{delay:.0f}s ({attempt + 1}/{attempts - 1})", file=sys.stderr)
            time.sleep(delay)


def load_reference(path: Path):
    from google.genai import types

    if not path.is_file():
        die(f"reference image not found: {path}")
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    if not mime.startswith("image/"):
        die(f"reference must be an image (got {mime}): {path}")
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def generate_once(client, model: str, prompt: str, refs: list, aspect: str, size: str):
    """One API call. Returns (images: list[(bytes, mime)], texts: list[str])."""
    from google.genai import types

    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size),
    )
    contents = [prompt, *refs] if refs else prompt
    response = client.models.generate_content(model=model, contents=contents, config=config)

    images: list[tuple[bytes, str]] = []
    texts: list[str] = []
    candidate = (response.candidates or [None])[0]
    if candidate is None or candidate.content is None or not candidate.content.parts:
        feedback = getattr(response, "prompt_feedback", None)
        finish = getattr(candidate, "finish_reason", None) if candidate else None
        raise RuntimeError(f"no content returned (finish_reason={finish}, prompt_feedback={feedback})")
    for part in candidate.content.parts:
        inline = getattr(part, "inline_data", None)
        if inline is not None and inline.data:
            data = inline.data
            if isinstance(data, str):
                data = base64.b64decode(data)
            images.append((data, inline.mime_type or "image/png"))
        elif getattr(part, "text", None):
            texts.append(part.text)
    if not images:
        raise RuntimeError("model returned no image" + (f": {' '.join(texts)[:300]}" if texts else ""))
    return images, texts


# --------------------------------------------------------------------------- files


def next_free_path(directory: Path, stem: str, ext: str) -> Path:
    """<stem>_01.png, _02 … skipping names that already exist (never overwrite)."""
    index = 1
    while True:
        cand = directory / f"{stem}_{index:02d}{ext}"
        if not cand.exists() and not cand.with_suffix(".json").exists():
            return cand
        index += 1


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


# --------------------------------------------------------------------------- main


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="?", help="生成プロンプト（英語推奨。@file.txt でファイルから読む）")
    p.add_argument("--tier", choices=TIERS, default="proto",
                   help="proto=試作(Lite) / final=本番(Flash) / ultra=最高品質(Pro, 許可制)。既定 proto")
    p.add_argument("--model", help="モデル ID を固定（通常は不要。tier ごとの自動選択を上書き）")
    p.add_argument("--confirm-ultra", action="store_true",
                   help="ユーザーから Pro(ultra) 使用の許可を得たことを宣言する。無いと ultra は拒否")
    p.add_argument("--confirm-spend", action="store_true",
                   help="この依頼の API 課金についてユーザーの許可を得たことを宣言する。生成時は必須")
    p.add_argument("--aspect", help="縦横比 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 / 21:9 など。"
                                    "既定 $IMAGE_GEN_DEFAULT_ASPECT または 1:1")
    p.add_argument("--size", default="1K", choices=["0.5K", "1K", "2K", "4K"],
                   help="解像度。proto は 1K のみ。既定 1K")
    p.add_argument("-n", "--count", type=int, default=1, help="生成枚数（1 枚ごとに 1 リクエスト）")
    p.add_argument("--ref", action="append", default=[], metavar="IMAGE",
                   help="参考画像（編集・スタイル参照）。複数指定可、最大 14 枚")
    p.add_argument("-o", "--output", help="出力ファイルパス（1 枚のとき）。省略時は --out-dir に自動命名")
    p.add_argument("--out-dir", help=f"出力ディレクトリ。既定 <cwd>/{DEFAULT_OUTPUT_DIRNAME}/YYYYMMDD/")
    p.add_argument("--name", default="img", help="自動命名の接頭辞。<name>_<tier>_<NN>.<画像形式>")
    p.add_argument("--jobs", type=int, help="並列リクエスト数。既定 $IMAGE_GEN_JOBS または 2")
    p.add_argument("--allow-preview", action="store_true",
                   help="preview / experimental モデルも自動選択の候補に含める")
    p.add_argument("--list-models", action="store_true", help="キーで使える画像モデルを一覧して終了")
    p.add_argument("--dry-run", action="store_true", help="モデル解決と出力計画だけ表示して終了（画像生成なし）")
    p.add_argument("--no-meta", action="store_true", help="再現用の .json サイドカーを書かない")
    return p.parse_args()


def main() -> int:
    load_env()
    args = parse_args()

    if not args.list_models and not args.dry_run and not args.confirm_spend:
        die("image generation may incur charges. Get the user's approval for this task, "
            "then pass --confirm-spend.", code=2)
    if not args.list_models and not args.dry_run and args.tier == "ultra" and not args.confirm_ultra:
        die("tier 'ultra' (Nano Banana Pro) は許可制です。最新料金を確認し、ユーザーに伝えて"
            "許可を得てから --confirm-ultra を付けて再実行してください。", code=2)
    if args.count < 1:
        die("--count must be >= 1")
    if len(args.ref) > 14:
        die("at most 14 reference images are supported")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        die("GEMINI_API_KEY is not set. Copy .env.example in the skill directory to .env and fill in "
            "the key from https://aistudio.google.com/apikey")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        die("google-genai is not installed: python3 -m pip install -U google-genai")

    timeout_s = float(os.environ.get("IMAGE_GEN_TIMEOUT", "300"))
    # The SDK logs an 'automatic function calling' warning on every generate_content
    # call even when no tools are used; it is noise here.
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    allow_preview = args.allow_preview or truthy(os.environ.get("IMAGE_GEN_ALLOW_PREVIEW"))

    if args.list_models:
        try:
            names = sorted(list_image_models(client), key=lambda n: (model_family(n), rank_model(n)))
        except Exception as exc:
            die(f"could not list models: {api_message(exc)}")
        resolved = resolve_models(client, allow_preview)
        tier_of = {spec["family"]: tier for tier, spec in TIERS.items()}
        for name in names:
            rank = rank_model(name)
            tier = tier_of[model_family(name)]
            mark = "  <- " + tier if resolved.get(tier) == name else ""
            print(f"{name:45s} tier={tier:5s} {'stable' if rank[1] else 'preview'}{mark}")
        return 0

    if not args.prompt:
        die("prompt is required (or use --list-models)")
    prompt = args.prompt
    if prompt.startswith("@"):
        prompt = Path(prompt[1:]).expanduser().read_text(encoding="utf-8").strip()

    pinned = args.model or os.environ.get(f"IMAGE_GEN_MODEL_{args.tier.upper()}", "").strip()
    if pinned and pinned.lower() != "auto":
        model = pinned.removeprefix("models/")
    else:
        model = resolve_models(client, allow_preview)[args.tier]
    validate_tier_model(args.tier, model)

    aspect = args.aspect or os.environ.get("IMAGE_GEN_DEFAULT_ASPECT", "").strip() or "1:1"
    if not ASPECT_RE.match(aspect):
        die(f"invalid --aspect {aspect!r}; use W:H like 16:9")
    size = args.size
    prices = TIERS[args.tier]["prices"]
    if size not in prices:
        if args.tier == "proto":
            print(f"warning: proto (Lite) supports 1K only; using 1K instead of {size}", file=sys.stderr)
            size = "1K"
        else:
            print(f"warning: {size} is not listed for tier {args.tier}; the API may reject it",
                  file=sys.stderr)
    unit_price = prices.get(size)

    if args.output and args.count == 1:
        out_path = Path(args.output).expanduser()
        out_dir = out_path.parent
        targets = [out_path]
        stem = out_path.stem  # extra images in one response fall back to <stem>_NN
    else:
        if args.out_dir:
            out_dir = Path(args.out_dir).expanduser()
        elif args.output:
            out_dir = Path(args.output).expanduser().parent
        else:
            root = Path(os.environ.get("IMAGE_GEN_OUTPUT_ROOT") or (Path.cwd() / DEFAULT_OUTPUT_DIRNAME))
            out_dir = root / dt.date.today().strftime("%Y%m%d")
        stem = Path(args.output).stem if args.output else f"{args.name}_{args.tier}"
        targets = []  # named lazily after each image arrives (extension depends on mime)

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "tier": args.tier, "model": model, "aspect": aspect, "size": size, "count": args.count,
        "refs": [str(Path(r)) for r in args.ref], "out_dir": str(out_dir),
        "cost_usd_est": round(unit_price * args.count, 4) if unit_price else None,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "prompt": prompt, **plan}, ensure_ascii=False, indent=2))
        return 0

    refs = [load_reference(Path(r).expanduser()) for r in args.ref]
    jobs = max(1, args.jobs or int(os.environ.get("IMAGE_GEN_JOBS", "2")))
    print(f"generating {args.count} image(s) with {model} [{args.tier}] {aspect} {size} …",
          file=sys.stderr)
    started = time.time()

    def run(index: int):
        return with_retry(lambda: generate_once(client, model, prompt, refs, aspect, size),
                          label=f"image {index + 1}/{args.count}")

    results: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(jobs, args.count)) as pool:
        futures = [pool.submit(run, i) for i in range(args.count)]
        for i, fut in enumerate(futures):
            try:
                images, texts = fut.result()
            except Exception as exc:
                errors.append(f"image {i + 1}: {api_message(exc)}")
                print(f"  image {i + 1}/{args.count} failed: {api_message(exc)}", file=sys.stderr)
                continue
            for data, mime in images:
                ext = EXT_BY_MIME.get(mime, ".png")
                if targets:
                    path = targets.pop(0)
                    if path.suffix.lower() != ext:
                        path = path.with_suffix(ext)
                    if path.exists():
                        path = next_free_path(path.parent, path.stem, ext)
                else:
                    path = next_free_path(out_dir, stem, ext)
                write_atomic(path, data)
                dims = image_dimensions(data)
                meta = {
                    "file": path.name, "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "prompt": prompt, "tier": args.tier, "model": model, "aspect": aspect,
                    "size": size, "refs": plan["refs"], "width": dims[0] if dims else None,
                    "height": dims[1] if dims else None, "mime": mime,
                    "cost_usd_est": unit_price, "model_text": " ".join(texts).strip() or None,
                }
                if not args.no_meta:
                    path.with_suffix(".json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                results.append({"path": str(path), "width": meta["width"], "height": meta["height"]})
                print(f"  saved {path} ({meta['width']}x{meta['height']})", file=sys.stderr)

    summary = {
        **plan, "files": results, "errors": errors,
        "cost_usd_est": round(unit_price * len(results), 4) if unit_price else None,
        "elapsed_s": round(time.time() - started, 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not results:
        return 3
    return 0 if not errors else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
