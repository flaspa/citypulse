# CityPulse

**Ask the city. It answers in video.**

CityPulse is a real-time video agent that turns hours of urban footage into grounded video answers.

Ask a natural-language question such as:

> **Where do drivers slow down for road work?**

CityPulse searches the indexed footage, retrieves the strongest evidence, selects six complementary shots, writes a grounded narration, and automatically directs them into a 30-second film.

## Live Demo

**CityPulse:** https://team-37-app.thecosmoslabs.com/app/

## What it does

City video archives contain enormous amounts of useful information, but finding the few moments that answer a specific question is still slow and manual.

CityPulse turns those archives into a conversational visual knowledge source:

1. Ask a question about the city.
2. Semantically search the indexed video archive.
3. Surface matching moments and their locations in the source footage.
4. Use YOLO11 detections as object-level evidence.
5. Select six complementary shots.
6. Generate a narration grounded in those shots.
7. Assemble the source footage into a narrated 30-second film.

Every shot in the final answer comes from the original licensed footage — the AI understands, retrieves, reasons, directs, and edits rather than generating synthetic video.

## Architecture

```text
Natural-language question
        |
        v
VAST Video Search & Summary
        |
        +--> NVIDIA Cosmos Embed / Cosmos Reason
        |       semantic video understanding
        |
        +--> YOLO11
        |       stored object detections
        |
        v
Candidate video moments
        |
        v
CityPulse AI Director
W&B serverless inference on CoreWeave
        |
        +--> selects 6 shots
        +--> orders the edit
        +--> writes grounded narration
        |
        v
Piper TTS + FFmpeg
        |
        v
30-second narrated video answer
