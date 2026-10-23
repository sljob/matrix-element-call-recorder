import unittest
from device_origin import device_kind,last_device
class Origin(unittest.TestCase):
 def test_platforms(self):
  for ua,kind in [('Android','мобильный'),('iPhone','мобильный'),('Windows NT','ПК'),('X11','ПК'),('Element X','не определено')]:
   self.assertEqual(device_kind({'last_seen_user_agent':ua}),kind)
 def test_latest_and_tie(self):
  self.assertEqual(last_device([{'last_seen_ts':1000,'last_seen_user_agent':'Android'},{'last_seen_ts':2000,'last_seen_user_agent':'Windows NT'}]),(2,'ПК'))
  self.assertEqual(last_device([{'last_seen_ts':2000,'last_seen_user_agent':'Android'},{'last_seen_ts':2000,'last_seen_user_agent':'Windows NT'}]),(2,'несколько типов устройств'))
 def test_dehydrated(self):
  self.assertEqual(last_device([{'last_seen_ts':1000,'dehydrated':True}])[0],None)
if __name__=='__main__':unittest.main()
