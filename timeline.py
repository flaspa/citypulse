"""Edit timing: shot lengths follow how much narration attention each shot receives."""
import math

import settings

WORDS_PER_SECOND = 2.8
LEAD_IN = 0.8
TAIL = 1.4
SENTENCE_GAP = 0.35
MIN_SHOT = 3.0
MAX_SHOT = 8.0


def estimate_durations(sentences: list[dict]) -> list[float]:
    return [max(1.2, len(s["text"].split()) / WORDS_PER_SECOND) for s in sentences]


def sentence_spans(durations: list[float], gap: float = SENTENCE_GAP, lead_in: float = LEAD_IN):
    spans, t = [], lead_in
    for d in durations:
        spans.append((round(t, 3), round(t + d, 3)))
        t += d + gap
    return spans


def fit(desired: list[float], total: float, lo: float, hi: float) -> list[float]:
    """Scale desired lengths to sum to total while respecting [lo, hi] per item."""
    n = len(desired)
    out = [None] * n
    free = list(range(n))
    remaining = total
    while free:
        weight = sum(max(desired[i], 1e-6) for i in free)
        changed = False
        for i in list(free):
            v = remaining * max(desired[i], 1e-6) / weight
            if v < lo or v > hi:
                out[i] = lo if v < lo else hi
                free.remove(i)
                remaining -= out[i]
                changed = True
        if not changed:
            for i in free:
                out[i] = remaining * max(desired[i], 1e-6) / weight
            break
    return out


def shot_lengths(shot_clips: list[str], sentences: list[dict], spans: list[tuple[float, float]],
                 total: float = settings.FILM_SECONDS) -> list[float]:
    attention = {c: 0.0 for c in shot_clips}
    for (t0, t1), s in zip(spans, sentences):
        cited = [c for c in s["clips"] if c in attention]
        for c in cited:
            attention[c] += (t1 - t0) / len(cited)
    desired = [attention[c] for c in shot_clips]
    mean = (sum(desired) / len(desired)) or 1.0
    desired = [d if d > 0 else 0.6 * mean for d in desired]
    desired[0] += LEAD_IN
    desired[-1] += TAIL
    lengths = fit(desired, total, MIN_SHOT, MAX_SHOT)
    lengths = [round(x, 2) for x in lengths]
    lengths[-1] = round(total - sum(lengths[:-1]), 2)
    return lengths


PICTURE_LEADS = (0.9, 0.45, 1.25, 0.65, 1.05, 0.55)
SNAP_WINDOW = 0.8


def offset_cuts(lengths: list[float], spans: list[tuple[float, float]],
                total: float = settings.FILM_SECONDS) -> list[float]:
    """Move cuts off sentence breaks so the next image arrives ahead of its words.

    A cut that lands within SNAP_WINDOW of a sentence start is pulled earlier by a varying lead,
    which keeps picture and voice from switching in lockstep.
    """
    cuts, t = [], 0.0
    for length in lengths[:-1]:
        t += length
        cuts.append(t)
    starts = [a for a, _ in spans[1:]]
    for i, cut in enumerate(cuts):
        near = min(starts, key=lambda s: abs(s - cut), default=None)
        if near is not None and abs(near - cut) < SNAP_WINDOW:
            cuts[i] = near - PICTURE_LEADS[i % len(PICTURE_LEADS)]
    bounds = [0.0] + cuts + [total]
    desired = [max(0.1, b - a) for a, b in zip(bounds, bounds[1:])]
    fitted = fit(desired, total, MIN_SHOT, MAX_SHOT)
    fitted = [round(x, 2) for x in fitted]
    fitted[-1] = round(total - sum(fitted[:-1]), 2)
    return fitted


def source_window(segment_start: float, duration: float, chunk_seconds: float = settings.CHUNK_SECONDS):
    """Window inside the parent chunk, keeping the matched 5 s moment near the middle."""
    center = segment_start + settings.SEGMENT_SECONDS / 2
    t_in = min(max(0.0, center - duration * 0.45), chunk_seconds - duration)
    return round(t_in, 2), round(t_in + duration, 2)


def segments_for_window(t_in: float, t_out: float, total_segments: int) -> list[int]:
    first = int(math.floor(t_in / settings.SEGMENT_SECONDS)) + 1
    last = int(math.ceil(t_out / settings.SEGMENT_SECONDS - 1e-6))
    return list(range(max(1, first), min(total_segments, last) + 1))


def layout(shots: list[dict], sentences: list[dict], durations: list[float],
           gap: float = SENTENCE_GAP, lead_in: float = LEAD_IN):
    """Place sentences on the film clock and cut shots to follow them (mutates both lists).

    Each shot needs: clip, match_window, total_segments, segment_stem.
    """
    spans = sentence_spans(durations, gap=gap, lead_in=lead_in)
    for s, (a, b) in zip(sentences, spans):
        s["start"], s["end"] = a, b
    lengths = offset_cuts(shot_lengths([s["clip"] for s in shots], sentences, spans), spans)
    t = 0.0
    for shot, length in zip(shots, lengths):
        t_in, t_out = source_window(shot["match_window"][0], length)
        shot.update({
            "in": t_in,
            "out": t_out,
            "duration": length,
            "t_start": round(t, 2),
            "t_end": round(t + length, 2),
            "segments": [{"number": n, "start": (n - 1) * settings.SEGMENT_SECONDS,
                          "source": f"{shot['segment_stem']}_segment_{n:03d}_of_{shot['total_segments']:03d}.mp4"}
                         for n in segments_for_window(t_in, t_out, shot["total_segments"])],
        })
        t += length
