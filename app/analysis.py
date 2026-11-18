from .demand import (DEFAULT_AS_OF, METHODS, build_forecast, explain_releases,
                     reservation_basis, select_method)
from .domain import historical_series, legacy_projection
from .storage import DataError, single


def _as_of(query, default=DEFAULT_AS_OF):
    value = single(query, 'as_of', default)
    return value


def _method(query):
    return single(query, 'method')


def history(store,query):
    selected=single(query,'course')
    return {'series':[historical_series(store,course['course']) for course in store.table('courses')
                      if selected is None or course['course']==selected]}


def forecast(store, query):
    cutoff = _as_of(query)
    target = single(query, 'target')
    method = _method(query)
    bundle = build_forecast(store, cutoff, target, method)
    # Keep the flat ``forecasts`` list for existing consumers while exposing
    # the course / campus / topic / county levels and their consistency block.
    bundle['forecasts'] = [
        dict(course=row['course'], point=row['point'], low=row['low'],
             high=row['high'], risk=row['risk'], status=row['status'])
        for row in bundle['courses']
    ]
    # A release is explained with the plan that was current when it was
    # issued; later plans and later relations do not rewrite it.
    release_view = explain_releases(store, cutoff)
    bundle['releases'] = release_view['releases']
    bundle['release_rows'] = store.table('forecasts')
    bundle['method_note'] = ('candidate methods are backtested over consecutive '
                             'enrollment terms; the chosen method weighs interval '
                             'coverage against the cost of understaffing')
    return bundle


def releases(store, query):
    return explain_releases(store, _as_of(query))


def methods(store, query):
    cutoff = _as_of(query)
    target = single(query, 'target')
    method = _method(query)
    return select_method(store, cutoff, target, method)


def basis(store, query):
    cutoff = _as_of(query)
    target = single(query, 'target')
    method = _method(query)
    campuses = query.get('campus')
    if campuses is not None and (len(campuses) < 1 or any(not campus for campus in campuses)):
        raise DataError('one or more nonempty campus values required')
    courses = query.get('course')
    if courses is not None and (len(courses) < 1 or any(not course for course in courses)):
        raise DataError('one or more nonempty course values required')
    return reservation_basis(store, campuses=campuses, as_of=cutoff,
                             target=target, courses=courses, method=method)


ROUTES={'/api/history':history,
        '/api/forecast':forecast,
        '/api/releases':releases,
        '/api/methods':methods,
        '/api/reservation-basis':basis}
