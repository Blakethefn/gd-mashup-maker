import math
import struct
import tempfile
import unittest
import warnings
import wave
from pathlib import Path

from mashup_app.audio_io import waveform_peaks


class WaveformPeakTests(unittest.TestCase):
    def test_reduces_audio_to_real_normalized_min_max_peaks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tone.wav"
            rate = 8_000
            samples = [int(math.sin(2 * math.pi * 220 * i / rate) * 24_000) for i in range(rate // 5)]
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                peaks, duration_ms = waveform_peaks(str(path), bins=64)

        self.assertEqual(len(peaks), 64)
        self.assertGreaterEqual(duration_ms, 190)
        self.assertTrue(any(low < -0.8 and high > 0.8 for low, high in peaks))
        self.assertTrue(all(-1.0 <= low <= high <= 1.0 for low, high in peaks))


if __name__ == "__main__":
    unittest.main()
