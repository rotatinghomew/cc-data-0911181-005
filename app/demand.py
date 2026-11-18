"""Point-in-time enrollment demand forecasting for staffing reservations.

Two ideas are kept strictly apart:

* ``enrolled`` is an observed lower bound on demand.  A class whose enrollment
  reached its seat count was censored by capacity; it does not prove that
  demand was exactly the number of seats.
* a forecast is a distribution.  Every business level (course, campus, topic,
  county) receives quantiles read from its own aggregate distribution.
  Quantiles are not added across levels; distributions are aggregated and
  then read.

Only material known at ``as_of`` is used: history published by the cutoff,
plans whose ``known_at`` is not later than the cutoff, and course relations
whose ``known_at`` is not later than the cutoff.  A new course may borrow
observed history from its parents, but never from its own later enrollment.
"""

from datetime import date, timedelta
from math import ceil, erf, exp, floor, log, pi, sqrt

from .storage import DataError

INTERVAL_LEVEL = 0.80
INTERVAL_LOW_Q = round((1.0 - INTERVAL_LEVEL) / 2.0, 10)
INTERVAL_HIGH_Q = round(1.0 - INTERVAL_LOW_Q, 10)
METHODS = ('censored_tobit', 'censored_empirical', 'last_observed')
DEFAULT_METHOD = 'censored_tobit'
DEFAULT_AS_OF = '2026-11-01'
_EM_ITERATIONS = 200


# ---------------------------------------------------------------------------
# Distributions over integer seat counts


class Distribution:
    """A finite discrete distribution over possible demand in seats."""

    def __init__(self, pairs):
        combined = {}
        for value, probability in pairs:
            try:
                probability = float(probability)
                value = int(round(float(value)))
            except (TypeError, ValueError):
                raise DataError('invalid demand distribution') from None
            if probability <= 0:
                continue
            combined[value] = combined.get(value, 0.0) + probability
        total = sum(combined.values())
        if total <= 0:
            raise DataError('empty demand distribution')
        self.pairs = tuple(sorted((value, probability / total)
                                  for value, probability in combined.items()))
        self._mean = None
        self._variance = None

    @classmethod
    def certain(cls, value):
        return cls([(value, 1.0)])

    def mean(self):
        if self._mean is None:
            self._mean = sum(value * probability for value, probability in self.pairs)
        return self._mean

    def variance(self):
        if self._variance is None:
            mean = self.mean()
            self._variance = sum(probability * (value - mean) ** 2
                                 for value, probability in self.pairs)
        return self._variance

    def quantile(self, q):
        q = min(max(float(q), 0.0), 1.0)
        cumulative = 0.0
        for value, probability in self.pairs:
            cumulative += probability
            if cumulative + 1e-12 >= q:
                return value
        return self.pairs[-1][0]

    def probability_above(self, threshold):
        return sum(probability for value, probability in self.pairs
                   if value > threshold + 1e-12)

    def convolve(self, other):
        combined = {}
        for left_value, left_probability in self.pairs:
            for right_value, right_probability in other.pairs:
                value = left_value + right_value
                combined[value] = (combined.get(value, 0.0)
                                   + left_probability * right_probability)
        return Distribution(combined.items())

    def __repr__(self):
        return 'Distribution(mean=%.2f, low=%d, high=%d)' % (
            self.mean(), self.quantile(INTERVAL_LOW_Q),
            self.quantile(INTERVAL_HIGH_Q))


def convolve_many(distributions):
    total = Distribution.certain(0)
    for distribution in distributions:
        total = total.convolve(distribution)
    return total


def split_distribution(distribution, shares):
    """Split a course distribution across campuses without duplication.

    For every possible demand value the campus amounts add back to exactly
    that value, so campus expectations add up to the county expectation and a
    shared course is never counted twice.  Quantiles of the campus marginals
    are not additive, which is expected.
    """
    names = list(shares)
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: distribution}
    parts = {name: [] for name in names}
    for value, probability in distribution.pairs:
        base = []
        remainders = []
        assigned = 0
        for index, name in enumerate(names):
            exact = value * shares[name]
            amount = int(floor(exact))
            base.append(amount)
            assigned += amount
            remainders.append((exact - amount, index))
        leftover = max(0, value - assigned)
        # Hand out the remaining seats by largest remainder, deterministically
        # (index breaks ties).  Never give a campus more than the total.
        remainders.sort(key=lambda item: (-item[0], item[1]))
        for _, index in remainders:
            if leftover <= 0:
                break
            base[index] += 1
            leftover -= 1
        while leftover > 0:
            for index in range(len(names)):
                if leftover <= 0:
                    break
                base[index] += 1
                leftover -= 1
        for index, name in enumerate(names):
            parts[name].append((base[index], probability))
    return {name: Distribution(pairs) for name, pairs in parts.items()}


