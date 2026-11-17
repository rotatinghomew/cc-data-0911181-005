def historical_series(store,course):
    catalog=store.find('courses',course=course)
    rows=sorted(store.select('history',course=course),key=lambda row:row['term'])
    return dict(course=course,campuses=catalog['campuses'],topic=catalog['topic'],
                observations=[dict(row,capacity_hit=row['enrolled']>=row['seats']) for row in rows])


def legacy_projection(store,course,cutoff):
    rows=[row for row in store.select('history',course=course) if row['published_at']<=cutoff]
    if not rows:
        return dict(course=course,point=None,reason='no_observations')
    latest=max(rows,key=lambda row:(row['term'],row['published_at']))
    return dict(course=course,point=latest['enrolled'],source_term=latest['term'],
                source_published_at=latest['published_at'])
