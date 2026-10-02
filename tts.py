"""Piper text-to-speech: per-sentence synthesis so subtitles and cuts know real timings."""
import threading
import wave
from pathlib import Path

import httpx
import onnxruntime
from piper import PiperVoice, SynthesisConfig

import settings

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_voices: dict[str, PiperVoice] = {}
_lock = threading.Lock()


def voice_files(name: str) -> tuple[Path, Path]:
    return settings.VOICE_DIR / f"{name}.onnx", settings.VOICE_DIR / f"{name}.onnx.json"


def ensure_voice(name: str = settings.PIPER_VOICE) -> tuple[Path, Path]:
    model, config = voice_files(name)
    if model.exists() and config.exists():
        return model, config
    lang, speaker, quality = name.split("-")
    url = f"{HF_BASE}/{lang.split('_')[0]}/{lang}/{speaker}/{quality}/{name}"
    settings.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    for suffix, dest in ((".onnx", model), (".onnx.json", config)):
        tmp = dest.with_suffix(dest.suffix + ".part")
        with httpx.stream("GET", url + suffix, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.rename(dest)
    return model, config


def load_voice(name: str = settings.PIPER_VOICE) -> PiperVoice:
    with _lock:
        if name not in _voices:
            model, config = ensure_voice(name)
            voice = PiperVoice.load(model, config_path=config)
            if settings.CPU_THREADS:
                opts = onnxruntime.SessionOptions()
                opts.intra_op_num_threads = settings.CPU_THREADS
                opts.inter_op_num_threads = 1
                voice.session = onnxruntime.InferenceSession(str(model), sess_options=opts,
                                                             providers=["CPUExecutionProvider"])
            _voices[name] = voice
        return _voices[name]


def synthesize(text: str, dest: Path, length_scale: float = 1.0, name: str = settings.PIPER_VOICE) -> float:
    """Write a mono WAV for text and return its duration in seconds."""
    voice = load_voice(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wf:
        voice.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=length_scale))
    with wave.open(str(dest), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())