def aggregate(parts):
    return convolve_many([part['distribution'] for part in parts])


# ---------------------------------------------------------------------------
# Censored normal model


def _normal_pdf(z):
    return exp(-0.5 * z * z) / sqrt(2.0 * pi)


def _normal_cdf(z):
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def _normal_quantile(q):
    """Acklam's rational approximation for the normal quantile."""
    if q <= 0.0:
        return float('-inf')
    if q >= 1.0:
        return float('inf')
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    low, high = 0.02425, 1.0 - 0.02425
    if q < low:
        t = sqrt(-2.0 * log(q))
        return (((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / \
               ((((d[0] * t + d[1]) * t + d[2]) * t + d[3]) * t + 1.0)
    if q > high:
        t = sqrt(-2.0 * log(1.0 - q))
        return -(((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / \
                ((((d[0] * t + d[1]) * t + d[2]) * t + d[3]) * t + 1.0)
    t = q - 0.5
    r = t * t
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * t / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def _tobit_em(observations):
    """Fit a censored normal demand model with EM.

    ``observations`` holds ``(enrolled, seats, censored)`` tuples.  For a
    censored observation the likelihood only says latent demand was at least
    the seat count, so the E step uses the conditional mean of the truncated
    normal instead of the observed enrollment.
    """
    if not observations:
        return None, None
    observed = [float(row[0]) for row in observations if not row[2]]
    if observed:
        mean = sum(observed) / len(observed)
        variance = sum((value - mean) ** 2 for value in observed) / len(observed)
        sigma = sqrt(variance) if variance > 0 else 1.0
    else:
        mean = sum(float(row[1]) for row in observations) / len(observations)
        sigma = 1.0
    sigma = max(sigma, 0.5)

    for _ in range(_EM_ITERATIONS):
        first = 0.0
        second = 0.0
        for value, seats, censored in observations:
            value = float(value)
            seats = float(seats)
            if not censored:
                first += value
                second += value * value
                continue
            z = (seats - mean) / sigma
            tail = 1.0 - _normal_cdf(z)
            if tail <= 1e-12:
                conditional = seats + sigma * max(z, 1.0)
                conditional_variance = sigma * sigma
            else:
                hazard = _normal_pdf(z) / tail
                conditional = mean + sigma * hazard
                conditional_variance = sigma * sigma * (1.0 - z * hazard + hazard * hazard)
                conditional_variance = max(conditional_variance, 1e-9)
            first += conditional
            second += conditional_variance + conditional * conditional
        count = len(observations)
        new_mean = first / count
        new_variance = max(second / count - new_mean * new_mean, 0.25)
        new_sigma = sqrt(new_variance)
        converged = abs(new_mean - mean) < 1e-9 and abs(new_sigma - sigma) < 1e-9
        mean, sigma = new_mean, new_sigma
        if converged:
            break
    return mean, min(max(sigma, 0.5), 30.0)


def _normal_distribution(mean, sigma, capacity):
    sigma = max(float(sigma), 0.5)
    upper = max(int(ceil(mean + 4.0 * sigma)), int(capacity) + 5, 1)
    pairs = []
    previous = 0.0
    for value in range(0, upper + 1):
        cumulative = _normal_cdf((value + 0.5 - mean) / sigma)
        probability = max(cumulative - previous, 0.0)
        previous = cumulative
        pairs.append((value, probability))
    tail = max(0.0, 1.0 - previous)
    if tail > 0:
        pairs.append((upper + 1, tail))
    return Distribution(pairs)


# ---------------------------------------------------------------------------
# Point-in-time inputs


def _parse_date(value, field):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise DataError(field + ' must be an ISO date') from None


def _term_key(term):
    try:
        year, season = str(term).split('-T', 1)
        return int(year), int(season)
    except (TypeError, ValueError):
        return (0, 0)


def _history_rows(store, course, cutoff):
    rows = [dict(row) for row in store.select('history', course=course)
            if row['published_at'] <= cutoff]
    rows.sort(key=lambda row: (_term_key(row['term']), row['published_at']))
    for row in rows:
        row['censored'] = row['enrolled'] >= row['seats']
        row['capacity_hit'] = row['censored']
    return rows


def _observations(rows):
    observations = []
    for row in rows:
        if row['censored']:
            # A full class proves demand reached at least the seat count.
            # A recorded waitlist, when present, raises that lower bound;
            # a missing waitlist is unknown, not zero.
            floor = row['seats']
            if row.get('waitlist') is not None:
                floor = max(floor, row['enrolled'] + row['waitlist'])
            observations.append((floor, floor, True))
        else:
            observations.append((row['enrolled'], row['seats'], False))
    return observations


def _slack(rows):
    """Typical distance below capacity when a class did not fill."""
    values = [row['seats'] - row['enrolled'] for row in rows if not row['censored']]
    if not values:
        return 1
    values.sort()
    middle = len(values) // 2
    if len(values) % 2:
        return max(1, values[middle])
    return max(1.0, (values[middle - 1] + values[middle]) / 2.0)


def _recency_weight(term, target):
    """Nearer terms inform the target more than distant ones."""
    try:
        term_year = _term_key(term)[0]
        target_year = _term_key(target)[0]
    except (TypeError, ValueError):
        return 1.0
    return 1.0 / (1.0 + max(0, target_year - term_year))


def _course_distribution(rows, method, target):
    if not rows:
        return None
    if method == 'last_observed':
        latest = max(rows, key=lambda row: (_term_key(row['term']),
                                           row['published_at']))
        return Distribution.certain(latest['enrolled'])
    if method == 'censored_empirical':
        slack = _slack(rows)
        values = []
        for row in rows:
            weight = _recency_weight(row['term'], target)
            if row['censored']:
                floor = row['seats']
                if row.get('waitlist') is not None:
                    floor = max(floor, row['enrolled'] + row['waitlist'])
                values.append((int(round(floor + slack)), weight))
            else:
                values.append((int(row['enrolled']), weight))
        return Distribution(values)
    mean, sigma = _tobit_em(_observations(rows))
    if mean is None:
        return None
    capacity = max(row['seats'] for row in rows)
    return _normal_distribution(mean, sigma, capacity)


# ---------------------------------------------------------------------------
# Plans and relations


def _plans_known(store, cutoff, target=None):
    return [plan for plan in store.table('plans')
            if plan['known_at'] <= cutoff
            and (target is None or plan['target'] == target)]


def _select_plan(store, cutoff, target=None):
    plans = _plans_known(store, cutoff, target)
    if not plans:
        return None
    plans.sort(key=lambda plan: (plan['known_at'], plan['plan']))
    return plans[-1]


def _default_target(store, cutoff):
    plans = _plans_known(store, cutoff)
    if plans:
        plans.sort(key=lambda plan: (_term_key(plan['target']),
                                     plan['known_at'], plan['plan']))
        return plans[-1]['target']
    rows = [row for row in store.table('history') if row['published_at'] <= cutoff]
    if not rows:
        return None
    latest = max(rows, key=lambda row: (_term_key(row['term']), row['published_at']))
    year, season = _term_key(latest['term'])
    if season >= 4:
        return str(year + 1) + '-T1'
    return str(year) + '-T' + str(season + 1)


def _relations_known(store, cutoff, target):
    rows = [dict(row) for row in store.table('relations')
            if row['known_at'] <= cutoff and row['effective'] <= target]
    rows.sort(key=lambda row: (row['effective'], row['known_at'], row['relation']))
    return rows


def _relation_for(relations, course):
    candidates = [row for row in relations if row['child'] == course]
    return candidates[-1] if candidates else None


def _catalog(store, course):
    return store.find('courses', course=course)


def _evidence_rows(rows):
    return [dict(course=row['course'], term=row['term'],
                 published_at=row['published_at'], seats=row['seats'],
                 enrolled=row['enrolled'], waitlist=row.get('waitlist'),
                 capacity_hit=bool(row['censored']))
            for row in rows]


# ---------------------------------------------------------------------------
# Course-level forecast


def _forecast_course(store, course, cutoff, target, method, relations,
                     cache, stack=()):
    if course in cache:
        return cache[course]
    if course in stack:
        raise DataError('cyclic course relation: ' + course)
    catalog = _catalog(store, course)
    rows = _history_rows(store, course, cutoff)
    if rows:
        distribution = _course_distribution(rows, method, target)
        if distribution is not None:
            result = dict(
                course=course,
                distribution=distribution,
                evidence=dict(
                    kind='observed_history',
                    method=method,
                    plan_target=target,
                    observations=_evidence_rows(rows),
                    censored_terms=[row['term'] for row in rows
                                    if row['censored']],
                    borrowed_from=[],
                    uses_observations_after_cutoff=False,
                ),
            )
            cache[course] = result
            return result

    relation = _relation_for(relations, course)
    borrowed = []
    distributions = []
    if relation is not None:
        for parent in relation['parents']:
            try:
                parent_result = _forecast_course(store, parent, cutoff, target,
                                                 method, relations, cache,
                                                 stack + (course,))
            except DataError:
                parent_result = None
            if parent_result is None:
                continue
            distributions.append(parent_result['distribution'])
            parent_rows = _history_rows(store, parent, cutoff)
            borrowed.append(dict(
                course=parent,
                relation=relation['relation'],
                meaning=relation['meaning'],
                known_at=relation['known_at'],
                effective=relation['effective'],
                observations=_evidence_rows(parent_rows),
                capacity_hit_terms=[row['term'] for row in parent_rows
                                    if row['censored']],
                same_title=_catalog(store, parent)['title'] == catalog['title'],
            ))
        kind = 'course_relation'
        relation_ids = [relation['relation']]
    else:
        # A new course without an explicit relation may borrow from courses
        # with the same title, then from the same topic.  This remains
        # point-in-time evidence and never includes the new course's own
        # later enrollments.
        candidates = []
        for other in store.table('courses'):
            if other['course'] == course:
                continue
            other_rows = _history_rows(store, other['course'], cutoff)
            if not other_rows:
                continue
            if other['title'] == catalog['title']:
                candidates.append((0, other))
            elif other['topic'] == catalog['topic']:
                candidates.append((1, other))
        candidates.sort(key=lambda item: (item[0], item[1]['course']))
        seen = set()
        same_topic_used = 0
        for rank, other in candidates:
            if other['course'] in seen:
                continue
            if rank == 1:
                if same_topic_used >= 3:
                    continue
                same_topic_used += 1
            seen.add(other['course'])
            try:
                other_result = _forecast_course(store, other['course'], cutoff,
                                                target, method, relations, cache,
                                                stack + (course,))
            except DataError:
                continue
            distributions.append(other_result['distribution'])
            other_rows = _history_rows(store, other['course'], cutoff)
            borrowed.append(dict(
                course=other['course'],
                relation='same_title' if rank == 0 else 'same_topic',
                meaning='analogous_offering',
                known_at=None,
                effective=None,
                observations=_evidence_rows(other_rows),
                capacity_hit_terms=[row['term'] for row in other_rows
                                    if row['censored']],
                same_title=other['title'] == catalog['title'],
            ))
        kind = 'analogous_courses'
        relation_ids = []

    if not distributions:
        result = dict(
            course=course,
            distribution=None,
            evidence=dict(kind='insufficient_evidence', method=method,
                          plan_target=target, observations=[],
                          borrowed_from=[], censored_terms=[],
                          relation_ids=[],
                          uses_observations_after_cutoff=False),
        )
        cache[course] = result
        return result

    distribution = convolve_many(distributions)
    result = dict(
        course=course,
        distribution=distribution,
        evidence=dict(
            kind=kind,
            method=method,
            plan_target=target,
            observations=[],
            borrowed_from=borrowed,
            censored_terms=[],
            relation_ids=relation_ids,
            uses_observations_after_cutoff=False,
            own_observations_available=False,
        ),
    )
    cache[course] = result
    return result


# ---------------------------------------------------------------------------
# Aggregation helpers


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value) + 1e-9, digits)


def _interval(distribution):
    return int(distribution.quantile(INTERVAL_LOW_Q)), \
           int(distribution.quantile(INTERVAL_HIGH_Q))


def _campus_shares(store, course, relations, cache):
    catalog = _catalog(store, course)
    campuses = list(catalog['campuses'])
    if not campuses:
        return {}
    if len(campuses) == 1:
        return {campuses[0]: 1.0}
    relation = _relation_for(relations, course)
    if relation is not None:
        weights = {campus: 0.0 for campus in campuses}
        for parent in relation['parents']:
            parent_catalog = _catalog(store, parent)
            parent_result = cache.get(parent)
            if parent_result is None or parent_result['distribution'] is None:
                continue
            weight = max(parent_result['distribution'].mean(), 1.0)
            parent_campuses = list(parent_catalog['campuses'])
            if not parent_campuses:
                continue
            for campus in parent_campuses:
                if campus in weights:
                    weights[campus] += weight / len(parent_campuses)
        total = sum(weights.values())
        if total > 0:
            return {campus: weights[campus] / total for campus in campuses}
    share = 1.0 / len(campuses)
    return {campus: share for campus in campuses}


def _capacity_split(capacity, campuses, shares):
    if len(campuses) == 1:
        return {campuses[0]: int(capacity)}
    assigned = {campus: int(floor(capacity * shares[campus]))
                for campus in campuses}
    leftover = int(capacity) - sum(assigned.values())
    order = sorted(campuses,
                   key=lambda campus: (-(capacity * shares[campus]
                                         - floor(capacity * shares[campus])),
                                       campus))
    for campus in order:
        if leftover <= 0:
            break
        assigned[campus] += 1
        leftover -= 1
    while leftover > 0:
        for campus in campuses:
            if leftover <= 0:
                break
            assigned[campus] += 1
            leftover -= 1
    return assigned


def _serialize_interval(distribution, capacity):
    low, high = _interval(distribution)
    expected = distribution.mean()
    risk = distribution.probability_above(capacity)
    return dict(
        point=int(round(expected)),
        expected=_round(expected),
        low=low,
        high=high,
        interval=dict(low=low, high=high, level=INTERVAL_LEVEL,
                      low_quantile=INTERVAL_LOW_Q,
                      high_quantile=INTERVAL_HIGH_Q),
        capacity=capacity,
        over_capacity_risk=_round(risk),
        risk=_round(risk),
        headroom=_round(capacity - expected),
    )


def _consistency(course_items, campus_rows, topic_rows, county):
    campus_expected = sum(row['expected'] for row in campus_rows)
    topic_expected = sum(row['expected'] for row in topic_rows)
    campus_capacity = sum(row['capacity'] for row in campus_rows)
    topic_capacity = sum(row['capacity'] for row in topic_rows)
    county_expected = county['expected']
    def close(left, right):
        return (left is not None and right is not None
                and abs(float(left) - float(right)) <= 0.05)
    return dict(
        course_expected_total=_round(sum(item['row']['expected']
                                         for item in course_items
                                         if item['row']['expected'] is not None)),
        campus_expected_total=_round(campus_expected),
        topic_expected_total=_round(topic_expected),
        county_expected=county_expected,
        campus_capacity_total=_round(campus_capacity),
        topic_capacity_total=_round(topic_capacity),
        county_capacity=county['capacity'],
        expected_additive=bool(close(campus_expected, county_expected)
                               and close(topic_expected, county_expected)),
        capacity_additive=bool(close(campus_capacity, county['capacity'])
                               and close(topic_capacity, county['capacity'])),
        endpoints_additive=False,
        endpoint_note='interval endpoints are quantiles of each aggregate '
                      'distribution; they must not be summed across levels',
    )


# ---------------------------------------------------------------------------
# Public forecast


def build_forecast(store, as_of=DEFAULT_AS_OF, target=None, method=None):
    cutoff = str(as_of)
    _parse_date(cutoff, 'as_of')
    target = target or _default_target(store, cutoff)
    if target is None:
        raise DataError('no target term available')
    selection = select_method(store, cutoff, target, method)
    chosen = selection['chosen']
    relations = _relations_known(store, cutoff, target)
    plan = _select_plan(store, cutoff, target)
    if plan is not None:
        course_names = list(plan['courses'])
        plan_source = dict(plan=plan['plan'], known_at=plan['known_at'],
                           target=plan['target'], courses=list(plan['courses']))
    else:
        course_names = [row['course'] for row in store.table('courses')
                        if row['introduced'] <= target]
        plan_source = dict(plan=None, known_at=None, target=target,
                           courses=course_names)
    course_names = list(dict.fromkeys(course_names))
    known_courses = {row['course'] for row in store.table('courses')}
    excluded = [name for name in course_names if name not in known_courses]
    course_names = [name for name in course_names if name in known_courses]

    cache = {}
    course_items = []
    excluded = []
    for name in course_names:
        if not store.select('courses', course=name):
            continue
        result = _forecast_course(store, name, cutoff, target, chosen,
                                  relations, cache)
        catalog = _catalog(store, name)
        distribution = result['distribution']
        row = dict(course=name, title=catalog['title'], topic=catalog['topic'],
                   campuses=list(catalog['campuses']),
                   capacity=catalog['capacity'], plan=plan_source,
                   evidence=result['evidence'])
        if distribution is None:
            row.update(status='insufficient_evidence', point=None, expected=None,
                       low=None, high=None, interval=None,
                       over_capacity_risk=None, risk=None, headroom=None)
        else:
            row.update(status='forecast',
                       **_serialize_interval(distribution, catalog['capacity']))
        course_items.append(dict(row=row, distribution=distribution,
                                 catalog=catalog))

    # Campus level: every course is split across its campuses with shares that
    # sum to one.  A shared course is counted once at county and topic level
    # and partitioned (not duplicated) across campuses.
    campus_parts = {}
    for item in course_items:
        name = item['row']['course']
        campuses = list(item['catalog']['campuses'])
        shares = _campus_shares(store, name, relations, cache)
        distribution = item['distribution']
        if not campuses:
            continue
        if distribution is None:
            split = {campus: None for campus in campuses}
        else:
            split = split_distribution(distribution, shares)
        capacities = _capacity_split(item['catalog']['capacity'], campuses, shares)
        for campus in campuses:
            campus_parts.setdefault(campus, []).append(dict(
                course=name,
                share=shares[campus],
                distribution=split[campus],
                capacity=capacities[campus],
            ))

    hours = {row['campus']: row['teacher_hours']
             for row in store.table('reservations') if row['term'] == target}
    campus_rows = []
    for campus in sorted(campus_parts):
        parts = campus_parts[campus]
        usable = [part for part in parts if part['distribution'] is not None]
        if not usable:
            continue
        distribution = aggregate(usable)
        capacity = sum(part['capacity'] for part in usable)
        row = dict(campus=campus, **_serialize_interval(distribution, capacity))
        teacher_hours = hours.get(campus)
        row.update(
            teacher_hours=teacher_hours,
            required_hours=int(ceil(row['high'])) if row['high'] is not None else None,
            hours_shortfall=(max(0, int(ceil(row['high'])) - int(teacher_hours))
                             if teacher_hours is not None else None),
            courses=[dict(course=part['course'], share=_round(part['share']),
                          expected=_round(part['distribution'].mean()))
                     for part in parts],
        )
        campus_rows.append(row)

    # Topic level: each course is counted exactly once.
    topic_parts = {}
    for item in course_items:
        if item['distribution'] is None:
            continue
        topic_parts.setdefault(item['row']['topic'], []).append(item)
    topic_rows = []
    for topic in sorted(topic_parts):
        items = topic_parts[topic]
        distribution = convolve_many([item['distribution'] for item in items])
        capacity = sum(item['catalog']['capacity'] for item in items)
        row = dict(topic=topic, **_serialize_interval(distribution, capacity))
        row['courses'] = [item['row']['course'] for item in items]
        topic_rows.append(row)

    # County level: all planned courses, each once.
    known_items = [item for item in course_items if item['distribution'] is not None]
    if known_items:
        county_distribution = convolve_many([item['distribution'] for item in known_items])
        county_capacity = sum(item['catalog']['capacity'] for item in known_items)
        county = _serialize_interval(county_distribution, county_capacity)
        county['courses'] = [item['row']['course'] for item in known_items]
    else:
        county = dict(point=None, expected=None, low=None, high=None,
                      interval=None, capacity=0, over_capacity_risk=None,
                      risk=None, headroom=None, courses=[])

    consistency = _consistency(course_items, campus_rows, topic_rows, county)
    course_output = [item['row'] for item in course_items]
    return dict(
        as_of=cutoff,
        target=target,
        method=chosen,
        method_requested=method,
        interval=dict(level=INTERVAL_LEVEL, low_quantile=INTERVAL_LOW_Q,
                      high_quantile=INTERVAL_HIGH_Q),
        plan=plan_source,
        excluded_courses=excluded,
        relation_caliber=dict(
            known_at_cutoff=cutoff,
            relations=[dict(relation=row['relation'], parents=row['parents'],
                            child=row['child'], meaning=row['meaning'],
                            effective=row['effective'], known_at=row['known_at'])
                       for row in relations],
        ),
        method_selection=selection,
        courses=course_output,
        campuses=campus_rows,
        topics=topic_rows,
        county=county,
        levels=dict(course=course_output, campus=campus_rows,
                    topic=topic_rows, county=county),
        consistency=consistency,
        evidence={row['course']: row['evidence'] for row in course_output},
    )


# ---------------------------------------------------------------------------
# Backtesting: candidate methods are compared over consecutive enrollment
# terms, scoring both interval credibility and the cost of understaffing.


def _backtest_terms(store, cutoff):
    return sorted({row['term'] for row in store.table('history')
                   if row['published_at'] <= cutoff}, key=_term_key)


def score_method(store, method, cutoff):
    if method not in METHODS:
        raise DataError('unknown forecast method: ' + str(method))
    terms = _backtest_terms(store, cutoff)
    observations = []
    for term in terms:
        rows = [row for row in store.table('history') if row['term'] == term]
        if not rows:
            continue
        test_date = min(row['published_at'] for row in rows)
        prediction_cutoff = (date.fromisoformat(test_date) - timedelta(days=1)).isoformat()
        for row in rows:
            history = _history_rows(store, row['course'], prediction_cutoff)
            if not history:
                continue
            distribution = _course_distribution(history, method, term)
            if distribution is None:
                continue
            low, high = _interval(distribution)
            seats = row['seats']
            censored = row['enrolled'] >= seats
            # A full class only proves demand reached the seat count.
            realized_floor = seats if censored else row['enrolled']
            covered = high >= realized_floor
            shortage = max(0, realized_floor - high)
            alpha = 1.0 - INTERVAL_LEVEL
            score = high - low
            if realized_floor < low:
                score += (2.0 / alpha) * (low - realized_floor)
            elif realized_floor > high:
                score += (2.0 / alpha) * (realized_floor - high)
            observations.append(dict(
                course=row['course'], term=term,
                realized_floor=realized_floor, capacity_hit=bool(censored),
                low=low, high=high, width=high - low,
                covered=bool(covered), shortage=shortage,
                interval_score=score,
            ))
    if not observations:
        return dict(method=method, observations=0, coverage=None,
                    mean_shortage=None, mean_width=None,
                    mean_interval_score=None, selection_score=None,
                    terms=[])
    count = len(observations)
    coverage = sum(1 for row in observations if row['covered']) / count
    mean_shortage = sum(row['shortage'] for row in observations) / count
    mean_width = sum(row['width'] for row in observations) / count
    mean_interval = sum(row['interval_score'] for row in observations) / count
    calibration_gap = abs(coverage - INTERVAL_LEVEL)
    selection_score = (calibration_gap * 10.0
                       + mean_shortage * 2.0
                       + mean_width * 0.02)
    return dict(
        method=method, observations=count,
        coverage=_round(coverage), calibration_gap=_round(calibration_gap),
        mean_shortage=_round(mean_shortage), mean_width=_round(mean_width),
        mean_interval_score=_round(mean_interval),
        selection_score=_round(selection_score),
        terms=sorted({row['term'] for row in observations}, key=_term_key),
    )


def select_method(store, cutoff=DEFAULT_AS_OF, target=None, requested=None):
    _parse_date(cutoff, 'as_of')
    if requested is not None:
        if requested not in METHODS:
            raise DataError('unknown forecast method: ' + str(requested))
        candidates = [score_method(store, requested, cutoff)]
        return dict(chosen=requested, reason='explicit_method', target=target,
                    as_of=cutoff, interval_level=INTERVAL_LEVEL,
                    candidates=candidates)
    candidates = [score_method(store, method, cutoff) for method in METHODS]
    usable = [row for row in candidates if row['selection_score'] is not None]
    if not usable:
        chosen = DEFAULT_METHOD
    else:
        # Prefer a censored model when it is not materially worse than the
        # legacy point projection, because observed enrollment truncates true
        # demand at capacity.
        censored = [row for row in usable if row['method'] != 'last_observed']
        if censored:
            best = min(censored, key=lambda row: row['selection_score'])
            legacy = next((row for row in usable
                           if row['method'] == 'last_observed'), None)
            if legacy is not None and legacy['selection_score'] + 0.25 < best['selection_score']:
                chosen = legacy['method']
            else:
                chosen = best['method']
        else:
            chosen = min(usable, key=lambda row: row['selection_score'])['method']
    return dict(chosen=chosen, reason='backtest_score', target=target,
                as_of=cutoff, interval_level=INTERVAL_LEVEL,
                candidates=candidates)


# ---------------------------------------------------------------------------
# Released forecasts keep the plan that was current when they were issued.


def explain_releases(store, cutoff=DEFAULT_AS_OF):
    _parse_date(cutoff, 'as_of')
    groups = {}
    for row in store.table('forecasts'):
        groups.setdefault(row['release'], []).append(row)
    releases = []
    for release in sorted(groups):
        rows = sorted(groups[release], key=lambda row: row['course'])
        issued_at = min(row['issued_at'] for row in rows)
        target = rows[0]['target']
        plan_id = rows[0]['plan']
        plan = None
        for candidate in store.table('plans'):
            if (candidate['plan'] == plan_id
                    and candidate['known_at'] <= issued_at):
                if plan is None or candidate['known_at'] >= plan['known_at']:
                    plan = candidate
        if plan is not None:
            plan_snapshot = dict(plan=plan['plan'], known_at=plan['known_at'],
                                 target=plan['target'],
                                 courses=list(plan['courses']),
                                 caliber='stored_plan_at_issue')
        else:
            # A legacy release did not reference a durable plan row.  Its own
            # published rows are the snapshot; later plans must not rewrite
            # how that release is explained.
            plan_snapshot = dict(plan=plan_id, known_at=issued_at, target=target,
                                 courses=[row['course'] for row in rows],
                                 caliber='release_plan_at_issue')
        course_explanations = []
        for row in rows:
            observed = [history for history in store.table('history')
                        if history['course'] == row['course']
                        and history['term'] == target
                        and history['published_at'] <= cutoff]
            if not observed:
                outcome = dict(status='not_observed', observed=None,
                               capacity_hit=None, at_or_above_point=None)
            else:
                history = observed[0]
                capacity_hit = history['enrolled'] >= history['seats']
                # For a full class the point is a floor, so the stored point
                # is validated against the seat count rather than the
                # truncated enrollment.
                reference = history['seats'] if capacity_hit else history['enrolled']
                outcome = dict(status='observed', observed=history['enrolled'],
                               seats=history['seats'],
                               capacity_hit=capacity_hit,
                               at_or_above_point=bool(row['point'] >= reference))
            course_explanations.append(dict(
                course=row['course'], target=row['target'],
                issued_at=row['issued_at'], point=row['point'],
                plan=row['plan'], outcome=outcome,
            ))
        releases.append(dict(
            release=release, issued_at=issued_at, target=target,
            plan=plan_snapshot, courses=course_explanations,
            explanation='explained with the plan and evidence available at '
                        'issue time; stored points are not rewritten',
        ))
    return dict(as_of=cutoff, releases=releases)


# ---------------------------------------------------------------------------
# Staffing reservation basis


def reservation_basis(store, campuses=None, as_of=DEFAULT_AS_OF, target=None,
                      courses=None, method=None, hours_per_seat=1.0):
    cutoff = str(as_of)
    _parse_date(cutoff, 'as_of')
    try:
        hours_per_seat = float(hours_per_seat)
    except (TypeError, ValueError):
        raise DataError('hours_per_seat must be numeric') from None
    if hours_per_seat <= 0:
        raise DataError('hours_per_seat must be positive')
    target = target or _default_target(store, cutoff)
    selection = select_method(store, cutoff, target, method)
    chosen = selection['chosen']
    relations = _relations_known(store, cutoff, target)
    plan = _select_plan(store, cutoff, target)
    if plan is not None:
        names = list(plan['courses'])
        plan_source = dict(plan=plan['plan'], known_at=plan['known_at'],
                           target=plan['target'])
    else:
        names = [row['course'] for row in store.table('courses')
                 if row['introduced'] <= target]
        plan_source = dict(plan=None, known_at=None, target=target)
    selected = set(courses) if courses is not None else None
    names = [name for name in dict.fromkeys(names)
             if selected is None or name in selected]
    if selected is not None:
        unknown = selected - set(names)
        if unknown:
            raise DataError('course not in plan: ' + ','.join(sorted(unknown)))

    cache = {}
    items = []
    for name in names:
        if not store.select('courses', course=name):
            continue
        result = _forecast_course(store, name, cutoff, target, chosen,
                                  relations, cache)
        items.append(dict(course=name, catalog=_catalog(store, name),
                          distribution=result['distribution']))

    campus_parts = {}
    for item in items:
        if item['distribution'] is None:
            continue
        catalog = item['catalog']
        shares = _campus_shares(store, item['course'], relations, cache)
        split = split_distribution(item['distribution'], shares)
        capacities = _capacity_split(catalog['capacity'], list(shares), shares)
        for campus in shares:
            campus_parts.setdefault(campus, []).append(dict(
                course=item['course'], share=shares[campus],
                distribution=split[campus], capacity=capacities[campus],
                expected=item['distribution'].mean() * shares[campus]))
    hours = {row['campus']: row['teacher_hours']
             for row in store.table('reservations') if row['term'] == target}
    campus_rows = []
    for campus in sorted(campus_parts):
        if campuses is not None and campus not in campuses:
            continue
        parts = campus_parts[campus]
        distribution = aggregate(parts)
        capacity = sum(part['capacity'] for part in parts)
        row = dict(campus=campus, **_serialize_interval(distribution, capacity))
        teacher_hours = hours.get(campus)
        required_hours = int(ceil(row['high'] * hours_per_seat))
        row.update(
            teacher_hours=teacher_hours,
            hours_per_seat=hours_per_seat,
            suggested_seats=row['high'],
            suggested_hours=required_hours,
            hours_shortfall=(max(0, required_hours - int(teacher_hours))
                             if teacher_hours is not None else None),
            courses=[dict(course=part['course'],
                          share=_round(part['share']),
                          expected=_round(part['expected']))
                     for part in parts],
        )
        campus_rows.append(row)
    total_seats = sum(row['suggested_seats'] for row in campus_rows)
    total_hours = sum(row['suggested_hours'] for row in campus_rows)
    return dict(as_of=cutoff, target=target, method=chosen,
                plan=plan_source,
                excludes_campus_demand=(campuses is not None
                                        and len(campus_rows) < len(campus_parts)),
                campuses=campus_rows,
                total_suggested_seats=total_seats,
                total_suggested_hours=total_hours,
                hours_per_seat=hours_per_seat)
