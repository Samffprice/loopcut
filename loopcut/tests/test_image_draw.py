"""Image texture ownership, fitting and failure fallback with mocked Blender/GPU APIs."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


class ImageDrawTest(unittest.TestCase):
    def setUp(self):
        self.images = mock.Mock()
        self.images.load.return_value = SimpleNamespace(size=(800, 400), alpha_mode='STRAIGHT')
        self.gpu = mock.Mock()
        self.blit = mock.Mock()
        presets = ModuleType('gpu_extras.presets')
        presets.draw_texture_2d = self.blit
        batch = ModuleType('gpu_extras.batch')
        batch.batch_for_shader = mock.Mock()
        modules = {'bpy': SimpleNamespace(data=SimpleNamespace(images=self.images)), 'blf': mock.Mock(),
                   'gpu': self.gpu, 'gpu_extras': ModuleType('gpu_extras'),
                   'gpu_extras.batch': batch, 'gpu_extras.presets': presets}
        patch = mock.patch.dict(sys.modules, modules)
        patch.start()
        self.addCleanup(patch.stop)
        path = Path(__file__).resolve().parents[2] / 'scripts/addons_core/loopcut/ui/draw.py'
        spec = importlib.util.spec_from_file_location('image_draw_under_test', path)
        self.draw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.draw)
        self.prim = {'t': 'image', 'path': '/stored/a.png', 'x': 10, 'y': 20, 'w': 300, 'h': 220}

    def test_texture_loads_once_and_fits_without_changing_aspect(self):
        for _ in range(3):
            self.draw._draw_image(self.prim, 600)
        self.images.load.assert_called_once_with('/stored/a.png', check_existing=False)
        self.gpu.texture.from_image.assert_called_once()
        self.assertEqual(self.blit.call_args.args[1:], ((10, 430), 300, 150))
        self.images.load.return_value.size = (100, 400)
        self.draw._draw_image(self.prim, 600)
        position, width, height = self.blit.call_args.args[1:]
        self.assertEqual(position, (132.5, 360))
        self.assertAlmostEqual(width, 55)
        self.assertAlmostEqual(height, 220)

    def test_preview_blending_matches_the_loaded_alpha_mode(self):
        for alpha_mode, blend in (('STRAIGHT', 'ALPHA'), ('PREMUL', 'ALPHA_PREMULT')):
            with self.subTest(alpha_mode=alpha_mode):
                self.images.load.return_value.alpha_mode = alpha_mode
                self.draw._draw_image(self.prim, 600)
                self.gpu.state.blend_set.assert_called_with(blend)
                self.assertTrue(self.blit.call_args.kwargs['is_scene_linear_with_rec709_srgb_target'])
        self.images.load.assert_called_once()

    def test_paths_stay_cached_until_conversation_or_panel_releases_them(self):
        self.draw._draw_image(self.prim, 600)
        self.draw.retain_images({'/stored/a.png'})
        self.images.remove.assert_not_called()
        self.draw.retain_images({'/stored/b.png'})
        self.images.remove.assert_called_once_with(self.images.load.return_value)
        self.assertEqual(self.draw._image_cache, {})
        self.draw._draw_image(self.prim, 600)
        self.draw.retain_images(())
        self.assertEqual(self.images.remove.call_count, 2)

    def test_failed_load_is_cached_and_original_output_still_renders(self):
        self.images.load.side_effect = RuntimeError('unreadable')
        shader, batch = mock.Mock(), mock.Mock()
        text = {'t': 'text', 'font': 'ui', 'size': 12, 'color': (1, 1, 1, 1),
                'x': 10, 'y': 260, 'h': 16, 'text': 'Image attached: 800 x 400'}
        with mock.patch.object(self.draw, '_rect_shader', return_value=(shader, batch)), \
                mock.patch.object(self.draw, 'font_id', return_value=0):
            self.gpu.matrix = mock.MagicMock()
            for _ in range(2):
                self.draw.render({'height': 600, 'prims': [self.prim, text]})
        self.images.load.assert_called_once()
        self.draw.blf.draw.assert_called_with(0, text['text'])
        self.blit.assert_not_called()

    def test_gpu_failure_releases_the_owned_image_and_does_not_retry(self):
        self.gpu.texture.from_image.side_effect = RuntimeError('no texture')
        for _ in range(2):
            self.draw._draw_image(self.prim, 600)
        self.images.load.assert_called_once()
        self.images.remove.assert_called_once_with(self.images.load.return_value)
        self.blit.assert_not_called()

    def test_loading_a_blend_can_invalidate_an_image_without_changing_the_conversation(self):
        expired = mock.Mock()
        type(expired).size = mock.PropertyMock(side_effect=ReferenceError('removed image'))
        self.draw._image_cache[self.prim['path']] = (expired, mock.Mock())
        self.draw._draw_image(self.prim, 600)
        self.images.load.assert_called_once_with(self.prim['path'], check_existing=False)
        self.blit.assert_called_once()

    def test_only_the_most_recently_drawn_pictures_stay_loaded_and_they_are_hidden(self):
        loads = []
        self.images.load.side_effect = lambda path, check_existing: loads.append(
            SimpleNamespace(size=(64, 64), alpha_mode='STRAIGHT', path=path)) or loads[-1]
        kept = self.draw.IMAGES_KEPT
        for n in range(kept + 2):
            self.draw._draw_image(dict(self.prim, path=f'/stored/images/{n}.png'), 600)
        self.assertEqual(len([e for e in self.draw._image_cache.values() if e is not None]), kept)
        self.assertNotIn('/stored/images/0.png', self.draw._image_cache)
        self.assertEqual([c.args[0] for c in self.images.remove.call_args_list], loads[:2])
        self.assertTrue(all(image.name.startswith('.') for image in loads))
        self.draw._draw_image(dict(self.prim, path='/stored/images/0.png'), 600)  # Drawn again: reloaded.
        self.assertIn('/stored/images/0.png', self.draw._image_cache)


if __name__ == '__main__':
    unittest.main()
