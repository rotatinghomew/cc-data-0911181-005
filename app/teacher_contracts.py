from .storage import DataError


def confirm(store,journal,payload):
    with journal.transaction():
        request=journal.get('staff_request',payload['reservation'])
        teacher=store.find('teachers',teacher=payload['teacher'])
        slots=set(payload['slots'])
        if not slots or not slots<=set(request['slots']) or not slots<=set(teacher['slots']):
            raise DataError('invalid confirmed time')
        for other in journal.all('teacher_commitment'):
            if other['teacher']==teacher['teacher'] and other['reservation']!=request['reservation'] and slots&set(other['slots']):
                raise DataError('teacher time already committed')
        row=dict(reservation=request['reservation'],teacher=teacher['teacher'],slots=sorted(slots),
                 courses=request['courses'],capacity=teacher['capacity'])
        return journal.put('teacher_commitment',request['reservation'],row)
