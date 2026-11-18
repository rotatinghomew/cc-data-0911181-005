from math import floor

from .storage import DataError


def _confirmable_slots(request,teacher):
    return sorted(set(request['slots'])&set(teacher['slots']))


def confirm(store,journal,payload):
    with journal.transaction():
        request=journal.get('staff_request',payload['reservation'])
        teacher=store.find('teachers',teacher=payload['teacher'])
        slots=sorted(set(payload['slots']))
        if not slots or not set(slots)<=set(request['slots']) or not set(slots)<=set(teacher['slots']):
            raise DataError('invalid confirmed time')
        # A confirmation is for whole time blocks, never a fraction of a block.
        for other in journal.all('teacher_commitment'):
            if (other['teacher']==teacher['teacher']
                    and other['reservation']!=request['reservation']
                    and set(slots)&set(other['slots'])):
                # This also covers a joint course offered at two campuses: one
                # teacher cannot be promised in both places in the same block.
                raise DataError('teacher time already committed')
        requested=len(request['slots'])
        confirmed=len(slots)
        # Confirming only part of the requested blocks leaves the remaining
        # blocks unstaffed, so the guarantee must shrink with the teacher's
        # confirmed share instead of keeping the original capacity.
        capacity=int(floor(teacher['capacity']*confirmed/requested))
        unconfirmed=sorted(set(request['slots'])-set(slots))
        row=dict(reservation=request['reservation'],teacher=teacher['teacher'],
                 slots=slots,unconfirmed_slots=unconfirmed,
                 courses=request['courses'],campuses=request['campuses'],
                 capacity=capacity,teacher_capacity=teacher['capacity'],
                 capacity_basis='confirmed_share_of_teacher_capacity',
                 requested_slots=requested,confirmed_slots=confirmed,
                 request_version=request.get('version',1),
                 status='confirmed' if not unconfirmed else 'partially_confirmed')
        promised=journal.maybe('seat_promise',request['reservation'])
        if promised is not None and promised['seats']>capacity:
            raise DataError('confirmed time cannot support the published seats')
        return journal.put('teacher_commitment',request['reservation'],row)
