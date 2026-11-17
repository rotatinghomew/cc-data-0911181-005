from .storage import DataError


def propose(store,journal,payload):
    courses=sorted(set(payload['courses']))
    for course in courses:
        store.find('courses',course=course)
    slots=sorted(set(payload['slots']))
    if not slots:
        raise DataError('teaching slots required')
    row=dict(reservation=payload['reservation'],courses=courses,slots=slots,status='proposed')
    with journal.transaction():
        return journal.put('staff_request',row['reservation'],row)
