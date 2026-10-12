"""measure_wake_word.py
Phase 1 of the wake-word plan (docs/plans/2026-10-12-1747-wake-word-implementation.md):
what a wake check costs on this machine with your real settings, and how far
apart voices score on clips as short as a wake word. The numbers decide the
"Sounds like you" default (wake_word.voice_match) and the check budget.

Close Koli first: this loads its own Whisper model.

    python tools/measure_wake_word.py              record from the microphone
    python tools/measure_wake_word.py --device 3   a specific input device
    python tools/measure_wake_word.py --tries 10   how many labelled tries

It records your wake word 3 times (your voiceprint, like the setup screen),
then asks for tries you label: me (your word), other (someone else saying
it), tv (a video or the TV). For each try it prints Whisper's time, what it
heard, whether it would wake, and the voice score at 0.5 s, 1 s and whole."""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from koli import wake_word as ww  # noqa: E402

RATE = ww.RATE


# ── Setup: Koli's own engine and voiceprint model, from your settings ───────

def load_engine_and_embedder():
    from koli.config_manager import ConfigManager
    from koli.main import setup_vad, setup_whisper_engine
    from koli.meeting.speakers import make_embedder
    from koli.model_registry import ModelRegistry

    cm = ConfigManager(quiet=True)
    whisper_cfg = cm.get_whisper_config()
    registry = ModelRegistry(
        whisper_models_config=whisper_cfg.get('models', {}), streaming_models_config={},
        speaker_models_config=(cm.config.get('meeting') or {}).get('speaker_models') or {})
    engine = setup_whisper_engine(whisper_cfg, setup_vad(cm.get_vad_config()), registry, ask=False)
    speaker_key = str((cm.config.get('meeting') or {}).get('speaker_model') or 'eres2net')
    embed = make_embedder(registry.get_speaker_model_path(speaker_key))
    wake = cm.config.get('wake_word') or {}
    mic = (cm.config.get('audio') or {}).get('input_device')
    wake['_mic'] = mic if isinstance(mic, int) else None   # Koli's microphone; None = the default
    print(f"Model {whisper_cfg['model']} on {whisper_cfg['device']}, languages {whisper_cfg.get('languages') or 'any'}, "
          f"second model {(whisper_cfg.get('language_model') or {}).get('language') or 'off'}, voice model {speaker_key}")
    return engine, embed, whisper_cfg.get('language') or 'auto', wake


# ── Recording one burst, the way the listener cuts it ────────────────────────

