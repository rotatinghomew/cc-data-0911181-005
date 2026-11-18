from .demand import DEFAULT_AS_OF, reservation_basis
from .storage import DataError


def _campuses(store,courses,requested):
    known=set()
    for course in courses:
        known.update(store.find('courses',course=course)['campuses'])
    if requested is None:
        return sorted(known)
    requested=sorted(set(requested))
    if not requested:
        raise DataError('campus required')
    unknown=set(requested)-known
    if unknown:
        raise DataError('campus does not offer the courses: '+','.join(sorted(unknown)))
    return requested


def _basis(store,courses,campuses,payload):
    return reservation_basis(store,campuses=campuses,courses=courses,
                             as_of=payload.get('as_of',DEFAULT_AS_OF),
                             target=payload.get('target'),
                             method=payload.get('method'))


def propose(store,journal,payload):
    courses=sorted(set(payload['courses']))
    if not courses:
        raise DataError('courses required')
    for course in courses:
        store.find('courses',course=course)
    slots=sorted(set(payload['slots']))
    if not slots:
        raise DataError('teaching slots required')
    requested=payload.get('campuses',payload.get('campus'))
    if isinstance(requested,str):
        requested=[requested]
    campuses=_campuses(store,courses,requested)
    basis=_basis(store,courses,campuses,payload)
    row=dict(reservation=payload['reservation'],courses=courses,slots=slots,
             campuses=campuses,status='proposed',
             target=basis['target'],as_of=basis['as_of'],method=basis['method'],
             plan=basis['plan'],demand_basis=basis)
    with journal.transaction():
        existing=journal.maybe('staff_request',row['reservation'])
        commitment=journal.maybe('teacher_commitment',row['reservation'])
        if commitment is not None:
            # Confirmed hours stay attached to the courses and slots that were
            # confirmed.  A later course combination must not quietly move
            # them to a new course.
            if commitment['courses']!=row['courses']:
                raise DataError('confirmed courses cannot be changed')
            if not set(commitment['slots'])<=set(row['slots']):
                raise DataError('confirmed slots cannot be removed')
        if existing is not None:
            row['version']=existing.get('version',1)+1
        else:
            row['version']=1
        return journal.put('staff_request',row['reservation'],row)
