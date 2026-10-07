import unittest

from scripts.execute_classification import public_progress


class ClassificationCliProgressTests(unittest.TestCase):
    jobs = [{'name': '场景 · 学习专注'}, {'name': '风格 · 流行抒情'}]

    def test_preflight_uses_verified_playlist_index_not_completed_count(self):
        event = {'stage': 'classification_preflight', 'step': 'playlist_verified',
                 'phase': 'reading', 'job_index': 0, 'completed_count': 1, 'total_count': 2}
        view = public_progress(event, self.jobs)
        self.assertEqual(view['name'], self.jobs[0]['name'])
        self.assertEqual(view['stage'], 'classification_preflight')

    def test_source_and_finished_preflight_have_no_fabricated_current_playlist(self):
        for step in ('source_start', 'finished'):
            view = public_progress({'stage': 'classification_preflight', 'step': step,
                                    'phase': 'preflight', 'completed_count': 1}, self.jobs)
            self.assertNotIn('name', view)

    def test_existing_write_phase_uses_original_completed_count(self):
        view = public_progress({'phase': 'completed', 'completed_count': 1, 'raw': 'SECRET'}, self.jobs)
        self.assertEqual(view['name'], self.jobs[0]['name'])
        self.assertNotIn('SECRET', str(view))

    def test_cli_events_and_invalid_preflight_index_do_not_invent_names(self):
        self.assertIsNone(public_progress({'kind': 'cli'}, self.jobs))
        for index in (True, -1, 2, '0'):
            self.assertNotIn('name', public_progress({'stage': 'classification_preflight',
                             'step': 'playlist_start', 'job_index': index}, self.jobs))
