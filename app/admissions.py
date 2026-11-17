from .storage import DataError


def publish(store,journal,payload):
    with journal.transaction():
        commitment=journal.get('teacher_commitment',payload['reservation'])
        seats=int(payload['seats'])
        if seats<=0 or seats>commitment['capacity']:
            raise DataError('seats exceed confirmed teacher capacity')
        row=dict(reservation=commitment['reservation'],courses=commitment['courses'],
                 slots=commitment['slots'],seats=seats,status='published')
        return journal.put('seat_promise',row['reservation'],row)
