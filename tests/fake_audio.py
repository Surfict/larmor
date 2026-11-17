#!/usr/bin/env python3
"""A stand-in for native/larmor_audio, so an ear can be driven without a microphone.

    LARMOR_AUDIO_BIN=tests/fake_audio.py python larmor_ear_gemini.py

Speaks the same protocol as the Swift binary:
  - announces `REF_FORMAT sr=24000 ch=1` and `FORMAT sr=16000 ch=1` on stderr;
  - streams little-endian int16 mono "mic" audio on stdout at real-time pace: a WAV file
    (LARMOR_FAKE_WAV, 16 kHz mono int16), or a sentence rendered by macOS `say`
    (LARMOR_FAKE_SAY, default "Please list the files in this folder."), after
    LARMOR_FAKE_LEAD_S of silence (default 1.5 s), then silence forever, or again every
    LARMOR_FAKE_REPEAT_S seconds when that is set;
  - sinks whatever is written to its stdin (the "speaker") into LARMOR_FAKE_SPEAKER
    (default ~/.larmor/fake_speaker.pcm), int16 at 24 kHz;
  - prints `FLUSHED` on stderr on SIGUSR1, like the real binary, so barge-in ordering shows
    in the log;
  - exits on stdin EOF or SIGTERM.
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

MIC_SR, REF_SR = 16000, 24000
CHUNK_S = 0.02


def elog(s: str) -> None:
    sys.stderr.write(s + "\n")
    sys.stderr.flush()


def load_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getframerate() != MIC_SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16 kHz mono int16 WAV, got {w.getframerate()} Hz "
                             f"{w.getnchannels()} ch {w.getsampwidth() * 8}-bit")
        return w.readframes(w.getnframes())


def say_wav(text: str) -> bytes:
    """Render a sentence with macOS `say` at 16 kHz mono (afconvert does the resample)."""
    d = tempfile.mkdtemp(prefix="larmor-fake-")
    aiff, wav = os.path.join(d, "say.aiff"), os.path.join(d, "say.wav")
    subprocess.run(["say", "-v", os.getenv("LARMOR_FAKE_VOICE", "Samantha"), "-o", aiff, text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{MIC_SR}", "-c", "1", aiff, wav], check=True)
    return load_wav(wav)


def sink_stdin(path: str) -> None:
    with open(path, "wb") as f:
        while True:
            b = sys.stdin.buffer.read(4096)
            if not b:
                break
            f.write(b)
            f.flush()
    os._exit(0)                                   # EOF on stdin: the ear is gone


def main() -> None:
    signal.signal(signal.SIGUSR1, lambda *_: elog("FLUSHED"))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    speaker = os.getenv("LARMOR_FAKE_SPEAKER", os.path.expanduser("~/.larmor/fake_speaker.pcm"))
    os.makedirs(os.path.dirname(speaker), exist_ok=True)
    threading.Thread(target=sink_stdin, args=(speaker,), daemon=True).start()
    wav = os.getenv("LARMOR_FAKE_WAV")
    pcm = load_wav(wav) if wav else say_wav(os.getenv("LARMOR_FAKE_SAY", "Please list the files in this folder."))
    lead = float(os.getenv("LARMOR_FAKE_LEAD_S", "1.5"))
    elog(f"REF_FORMAT sr={REF_SR} ch=1")
    elog(f"FORMAT sr={MIC_SR} ch=1")
    elog(f"fake mic: {len(pcm) / 2 / MIC_SR:.1f}s of speech after {lead:.1f}s, speaker -> {speaker}")
    out = sys.stdout.buffer
    step = int(MIC_SR * CHUNK_S) * 2
    stream = b"\0" * (int(lead * MIC_SR) * 2) + pcm
    repeat = float(os.getenv("LARMOR_FAKE_REPEAT_S", "0"))
    if repeat:
        stream += b"\0" * (int(repeat * MIC_SR) * 2)
    t0, i, n = time.time(), 0, 0
    while True:
        if repeat and i >= len(stream):
            i = len(b"\0" * (int(lead * MIC_SR) * 2))          # speak again, with the same lead
        chunk = stream[i:i + step] if i < len(stream) else b"\0" * step
        try:
            out.write(chunk)
            out.flush()
        except BrokenPipeError:
            os._exit(0)
        i += step
        n += 1
        if i == len(stream) or (i > len(stream) and i - step < len(stream)):
            elog("fake mic: speech done, silence from here")
        time.sleep(max(0.0, t0 + n * CHUNK_S - time.time()))


if __name__ == "__main__":
    main()
