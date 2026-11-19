"""Offline URL error classification regressions; no real network or business data."""
import sys, unittest, tempfile, socket, json
from pathlib import Path
from unittest import mock
from http.client import HTTPException
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline import atlas_fmt_engine as engine
import url_ingest_helper as helper
from storage.vault_kek_store import VaultStoreError

class URLBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def code(self, expected, call):
  with self.assertRaises(VaultStoreError) as caught:call()
  self.assertEqual(caught.exception.code,expected)
  self.assertEqual(list(self.root.iterdir()),[])
 def check(self):engine.check_url(self.root,'https://source-unavailable.invalid/report')
 def test_dns_nxdomain_through_real_helper(self):
  response=mock.MagicMock();response.__enter__.return_value.read.return_value=b'{"Status":3}'
  opener=mock.Mock();opener.open.return_value=response
  with mock.patch.object(helper.socket,'getaddrinfo',return_value=[(2,1,6,'',('198.18.0.1',443))]), mock.patch.object(helper.urllib.request,'build_opener',return_value=opener):
   self.code('E_URL_NETWORK',self.check)
   self.assertEqual(opener.open.call_count,1)
 def test_bad_dns_json(self):
  response=mock.MagicMock();response.__enter__.return_value.read.return_value=b'not-json'
  opener=mock.Mock();opener.open.return_value=response
  with mock.patch.object(helper.socket,'getaddrinfo',return_value=[(2,1,6,'',('198.18.0.1',443))]), mock.patch.object(helper.urllib.request,'build_opener',return_value=opener):self.code('E_URL_NETWORK',self.check)
 def test_empty_dns_and_socket_failure(self):
  for effect in ([],socket.gaierror('synthetic dns failure')):
   with self.subTest(effect=str(effect)),mock.patch.object(helper.socket,'getaddrinfo',**({'side_effect':effect} if isinstance(effect,Exception) else {'return_value':effect})):
    self.code('E_URL_NETWORK',self.check)
 def test_http_protocol_failure(self):
  with mock.patch.object(engine,'_check_url',side_effect=HTTPException('synthetic protocol failure')):self.code('E_URL_NETWORK',self.check)
 def test_policy_rejections_do_not_lookup_or_download(self):
  for url in ('file:///tmp/example','https://user:password@example.com','http://127.0.0.1/'):
   with self.subTest(url=url),mock.patch.object(helper.socket,'getaddrinfo') as dns,mock.patch.object(engine,'download_url') as download:
    self.code('E_URL_POLICY',lambda:engine.check_url(self.root,url));dns.assert_not_called();download.assert_not_called()
 def test_malformed_dns_answer_is_network_failure(self):
  values=[None,[None],[{"type":1}],[{"type":1,"data":"invalid"}],[{"type":1,"data":"8.8.8.8","TTL":"invalid"}],[{"type":1,"data":123}]]
  for value in values:
   response=mock.MagicMock();response.__enter__.return_value.read.return_value=json.dumps({"Status":0,"Answer":value}).encode()
   opener=mock.Mock();opener.open.return_value=response
   with self.subTest(value=value),mock.patch.object(helper.socket,'getaddrinfo',return_value=[(2,1,6,'',('198.18.0.1',443))]),mock.patch.object(helper.urllib.request,'build_opener',return_value=opener):self.code('E_URL_NETWORK',self.check)
 def test_public_dns_private_answer_is_policy_failure(self):
  response=mock.MagicMock();response.__enter__.return_value.read.return_value=b'{"Status":0,"Answer":[{"type":1,"data":"127.0.0.1","TTL":0}]}'
  opener=mock.Mock();opener.open.return_value=response
  with mock.patch.object(helper.socket,'getaddrinfo',return_value=[(2,1,6,'',('198.18.0.1',443))]),mock.patch.object(helper.urllib.request,'build_opener',return_value=opener):self.code('E_URL_POLICY',self.check)
if __name__=='__main__':unittest.main()
