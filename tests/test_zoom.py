import unittest

from app.core.zoom import ZoomState, fit_rect, MAX_ZOOM


class FitRectTests(unittest.TestCase):
    def test_wide_picture_in_square_is_letterboxed(self):
        ox, oy, w, h = fit_rect(1600, 900, 800, 800)
        self.assertEqual((ox, w, h), (0, 800, 450))
        self.assertEqual(oy, 175)

    def test_degenerate_sizes(self):
        self.assertEqual(fit_rect(0, 10, 100, 100), (0.0, 0.0, 0.0, 0.0))


class ZoomTests(unittest.TestCase):
    def test_starts_inactive_full_frame(self):
        z = ZoomState()
        self.assertFalse(z.active)
        self.assertEqual(z.crop_rect(640, 360), (0, 0, 640, 360))

    def test_zoom_in_centre_shows_middle(self):
        z = ZoomState()
        z.zoom_at(2.0)
        self.assertEqual(z.crop_rect(640, 360), (160, 90, 320, 180))

    def test_point_under_cursor_stays_put(self):
        z = ZoomState()
        u, v = 0.25, 0.75
        before = (z.x + u * z.size, z.y + v * z.size)
        z.zoom_at(4.0, u, v)
        after = (z.x + u * z.size, z.y + v * z.size)
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])

    def test_zoom_is_capped(self):
        z = ZoomState()
        z.zoom_at(100.0)
        self.assertEqual(z.zoom, MAX_ZOOM)

    def test_zoom_out_to_one_resets(self):
        z = ZoomState()
        z.zoom_at(3.0, 0.9, 0.9)
        z.zoom_at(1.0)
        self.assertFalse(z.active)
        self.assertEqual((z.x, z.y), (0.0, 0.0))

    def test_wheel_in_then_out_returns_to_full(self):
        z = ZoomState()
        for _ in range(4):
            z.wheel(1)
        for _ in range(10):
            z.wheel(-1)
        self.assertFalse(z.active)

    def test_pan_stays_inside_frame(self):
        z = ZoomState()
        z.zoom_at(2.0)
        z.pan(-50, -50)                 # drag far one way
        self.assertLessEqual(z.x + z.size, 1.0 + 1e-9)
        self.assertLessEqual(z.y + z.size, 1.0 + 1e-9)
        z.pan(50, 50)                   # and far the other way
        self.assertEqual((z.x, z.y), (0.0, 0.0))

    def test_pan_direction_follows_the_drag(self):
        z = ZoomState()
        z.zoom_at(2.0)
        x0 = z.x
        z.pan(0.2, 0)                   # drag picture to the right -> view moves left
        self.assertLess(z.x, x0)

    def test_pan_ignored_when_not_zoomed(self):
        z = ZoomState()
        z.pan(0.5, 0.5)
        self.assertEqual((z.x, z.y), (0.0, 0.0))

    def test_crop_rect_always_inside_image(self):
        z = ZoomState()
        for zoom in (1.3, 2.0, 3.7, 8.0):
            for u, v in ((0, 0), (1, 1), (0.5, 0.2)):
                z.reset()
                z.zoom_at(zoom, u, v)
                x, y, w, h = z.crop_rect(704, 576)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, 704)
                self.assertLessEqual(y + h, 576)


if __name__ == "__main__":
    unittest.main()
