from .domain import historical_series, legacy_projection
from .storage import single


def history(store,query):
    selected=single(query,'course')
    return {'series':[historical_series(store,course['course']) for course in store.table('courses')
                      if selected is None or course['course']==selected]}


def forecast(store,query):
    cutoff=single(query,'as_of','2026-11-01')
    return {'as_of':cutoff,'forecasts':[legacy_projection(store,course['course'],cutoff)
                                      for course in store.table('courses')],
            'method':'last_observed_enrollment','releases':store.table('forecasts')}


ROUTES={'/api/history':history,'/api/forecast':forecast}
