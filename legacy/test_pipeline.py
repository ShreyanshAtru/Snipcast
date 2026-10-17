import datetime as dt, json, pathlib, tempfile, unittest
from unittest.mock import patch
from legacy import pipeline as p
class PipelineTests(unittest.TestCase):
    def test_ist_slots(self):
        self.assertEqual(p.slots('2026-10-20',['19:00','12:00'],'Asia/Kolkata',3),['2026-10-20T06:30:00Z','2026-10-20T13:30:00Z','2026-10-21T06:30:00Z'])
    def test_dst_rejected(self):
        with self.assertRaises(ValueError): p.slots('2026-11-01',['01:30'],'America/New_York',1)
    def test_payload(self):
        i=dict(date='2027-01-01T00:00:00Z',integration_id='channel',title='Part 1',description='Test #Shorts',visibility='private',made_for_kids='no',tags=['test'])
        result=p.payload(i,dict(id='media',path='https://example.com/a.mp4'))
        self.assertEqual(result['posts'][0]['settings']['tags'],[{'value':'test','label':'test'}])
        self.assertEqual(result['posts'][0]['value'][0]['content'],'Test #Shorts')
    def test_unknown_blocks_retry(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'manifest.json'; p.save(path,{'posts':[{'state':'unknown'}]})
            with patch.object(p,'Postiz') as client:
                with self.assertRaises(ValueError): p.schedule(path)
                client.return_value.call.assert_not_called()
    def test_scheduled_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'manifest.json'; p.save(path,{'posts':[{'state':'scheduled'}]})
            with patch.object(p,'Postiz') as client:
                p.schedule(path); client.return_value.call.assert_not_called()
if __name__=='__main__': unittest.main()
