from math import floor

from .storage import DataError


def publish(store,journal,payload):
    with journal.transaction():
        commitment=journal.get('teacher_commitment',payload['reservation'])
        request=journal.get('staff_request',commitment['reservation'])
        seats=int(payload['seats'])
        if seats<=0 or seats>commitment['capacity']:
            raise DataError('seats exceed confirmed teacher capacity')
        # The published seat guarantee may not exceed the demand interval the
        # reservation was built on, and it must fit inside the capacity of the
        # campuses that actually carry the offer.
        limit=int(floor(request['demand_basis']['total_suggested_seats']))
        if seats>limit:
            raise DataError('seats exceed supported demand')
        campus_limit=sum(int(row['capacity']) for row in request['demand_basis']['campuses'])
        if seats>campus_limit:
            raise DataError('seats exceed campus capacity')
        row=dict(reservation=commitment['reservation'],courses=commitment['courses'],
                 campuses=commitment['campuses'],
                 slots=commitment['slots'],unconfirmed_slots=commitment['unconfirmed_slots'],
                 seats=seats,status='published')
        return journal.put('seat_promise',row['reservation'],row)
