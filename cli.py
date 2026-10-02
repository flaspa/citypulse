"""Command-line director: python cli.py "question" [--out edl.json] [--render]"""
import argparse
import asyncio
import json
import sys
import textwrap
from pathlib import Path

import settings
from director import Director
from vss import VSS


async def log_event(stage: str, data: dict):
    if stage == "searching":
        print(f"\n[search] angle: {data['angle']}")
        for q in data["queries"]:
            print(f"   - {q}")
    elif stage == "candidates":
        sets = sorted({p["drive_set"] for p in data["pool"]})
        print(f"[retrieve] {data['count']} unique clips -> pool of {len(data['pool'])} across {', '.join(sets)}")
    elif stage == "directing":
        print("[direct] choosing 6 shots and writing narration...")
    elif stage == "narration":
        print(f"[narration] verified={data['verified']}")
    elif stage == "rendering":
        print(f"[render] {data['step']}")


def report(edl: dict):
    w = 100
    print("\n" + "=" * w)
    print(f"{edl['title']}   ({edl['narration']['words']} words, grounding verified: {edl['grounding']['verified']})")
    print("=" * w)
    print(textwrap.fill(edl["narration"]["text"], w))
    print("\nSentences and citations:")
    for s in edl["narration"]["sentences"]:
        print(f"  [{s['start']:5.1f}-{s['end']:5.1f}] {', '.join(s['clips']):12} {s['text']}")
    print("\nEdit:")
    for s in edl["shots"]:
        print(f"  {s['index']}. {s['t_start']:5.2f}-{s['t_end']:5.2f} ({s['duration']:.2f}s) {s['clip']:4} "
              f"{s['drive_set']} chunk {s['chunk_index']:>2} src {s['in']:.1f}-{s['out']:.1f}s "
              f"[{s['role']}] sim {s['similarity']:.2f}")
        print(textwrap.indent(textwrap.fill(s["caption"][:320], w - 8), "        "))
    for r in edl["grounding"]["rounds"]:
        if r["issues"]:
            print(f"  grounding round {r['round']}: {r['issues']}")
    print(f"\nTimings: {edl['timings']}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--edl", type=Path, help="render an existing EDL instead of directing a new one")
    ap.add_argument("--model", default=settings.DIRECTOR_MODEL)
    args = ap.parse_args()
    if not args.question and not args.edl:
        ap.error("give a question or --edl")

    vss = VSS()
    try:
        if args.edl:
            edl = json.loads(args.edl.read_text())
            args.render = True
        else:
            edl = await Director(vss, model=args.model).run(args.question, emit=log_event)
        if args.render:
            from render import render
            edl = await render(vss, edl, emit=log_event)
        report(edl)
        out = args.out or (settings.DATA_DIR / "edl" / f"{edl['id']}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(edl, indent=2))
        print(f"EDL written to {out}")
    finally:
        await vss.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
