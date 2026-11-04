"""Reproducible generated-audio check: python -m soundboard.benchmark_noise.

Does not capture a microphone, open playback devices, or upload audio. Optional
--speech accepts a local test WAV; --output saves generated comparison artifacts.
"""
import argparse
import json
from math import gcd
from pathlib import Path
import time

import numpy as np
from scipy.signal import correlate, correlation_lags, resample_poly
import soundfile as sf

from .noise import FRAME_SIZE, SAMPLE_RATE, MicrophoneProcessor, RNNoiseFilter, filter_recording


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def benchmark(seconds=60):
    random = np.random.default_rng(42)
    samples = random.normal(0, 0.015, (int(seconds * 100), FRAME_SIZE)).astype(np.float32)
    timings, processed = [], []
    with RNNoiseFilter() as processor:
        for _ in range(20):
            processor.process(np.zeros(FRAME_SIZE, np.float32))
        for frame in samples:
            started = time.perf_counter()
            processed.append(processor.process(frame))
            timings.append((time.perf_counter() - started) * 1000)
    report = {
        'audio_seconds_processed': seconds,
        'frame_ms_p50': float(np.percentile(timings, 50)),
        'frame_ms_p95': float(np.percentile(timings, 95)),
        'frame_ms_max': max(timings),
        'compute_seconds': sum(timings) / 1000,
        'noise_only_reduction_db': 20 * np.log10(
            max(rms(samples[100:].ravel()), 1e-12) / max(rms(np.asarray(processed)[100:].ravel()), 1e-12)),
    }
    worker = MicrophoneProcessor()
    latencies = []
    try:
        deadline = time.perf_counter()
        for frame in samples[:300]:
            started = time.perf_counter()
            worker.submit(frame)
            while worker.output.available < FRAME_SIZE:
                if worker.error:
                    raise RuntimeError(worker.error)
                if time.perf_counter() - started > 1:
                    raise RuntimeError('Filter worker timed out.')
                time.sleep(0.0005)
            worker.read(FRAME_SIZE)
            latencies.append((time.perf_counter() - started) * 1000)
            deadline += 0.01
            time.sleep(max(0, deadline - time.perf_counter()))
        report['worker_roundtrip_ms_p95'] = float(np.percentile(latencies, 95))
        report['worker_dropped_samples'] = worker.dropped_samples
        report['worker_remaining_samples'] = worker.input.available + worker.output.available
    finally:
        worker.stop()
    return report


def speech_comparison(path, folder):
    speech, rate = sf.read(str(path), dtype='float32', always_2d=True)
    speech = speech.mean(axis=1)
    divisor = gcd(rate, SAMPLE_RATE)
    speech = resample_poly(speech, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)
    speech *= 0.35 / max(float(np.max(abs(speech))), 1e-6)
    # A short pause gives the model a noise-only lead-in. A quieter repetition
    # probes natural speech preservation without a hard voice-activity gate.
    reference = np.concatenate([np.zeros(SAMPLE_RATE), speech, np.zeros(SAMPLE_RATE),
                                speech * 0.2, np.zeros(SAMPLE_RATE)]).astype(np.float32)
    random = np.random.default_rng(13)
    noise = random.normal(0, 0.009, len(reference)).astype(np.float32)
    t = np.arange(len(reference)) / SAMPLE_RATE
    noise += (0.006 * np.sin(2 * np.pi * 120 * t)).astype(np.float32)
    for offset in range(SAMPLE_RATE, len(noise) - 1000, SAMPLE_RATE // 3):
        click = random.normal(0, 0.035, 1000) * np.exp(-np.arange(1000) / 120)
        noise[offset:offset + 1000] += click.astype(np.float32)
    noisy = reference + noise
    filtered = filter_recording(noisy)
    correlation = correlate(filtered, reference, mode='full', method='fft')
    lags = correlation_lags(len(filtered), len(reference), mode='full')
    valid = (lags >= 0) & (lags <= 4800)
    delay = int(lags[valid][np.argmax(correlation[valid])])
    aligned = filtered[delay:delay + len(reference)]
    reference = reference[:len(aligned)]
    noisy = noisy[:len(aligned)]
    voiced = abs(reference) > 0.002

    def aligned_snr(candidate):
        target = reference[voiced]
        result = candidate[voiced]
        scale = float(np.dot(result, target) / max(np.dot(target, target), 1e-12))
        return 20 * np.log10(max(rms(target * scale), 1e-12) /
                            max(rms(result - target * scale), 1e-12))

    folder.mkdir(parents=True, exist_ok=True)
    for name, audio in [('reference', reference), ('noisy', noisy), ('filtered', filtered)]:
        sf.write(str(folder / (name + '.wav')), audio, SAMPLE_RATE, subtype='FLOAT')
    return {'measured_signal_delay_ms': delay / SAMPLE_RATE * 1000,
            'speech_region_snr_before_db': float(aligned_snr(noisy)),
            'speech_region_snr_after_db': float(aligned_snr(aligned)),
            'fixture': 'Local speech mixed with generated fan-like noise and clicks; not a Krisp comparison.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--speech', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.seconds < 3:
        parser.error('--seconds must be at least 3')
    if args.speech and not args.output:
        parser.error('--speech requires --output for comparison files')
    report = benchmark(args.seconds)
    if args.speech:
        report.update(speech_comparison(args.speech, args.output))
    report['limits'] = 'Generated audio only. Does not measure device/network latency or establish perceptual parity with Krisp.'
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'filter-benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
