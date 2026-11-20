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
 def test_empty_short_page_without_links_is_auto_deleted(self):
  md=self.root/'empty_stub.md'
  md.write_text('---\ntitle: "Empty"\n---\n\n> [!NOTE] 离线网页转译提示\n> 本文由本地 HTML 文档结构化清洗脱水转译。\n\n暂无正文，请稍后再试。\n',encoding='utf-8')
  res=engine._inspect_thin_url_output(md,'https://example.com/empty',min_chars=300,depth=1)
  self.assertIsNotNone(res)
  self.assertEqual(res['status'],'discarded_too_short')
  self.assertTrue(res['auto_deleted'])
  self.assertFalse(md.exists())
 def test_pure_directory_over_300_raw_chars_detected_and_deleted(self):
  md=self.root/'dir_only.md'
  items='\n'.join([f'- [Subsystem Guide {i}: Architecture & Design Reference](https://example.com/docs/sub{i})' for i in range(1,12)])
  md.write_text(f'---\ntitle: "Docs Index"\n---\n\n# 目录\n\n{items}\n',encoding='utf-8')
  res=engine._inspect_thin_url_output(md,'https://example.com/docs',min_chars=300,depth=1)
  self.assertIsNotNone(res)
  self.assertEqual(res['status'],'thin_with_links')
  self.assertTrue(res['is_directory_only'])
  self.assertEqual(res['next_depth'],2)
  self.assertFalse(res['depth_limit_reached'])
  self.assertEqual(len(res['extracted_links']),11)
  self.assertFalse(md.exists())
 def test_substantive_article_with_inline_links_is_kept(self):
  md=self.root/'real_article.md'
  para=('这是一篇包含真实技术原理推导与状态机分析的深度工程文档，详细解释了循环边界、事件溯源与容错契约。' * 12)
  md.write_text(f'---\ntitle: "Real Article"\n---\n\n{para}\n参见 [官方说明](https://example.com/ref)。\n',encoding='utf-8')
  override,metrics=engine._evaluate_url_output(md,'https://example.com/article',min_chars=300,depth=2)
  self.assertIsNone(override)
  self.assertGreaterEqual(metrics['prose_chars'],300)
  self.assertFalse(metrics.get('index_candidate',False))
  self.assertTrue(md.exists())
 def test_link_heavy_borderline_index_flagged_for_cleanup(self):
  md=self.root/'borderline_index.md'
  lines='\n'.join([f'- [Topic {i}](https://example.com/t{i})：简述模块第{i}部分功能与架构说明。' for i in range(1,22)])
  md.write_text(f'---\ntitle: "Section Index"\n---\n\n{lines}\n',encoding='utf-8')
  override,metrics=engine._evaluate_url_output(md,'https://example.com/index',min_chars=300,depth=1)
  self.assertIsNone(override)
  self.assertTrue(metrics.get('index_candidate'))
  self.assertEqual(metrics.get('link_count'),21)
 def test_crawl_depth_limit_enforced_at_3(self):
  self.assertEqual(engine._validate_crawl_depth(None),1)
  self.assertEqual(engine._validate_crawl_depth(3),3)
  self.code('E_PARAM_INVALID',lambda:engine._validate_crawl_depth(4))
  self.code('E_PARAM_INVALID',lambda:engine._validate_crawl_depth(0))
if __name__=='__main__':unittest.main()
