from pathlib import Path
import tempfile
import unittest
from app.journal import Journal
from app.storage import DataError,Store

DATA=Path(__file__).parents[1]/'data'


class BusinessChainTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(DATA)
        self.journal=Journal()

    def tearDown(self):
        self.journal.close()

    def test_business_chain(self):
        self.exercise(self.store,self.journal)

    def test_rollback_on_failure(self):
        with self.assertRaises(DataError):
            with self.journal.transaction():
                self.journal.put('test','a',{'value':1})
                raise DataError('aborted')
        self.assertEqual(self.journal.all('test'),[])

    def test_restart_retains_business_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'workflow.sqlite3'
            journal=Journal(path)
            try:
                self.exercise(self.store,journal)
                before={kind[0] for kind in journal.connection.execute('SELECT DISTINCT kind FROM documents')}
                snapshot={kind:journal.all(kind) for kind in before}
            finally:
                journal.close()
            reopened=Journal(path)
            try:
                self.assertEqual(snapshot,{kind:reopened.all(kind) for kind in before})
            finally:
                reopened.close()

    def exercise(self,store,journal):
        from app import staffing,teacher_contracts,admissions
        proposal=staffing.propose(store,journal,dict(reservation='r1',courses=['C8'],slots=['mon-am','wed-am']))
        self.assertEqual(proposal['campuses'],['S0','S1'])
        self.assertGreater(proposal['demand_basis']['total_suggested_seats'],0)
        # Confirming one of the two requested blocks halves the guaranteed
        # teacher capacity; the published seats must reflect the remainder.
        commitment=teacher_contracts.confirm(store,journal,dict(reservation='r1',teacher='T0',slots=['mon-am']))
        self.assertEqual(commitment['capacity'],15)
        self.assertEqual(commitment['unconfirmed_slots'],['wed-am'])
        with self.assertRaises(DataError):
            admissions.publish(store,journal,dict(reservation='r1',seats=25))
        self.assertEqual(admissions.publish(store,journal,dict(reservation='r1',seats=15))['slots'],['mon-am'])
        # A different course combination cannot inherit the confirmed hours.
        with self.assertRaises(DataError):
            staffing.propose(store,journal,dict(reservation='r1',courses=['C8','C2'],slots=['mon-am','wed-am']))
        staffing.propose(store,journal,dict(reservation='r2',courses=['C2'],slots=['mon-am']))
        with self.assertRaises(DataError):
            teacher_contracts.confirm(store,journal,dict(reservation='r2',teacher='T0',slots=['mon-am']))
        self.assertEqual(len(journal.all('teacher_commitment')),1)
