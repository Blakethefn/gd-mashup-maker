import tempfile
import unittest
import warnings
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from mashup_app.audio_graph import (
    ArraySourceNode,
    AudioGraph,
    CachedNode,
    GainPanNode,
    MixNode,
    OffsetNode,
    OutputSafetyNode,
    SmartRenderer,
    AudioGraphFactory,
)
from mashup_app import preprocessing
from mashup_app.preprocessing import MODE_SIMPLE, MODE_STEMS, PreparedProject, PreparedSource


class AudioGraphTests(unittest.TestCase):
    @staticmethod
    def _write_stereo_wav(path: Path, frames: int = 128):
        values = (np.full((frames, 2), 0.2, dtype=np.float32) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            output.writeframes(values.tobytes())

    def test_graph_renders_only_the_requested_block_and_keeps_sources_immutable(self):
        original = np.full((12, 2), 0.25, dtype=np.float32)
        source = ArraySourceNode(original)
        graph = AudioGraph(OutputSafetyNode(GainPanNode(source, gain_db=6.020599913)))

        block = graph.render_block(4, 3)

        np.testing.assert_allclose(block, 0.5, atol=1e-5)
        np.testing.assert_allclose(original, 0.25)

    def test_offset_and_mix_nodes_use_timeline_frame_coordinates(self):
        dry = ArraySourceNode(np.full((8, 2), 0.1, dtype=np.float32), "dry")
        late = OffsetNode(ArraySourceNode(np.full((3, 2), 0.25, dtype=np.float32), "late"), 2)
        graph = AudioGraph(MixNode((dry, late)))

        block = graph.render_block(0, 6)

        np.testing.assert_allclose(block[:2], 0.1)
        np.testing.assert_allclose(block[2:5], 0.35)
        np.testing.assert_allclose(block[5], 0.1)

    def test_cached_node_reuses_a_preview_block(self):
        cached = CachedNode(ArraySourceNode(np.ones((16, 2), dtype=np.float32)))

        cached.render(4, 4)
        cached.render(4, 4)

        self.assertEqual(cached.misses, 1)
        self.assertEqual(cached.hits, 1)

    def test_smart_renderer_reuses_an_unchanged_atomic_wav_export(self):
        samples = np.linspace(-0.5, 0.5, 400, dtype=np.float32)
        graph = AudioGraph(ArraySourceNode(np.repeat(samples[:, None], 2, axis=1)))
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "mix.wav"
            renderer = SmartRenderer(Path(directory) / "manifests")

            first = renderer.render(graph, str(output_path))
            second = renderer.render(graph, str(output_path))

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(second.rendered_blocks, 0)
            with wave.open(str(output_path), "rb") as rendered:
                self.assertEqual(rendered.getnframes(), 400)

    def test_stem_factory_compiles_prepared_assets_without_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "stem.wav"
            self._write_stereo_wav(audio_path)
            stems = {name: str(audio_path) for name in ("vocals", "drums", "bass", "other")}
            source = PreparedSource(
                original_path=str(audio_path), source_key="source", audio_path=str(audio_path),
                duration_ms=3, dbfs=-14.0, bpm=120.0, stems=stems,
            )
            project = PreparedProject(1, MODE_STEMS, source, source, str(root / "manifest.json"))
            settings = {
                "tempo_match": False, "match_loudness": False, "auto_fallback": False,
                "timeline_tracks": [{
                    "id": "vocals", "kind": "stem", "stem": "vocals", "bed": "primary",
                    "gain_db": 0.0, "pan": 0.0, "mute": False, "solo": False,
                }],
                "stem_sources": {"vocals": "primary"},
            }

            graph = AudioGraphFactory(root / "cache").build(project, settings)
            rendered = graph.render_block(0, 32)

            self.assertGreater(float(np.abs(rendered).max()), 0.1)
            graph.close()

    def test_preparation_manifest_round_trips_without_work_in_the_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.wav"
            self._write_stereo_wav(source_path)
            with (
                mock.patch.object(preprocessing, "MEDIA_DIR", root / "media"),
                mock.patch.object(preprocessing, "MANIFEST_DIR", root / "projects"),
            ):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    prepared = preprocessing.prepare_project(
                        str(source_path), str(source_path), MODE_SIMPLE
                    )
                loaded = preprocessing.load_prepared_project(
                    str(source_path), str(source_path), MODE_SIMPLE
                )

            self.assertEqual(loaded.fingerprint, prepared.fingerprint)
            self.assertTrue(Path(loaded.primary.audio_path).exists())


if __name__ == "__main__":
    unittest.main()
