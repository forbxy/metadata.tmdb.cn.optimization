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

    def test_strip_4ksj_net_marker(self):
        cleaned = optimize_post_kodi_title("La.Grande,Guerra,1959 4KSJ.net")
        self.assertEqual("La.Grande,Guerra,1959", cleaned)

    def test_strip_leading_bracketed_release_prefix(self):
        cleaned = optimize_post_kodi_title("【蓝光DIY中字 恐怖】致命录像带,VHS,2012")
        self.assertEqual("致命录像带,VHS,2012", cleaned)

    def test_keep_real_bracketed_title(self):
        cleaned = optimize_post_kodi_title("[恐怖游轮]")
        self.assertEqual("恐怖游轮", cleaned)

    def test_candidates_strip_4ksj_net_marker(self):
        candidates = build_search_title_candidates("La.Grande,Guerra,1959 4ksj net")
        self.assertNotIn("La.Grande,Guerra,1959 4ksj net", candidates)
        self.assertIn("La.Grande,Guerra,1959", candidates)

    def test_candidates_strip_release_prefix_before_search(self):
        candidates = build_search_title_candidates("【蓝光DIY中字 恐怖】致命录像带,VHS,2012")
        self.assertEqual("致命录像带,VHS,2012", candidates[0])
        self.assertIn("致命录像带", candidates)
        self.assertTrue(all("蓝光DIY中字" not in candidate for candidate in candidates))

    def test_strip_trailing_guoyue_markers(self):
        cleaned = optimize_post_kodi_title("1999 古惑仔激情篇之洪兴大飞哥 国粤双语 中英字幕")
        self.assertEqual("1999 古惑仔激情篇之洪兴大飞哥", cleaned)

    def test_add_colon_candidate_for_zhi_connector(self):
        candidates = build_search_title_candidates("1999 古惑仔激情篇之洪兴大飞哥 国粤双语 中英字幕")
        self.assertIn("古惑仔激情篇：洪兴大飞哥", candidates)

    def test_keep_numeric_prefix_when_replacing_zhi_connector(self):
        candidates = build_search_title_candidates("007之雷霆谷")
        self.assertIn("007：雷霆谷", candidates)
        self.assertNotIn("：雷霆谷", candidates)
        self.assertNotIn("之雷霆谷", candidates)

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

    def test_mixed_sequel_candidates_prioritize_latin_and_keep_number(self):
        candidates = build_search_title_candidates("新扎师妹2 Love Undercover 2")
        self.assertIn("Love Undercover 2", candidates)
        self.assertIn("新扎师妹2", candidates)
        self.assertNotIn("新扎师妹", candidates)
        self.assertLess(candidates.index("Love Undercover 2"), candidates.index("新扎师妹2"))
        self.assertNotIn("新扎师妹22", candidates)

    def test_keep_cjk_attached_number_for_sequel(self):
        candidates = build_search_title_candidates("赌博默示录2 Gambling Apocalypse Kaiji")
        self.assertIn("Gambling Apocalypse Kaiji 2", candidates)
        self.assertIn("赌博默示录2", candidates)
        self.assertNotIn("赌博默示录", candidates)

    def test_add_candidate_without_theatrical_edition_keyword(self):
        candidates = build_search_title_candidates("哆啦A梦剧场版：大雄与绿巨人传")
        self.assertIn("哆啦A梦：大雄与绿巨人传", candidates)

    def test_add_candidate_without_live_action_keyword(self):
        candidates = build_search_title_candidates("进击的巨人真人版")
        self.assertIn("进击的巨人", candidates)

    def test_drop_single_non_cjk_title(self):
        candidates = build_search_title_candidates("[X")
        self.assertEqual([], candidates)

    def test_add_candidate_without_leading_sequence_prefix(self):
        candidates = build_search_title_candidates("09. Paperman")
        self.assertIn("Paperman", candidates)

    def test_keep_real_numeric_title_without_separator(self):
        candidates = build_search_title_candidates("12 Angry Men")
        self.assertIn("12 Angry Men", candidates)
        self.assertNotIn("Angry Men", candidates)

    def test_add_candidate_without_trailing_year_suffix(self):
        candidates = build_search_title_candidates("La Grande,Guerra,1959")
        self.assertIn("La Grande,Guerra", candidates)
        self.assertIn("La Grande Guerra", candidates)

    def test_keep_standalone_year_title(self):
        candidates = build_search_title_candidates("1917")
        self.assertEqual(["1917"], candidates)

    def test_add_candidate_without_attached_trailing_year(self):
        candidates = build_search_title_candidates("虎度门1996 国粤音軌 簡繁英字幕")
        self.assertIn("虎度门1996", candidates)
        self.assertIn("虎度门", candidates)
        self.assertNotIn("虎度门1996 国粤音軌 簡繁英", candidates)


if __name__ == '__main__':
    unittest.main()