def record_burst(device, seconds=5.0, max_burst=3.0):
    import sounddevice as sd
    import soxr
    from koli.meeting.capture import speech_prob_function
    # The device's own rate (WASAPI refuses others), resampled like Koli does.
    native = int(sd.query_devices(device, 'input')['default_samplerate'])
    audio = sd.rec(int(seconds * native), samplerate=native, channels=1, dtype='float32', device=device)
    sd.wait()
    audio = audio.reshape(-1)
    if native != RATE:
        audio = soxr.resample(audio, native, RATE).astype(np.float32)
    cutter = ww.BurstCutter(head_s=max_burst, end_silence_s=0.7, max_s=max_burst)
    prob = speech_prob_function()
    for start in range(0, len(audio) // ww.FRAME * ww.FRAME, ww.FRAME):
        frame = audio[start:start + ww.FRAME]
        for event, burst in cutter.feed(frame, prob(frame)):
            if event == 'end' and burst.speech_seconds() >= 0.3:
                return burst.audio()
    if cutter.burst is not None and cutter.burst.speech_seconds() >= 0.3:
        return cutter.burst.audio()
    return None


def ask_burst(device, prompt):
    while True:
        input(f"{prompt} — press Enter, then speak (5 s)… ")
        burst = record_burst(device)
        if burst is not None:
            return burst
        print("   heard no speech, again")


def unit(vec):
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


# ── Measuring ────────────────────────────────────────────────────────────────

def timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


def check(engine, audio, language):
    return engine.transcribe_audio(audio, language=language, use_prompt=False, beam_size=1,
                                   task='transcribe', quiet=True) or ''


def tap_resample_cost():
    # What a tap costs the audio thread on WASAPI: one 10 ms chunk at 48 kHz
    # resampled to 16 kHz, as AudioRecorder._notify_taps does per chunk.
    import soxr
    chunk = np.random.default_rng(0).standard_normal(480).astype(np.float32) * 0.01
    t0 = time.perf_counter()
    for _ in range(2000):
        soxr.resample(chunk, 48000, RATE)
    per_chunk = (time.perf_counter() - t0) / 2000
    print(f"\nTap resampling: {per_chunk * 1e6:.0f} µs per 10 ms chunk ({per_chunk / 0.010 * 100:.2f}% of the audio thread)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    parser.add_argument('--device', type=int, default=None, help="input device index (default: Koli's microphone)")
    parser.add_argument('--tries', type=int, default=8)
    args = parser.parse_args()

    engine, embed, language, wake = load_engine_and_embedder()
    if args.device is None:
        args.device = wake.get('_mic')
    print(f"Microphone: {args.device if args.device is not None else 'the default input'}")
    spellings = list(ww.DEFAULT_SPELLINGS)
    print("\nWarming up Whisper…")
    check(engine, np.zeros(RATE, dtype=np.float32), language)

    print("\n1) Your wake word, 3 times (as on the setup screen)")
    enrolled, heard = [], []
    for i in range(3):
        burst = ask_burst(args.device, f"   Recording {i + 1} of 3")
        heard.append(check(engine, burst, language))
        enrolled.append(unit(embed(burst)))
        print(f"   heard “{heard[-1]}” ({len(burst) / RATE:.1f} s)")
    personal = ww.spellings_from_recordings(heard)
    if personal:
        spellings = personal
        print(f"   your spellings: {personal}")
    else:
        print("   Whisper heard different words each time; testing against “Hey Koli” instead")
    centroid = unit(np.mean(enrolled, axis=0))
    # The language your word was detected in, for the fixed-language timing
    # (a normal call, so last_info says which language it picked).
    engine.transcribe_audio(burst, language='auto', use_prompt=False, beam_size=1, task='transcribe')
    fixed_language = (getattr(engine, 'last_info', None) or {}).get('language') or language
    ratio = ww.ratio_for(wake.get('sensitivity', 'medium'), personal=bool(personal))

    print(f"\n2) Tries: label each one me / other / tv. Fixed-language timing uses “{fixed_language}”.")
    print("   Make some 'me' tries one breath (“Koli, next song”): the voice is checked on the")
    print("   first 2 s of a burst, not on the word alone.")
    rows = []
    for i in range(args.tries):
        label = input(f"   Try {i + 1}/{args.tries} label [me/other/tv, empty to stop]: ").strip() or None
        if not label:
            break
        burst = ask_burst(args.device, f"   {label}")
        text, auto_s = timed(lambda: check(engine, burst, language))
        _, fixed_s = timed(lambda: check(engine, burst, fixed_language))
        scores = []
        for seconds in (0.5, 1.0, None):
            clip = burst if seconds is None else burst[:int(seconds * RATE)]
            vec = embed(clip) if len(clip) >= int(0.3 * RATE) else None
            scores.append(None if vec is None else round(float(np.dot(unit(vec), centroid)), 2))
        word = ww.match_wake(text, spellings, ratio) is not None
        rows.append((label, len(burst) / RATE, auto_s, fixed_s, word, scores, text))
        print(f"   {label:5} {len(burst) / RATE:.1f}s  whisper {auto_s:.2f}s (fixed {fixed_s:.2f}s)  "
              f"word {'yes' if word else 'no '}  voice 0.5s/1s/all {scores}  “{text}”")

    if rows:
        print("\nSummary (voice score on the whole clip):")
        for label in sorted({r[0] for r in rows}):
            got = [r[5][2] for r in rows if r[0] == label and r[5][2] is not None]
            times = [r[2] for r in rows if r[0] == label]
            if got:
                print(f"   {label:5} voice min {min(got):.2f} max {max(got):.2f}   whisper avg {sum(times) / len(times):.2f}s")
        print(f"   Current “Sounds like you” threshold: {wake.get('voice_match', ww.WakeListener.DEFAULT_VOICE_MATCH)}"
              " — it should sit between your lowest 'me' and the highest 'other'/'tv'.")
    tap_resample_cost()


if __name__ == '__main__':
    main()
