import unittest

from python.title_search_optimizer import build_search_title_candidates, optimize_post_kodi_title


class TestTitleSearchOptimizer(unittest.TestCase):
    def test_preserve_original_as_first_candidate(self):
        candidates = build_search_title_candidates("The Matrix")
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual("The Matrix", candidates[0])

    def test_strip_common_prefix_suffix_noise(self):
        cleaned = optimize_post_kodi_title("国际通用版 ANDREI RUBLEV DISC 2 KOREAN")
        self.assertEqual("ANDREI RUBLEV", cleaned)

    def test_strip_unmatched_bracket_prefix(self):
        cleaned = optimize_post_kodi_title("[Pale Flower")
        self.assertEqual("Pale Flower", cleaned)

    def test_build_bilingual_candidates(self):
        candidates = build_search_title_candidates("卡蜜儿·克劳岱尔 Camille Claudel")
        self.assertIn("卡蜜儿克劳岱尔", candidates)
        self.assertIn("Camille Claudel", candidates)

    def test_limit_candidate_count(self):
        candidates = build_search_title_candidates("国际通用版 卡蜜儿·克劳岱尔 Camille Claudel DISC 2", max_candidates=3)
        self.assertLessEqual(len(candidates), 3)

    def test_strip_bracket_fragment_but_keep_cleaned_title(self):
        candidates = build_search_title_candidates("[长大")
        self.assertNotIn("[长大", candidates)
        self.assertIn("长大", candidates)

    def test_keep_single_cjk_title(self):
        candidates = build_search_title_candidates("[夜")
        self.assertEqual(["夜"], candidates)

    def test_keep_single_cjk_from_mixed_title(self):
        candidates = build_search_title_candidates("信 The Letter")
        self.assertIn("信", candidates)
        self.assertIn("The Letter", candidates)

    def test_add_candidate_without_theatrical_edition_keyword(self):
        candidates = build_search_title_candidates("哆啦A梦剧场版：大雄与绿巨人传")
        self.assertIn("哆啦A梦：大雄与绿巨人传", candidates)

    def test_add_candidate_without_live_action_keyword(self):
        candidates = build_search_title_candidates("进击的巨人真人版")
        self.assertIn("进击的巨人", candidates)

    def test_drop_single_non_cjk_title(self):
        candidates = build_search_title_candidates("[X")
        self.assertEqual([], candidates)


if __name__ == '__main__':
    unittest.main()
