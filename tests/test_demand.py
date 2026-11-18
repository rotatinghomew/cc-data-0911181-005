from pathlib import Path
import unittest

from app.demand import (DEFAULT_AS_OF, Distribution, build_forecast,
                        explain_releases, reservation_basis, select_method,
                        split_distribution)
from app.storage import DataError, Store

DATA = Path(__file__).parents[1] / 'data'


class DemandForecastTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_plan_is_chosen_by_knowledge_date(self):
        before = build_forecast(self.store, '2026-10-31')
        after = build_forecast(self.store, '2026-11-01')
        self.assertEqual(before['plan']['plan'], 'plan-2027-a')
        self.assertNotIn('C8', [row['course'] for row in before['courses']])
        self.assertEqual(after['plan']['plan'], 'plan-2027-b')
        self.assertIn('C8', [row['course'] for row in after['courses']])

    def test_target_derives_from_known_plan_not_later_data(self):
        bundle = build_forecast(self.store, '2026-10-31')
        self.assertEqual(bundle['target'], '2027-T1')
        self.assertLessEqual(bundle['plan']['known_at'], '2026-10-31')

    def test_new_course_borrows_documented_evidence_only(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        row = next(row for row in bundle['courses'] if row['course'] == 'C8')
        evidence = row['evidence']
        self.assertEqual(evidence['kind'], 'course_relation')
        self.assertFalse(evidence['uses_observations_after_cutoff'])
        borrowed = {item['course']: item for item in evidence['borrowed_from']}
        self.assertEqual(set(borrowed), {'C0', 'C1'})
        for item in borrowed.values():
            self.assertEqual(item['relation'], 'merge-1')
            self.assertEqual(item['known_at'], '2026-11-01')
            self.assertTrue(item['observations'])
            # Every borrowed observation was published by the cutoff.
            self.assertTrue(all(observation['published_at'] <= DEFAULT_AS_OF
                                for observation in item['observations']))

    def test_relation_is_not_known_before_its_known_at(self):
        bundle = build_forecast(self.store, '2026-10-31')
        self.assertEqual(bundle['relation_caliber']['relations'], [])
        # C8 is also absent from the plan known at that date, so it cannot be
        # forecast from evidence that did not exist yet.
        self.assertNotIn('C8', [row['course'] for row in bundle['courses']])

    def test_unrelated_new_course_borrows_same_title_history(self):
        # C8 normally arrives through merge-1.  With a later cutoff and the
        # relation stripped, the fallback must still document its borrowed
        # evidence instead of inventing a point forecast.
        from copy import deepcopy
        store = deepcopy(self.store)
        store._tables['relations'] = []
        bundle = build_forecast(store, '2027-06-01')
        row = next(row for row in bundle['courses'] if row['course'] == 'C8')
        self.assertEqual(row['evidence']['kind'], 'analogous_courses')
        self.assertTrue(row['evidence']['borrowed_from'])
        self.assertFalse(row['evidence']['uses_observations_after_cutoff'])

    def test_capacity_hit_is_a_lower_bound(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        self.assertNotEqual(bundle['method'], 'last_observed')
        for row in bundle['courses']:
            if row['status'] != 'forecast':
                continue
            self.assertGreaterEqual(row['high'], row['point'])
            self.assertGreaterEqual(row['low'], 0)
            # A forecast must not collapse onto the observed seat count just
            # because the class filled: capacity truncated the observation.
            self.assertGreater(row['high'], row['capacity'] - 10)

    def test_censored_classes_raise_the_upper_end(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        row = next(row for row in bundle['courses'] if row['course'] == 'C0'
                   or row['course'] == 'C2')
        evidence = row['evidence']
        # C2 filled in some seasons; those terms are recorded as capacity hits
        # and are not treated as exact demand.
        censored = [observation for observation in evidence['observations']
                    if observation['capacity_hit']]
        self.assertTrue(censored)
        self.assertTrue(all(observation['enrolled'] == observation['seats']
                            for observation in censored))

    def test_levels_are_business_consistent_without_adding_endpoints(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        county = bundle['county']
        course_total = sum(row['expected'] for row in bundle['courses']
                           if row['expected'] is not None)
        campus_total = sum(row['expected'] for row in bundle['campuses'])
        topic_total = sum(row['expected'] for row in bundle['topics'])
        self.assertAlmostEqual(course_total, county['expected'], places=1)
        self.assertAlmostEqual(campus_total, county['expected'], places=1)
        self.assertAlmostEqual(topic_total, county['expected'], places=1)
        self.assertTrue(bundle['consistency']['expected_additive'])
        self.assertTrue(bundle['consistency']['capacity_additive'])
        self.assertFalse(bundle['consistency']['endpoints_additive'])
        # The interval endpoints are quantiles, so they do not add up the way
        # headcounts do.
        endpoint_sum = sum(row['high'] for row in bundle['topics'])
        self.assertNotEqual(endpoint_sum, county['high'])

    def test_shared_course_is_partitioned_not_duplicated(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        campus_capacity = sum(row['capacity'] for row in bundle['campuses'])
        county_capacity = sum(row['capacity'] for row in bundle['courses'])
        self.assertEqual(campus_capacity, county_capacity)
        rows = {row['course']: row for row in bundle['courses']}
        self.assertEqual(rows['C8']['campuses'], ['S0', 'S1'])
        county_expected = bundle['county']['expected']
        self.assertLess(county_expected, sum(row['capacity']
                                             for row in bundle['courses']) + 1)

    def test_risk_is_a_probability_and_matches_headroom(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        for row in [*bundle['courses'], *bundle['campuses'],
                    *bundle['topics'], bundle['county']]:
            if row['risk'] is None:
                continue
            self.assertGreaterEqual(row['risk'], 0.0)
            self.assertLessEqual(row['risk'], 1.0)
            self.assertAlmostEqual(row['headroom'],
                                   row['capacity'] - row['expected'], places=1)

    def test_split_preserves_total_demand(self):
        distribution = Distribution([(10, 0.5), (11, 0.5)])
        parts = split_distribution(distribution, {'S0': 0.47, 'S1': 0.53})
        self.assertAlmostEqual(parts['S0'].mean() + parts['S1'].mean(),
                               distribution.mean(), places=6)
        # No campus receives a fractional seat and no seat is created or
        # lost by the split: only the deterministic rounding remainder moves.
        for part in parts.values():
            self.assertTrue(all(isinstance(value, int) for value, _ in part.pairs))
            self.assertGreaterEqual(part.quantile(0.0), 0)
        self.assertLessEqual(parts['S0'].mean(), 11 * 0.47 + 1)
        self.assertLessEqual(parts['S1'].mean(), 11 * 0.53 + 1)

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(DataError):
            build_forecast(self.store, DEFAULT_AS_OF, method='invented')

    def test_explicit_method_is_scored_too(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF, method='last_observed')
        self.assertEqual(bundle['method'], 'last_observed')
        candidates = bundle['method_selection']['candidates']
        self.assertEqual([row['method'] for row in candidates], ['last_observed'])
        self.assertIsNotNone(candidates[0]['selection_score'])


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_candidate_methods_are_compared_over_consecutive_terms(self):
        selection = select_method(self.store, DEFAULT_AS_OF)
        methods = {row['method']: row for row in selection['candidates']}
        self.assertEqual(set(methods), {'censored_tobit', 'censored_empirical',
                                        'last_observed'})
        for row in methods.values():
            self.assertGreater(row['observations'], 0)
            self.assertGreater(len(row['terms']), 1)
        # The legacy projection treats a full class as exact demand and
        # therefore under-covers the lower bound.
        self.assertLess(methods['last_observed']['coverage'],
                        methods[selection['chosen']]['coverage'])
        self.assertGreater(methods['last_observed']['mean_shortage'], 0)

    def test_each_backtest_prediction_uses_only_earlier_terms(self):
        selection = select_method(self.store, DEFAULT_AS_OF)
        for row in selection['candidates']:
            # A term is predicted from the history published before it, never
            # from its own outcome.  The first term has no earlier evidence
            # and so contributes no backtest observation.
            self.assertNotIn('2024-T1', row['terms'])
            self.assertTrue(all(term < '2026-T1' for term in row['terms']))
        # Each predicted term contributes one observation per course observed.
        expected = (len({row['term'] for row in self.store.table('history')}) - 1) * 8
        self.assertEqual(selection['candidates'][0]['observations'], expected)

    def test_known_cutoff_limits_backtest_terms(self):
        # With only the first term published, no earlier term exists to
        # predict it from.
        selection = select_method(self.store, '2024-01-01')
        for row in selection['candidates']:
            self.assertEqual(row['terms'], [])
            self.assertEqual(row['observations'], 0)


class ReleaseExplanationTests(unittest.TestCase):
    def test_release_uses_plan_at_issue_even_after_newer_plans(self):
        view = explain_releases(Store(DATA), DEFAULT_AS_OF)
        release = view['releases'][0]
        self.assertEqual(release['release'], 'legacy-2026')
        self.assertEqual(release['issued_at'], '2025-12-01')
        self.assertEqual(release['plan']['caliber'], 'release_plan_at_issue')
        self.assertEqual(release['plan']['courses'],
                         ['C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7'])
        # The newer 2027 plans must not be imported into a 2025 release.
        self.assertNotIn('C8', release['plan']['courses'])

    def test_new_course_is_not_validated_with_later_enrollment(self):
        view = explain_releases(Store(DATA), DEFAULT_AS_OF)
        for release in view['releases']:
            self.assertNotIn('C8', [row['course'] for row in release['courses']])


class ReservationBasisTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(DATA)

    def test_basis_matches_forecast_interval(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        basis = reservation_basis(self.store, as_of=DEFAULT_AS_OF)
        forecast_by_campus = {row['campus']: row for row in bundle['campuses']}
        for row in basis['campuses']:
            self.assertEqual(row['suggested_seats'], row['high'])
            self.assertEqual(row['suggested_hours'], row['high'])
            self.assertEqual(row['low'], forecast_by_campus[row['campus']]['low'])
            self.assertEqual(row['high'], forecast_by_campus[row['campus']]['high'])
            self.assertEqual(row['over_capacity_risk'],
                             forecast_by_campus[row['campus']]['risk'])

    def test_hours_shortfall_uses_interval_upper_end(self):
        basis = reservation_basis(self.store, as_of=DEFAULT_AS_OF)
        for row in basis['campuses']:
            self.assertEqual(row['hours_shortfall'],
                             max(0, row['suggested_hours'] - row['teacher_hours']))

    def test_course_filter_rejects_courses_outside_plan(self):
        with self.assertRaises(DataError):
            reservation_basis(self.store, as_of=DEFAULT_AS_OF, courses=['C0'])

    def test_joint_course_appears_once_per_campus_and_county(self):
        bundle = build_forecast(self.store, DEFAULT_AS_OF)
        joint = [row for row in bundle['courses'] if row['course'] == 'C8'][0]
        self.assertEqual(joint['campuses'], ['S0', 'S1'])
        self.assertEqual(len([row for row in bundle['courses']
                              if row['course'] == 'C8']), 1)
        appearances = [row['campus'] for row in bundle['campuses']
                       if 'C8' in [course['course'] for course in row['courses']]]
        self.assertEqual(sorted(appearances), ['S0', 'S1'])


if __name__ == '__main__':
    unittest.main()
