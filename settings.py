import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


VSS_URL = (_env("VSS_URL", "INGRESS_URL") or "").rstrip("/")
VSS_USERNAME = _env("VSS_USERNAME", "USERNAME")
VSS_PASSWORD = _env("VSS_PASSWORD", "PASSWORD")

WANDB_API_KEY = _env("WANDB_API_KEY")
WANDB_PROJECT_PATH = f"{_env('WANDB_TEAM', default='')}/{_env('WANDB_PROJECT', default='')}"
WANDB_BASE_URL = _env("WANDB_BASE_URL", default="https://api.inference.wandb.ai/v1")

DIRECTOR_MODEL = _env("DIRECTOR_MODEL", default="deepseek-ai/DeepSeek-V4-Flash")
VERIFIER_MODEL = _env("VERIFIER_MODEL", default="deepseek-ai/DeepSeek-V4-Flash")

LOCATION = "toronto"
FILM_SECONDS = 30.0
SHOT_COUNT = 6
SEGMENT_SECONDS = 5.0
CHUNK_SECONDS = 30.0

DATA_DIR = Path(_env("DIRECTOR_DATA_DIR", default=str(ROOT / "data")))
RENDER_DIR = DATA_DIR / "renders"
CACHE_DIR = DATA_DIR / "segments"
VOICE_DIR = DATA_DIR / "voices"
PIPER_VOICE = _env("PIPER_VOICE", default="en_US-ryan-high")

# In a CPU-limited pod the host core count is far above the quota; 0 keeps library defaults.
CPU_THREADS = int(_env("CPU_THREADS", default="0"))
FFMPEG_THREADS = int(_env("FFMPEG_THREADS", default="0"))
