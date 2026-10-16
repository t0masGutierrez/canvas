import contextlib
import importlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import canvas as c
from canvas_api import CanvasError, fetch_json, request_get
from playwright.sync_api import sync_playwright

class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.events.append((self.path,self.headers.get('Authorization')))
                if self.path=='/api/v1/users/self/profile':
                    self.send_response(302);self.send_header('Location',f'http://localhost:{self.server.server_port}/outside');self.end_headers();return
                if self.path=='/api/v1/redirect':
                    self.send_response(302);self.send_header('Location','/api/v1/list');self.end_headers();return
                if self.path=='/api/v1/outside':
                    self.send_response(302);self.send_header('Location',f'http://localhost:{self.server.server_port}/outside');self.end_headers();return
                if self.path=='/api/v1/retry' and sum(path==self.path for path,_ in cls.events)<2:
                    self.send_response(429);self.send_header('Retry-After','0');self.end_headers();return
                if self.path=='/file':
                    self.send_response(200);self.send_header('Content-Length','9');self.end_headers();self.wfile.write(b'%PDF-test');return
                if self.path=='/large':
                    self.send_response(200);self.send_header('Content-Length','200');self.end_headers();return
                if self.path=='/short':
                    self.send_response(200);self.send_header('Content-Length','20');self.end_headers();self.wfile.write(b'partial');return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'[{"id":1}]')
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.p=sync_playwright().start()
    @classmethod
    def tearDownClass(cls):
        cls.p.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):self.events.clear()
    @contextlib.contextmanager
    def context(self):
        req=self.p.request.new_context(base_url=self.base,extra_http_headers={'Authorization':'Bearer TEST-ONLY'})
        try:yield req
        finally:req.dispose()
    def test_same_origin_redirect_can_return_data(self):
        with self.context() as req:self.assertEqual(request_get(req,self.base,'/api/v1/redirect').json(),[{'id':1}])
    def test_redirect_to_other_host_is_blocked_before_credentials_are_sent(self):
        with self.context() as req,self.assertRaises(CanvasError):request_get(req,self.base,'/api/v1/outside')
        self.assertEqual(self.events,[('/api/v1/outside','Bearer TEST-ONLY')])
    def test_auth_verification_blocks_external_redirect(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c,'BASE_URL',self.base):
            state=Path(d)/'state.json';state.write_text(json.dumps({'canvas_cli_auth':{'type':'bearer_token','access_token':'TEST-ONLY'}}))
            with self.assertRaises((CanvasError,SystemExit)):c.verify_canvas_state_file(self.p,state)
        self.assertEqual(self.events,[('/api/v1/users/self/profile','Bearer TEST-ONLY')])
    def test_rate_limit_is_retried(self):
        with self.context() as req:self.assertEqual(request_get(req,self.base,'/api/v1/retry').json(),[{'id':1}])
        self.assertEqual(len(self.events),2)
    def test_protocol_relative_request_never_reaches_server(self):
        with self.context() as req,self.assertRaises(CanvasError):request_get(req,self.base,f'//localhost:{self.server.server_port}/api/v1/list')
        self.assertEqual(self.events,[])
    def test_download_contains_file_bytes_and_no_credentials(self):
        content=importlib.import_module('canvas_content')
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'notes.pdf'
            result=content.download_file({'id':7,'url':self.base+'/file','size':9},target,self.base)
            self.assertEqual(target.read_bytes(),b'%PDF-test');self.assertEqual(result['size'],9);self.assertEqual(len(result['sha256']),64)
        self.assertEqual(self.events,[('/file',None)])
    def test_download_does_not_overwrite_existing_file(self):
        content=importlib.import_module('canvas_content')
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'notes.pdf';target.write_bytes(b'keep')
            with self.assertRaises(CanvasError):content.download_file({'url':self.base+'/file'},target,self.base)
            self.assertEqual(target.read_bytes(),b'keep')
        self.assertEqual(self.events,[])
    def test_short_download_does_not_leave_a_file(self):
        content=importlib.import_module('canvas_content')
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'notes.pdf'
            with self.assertRaises(CanvasError):content.download_file({'url':self.base+'/short'},target,self.base)
            self.assertEqual(list(Path(d).iterdir()),[])
    def test_large_download_is_stopped(self):
        content=importlib.import_module('canvas_content')
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'notes.pdf'
            with self.assertRaises(CanvasError):content.download_file({'url':self.base+'/large'},target,self.base,max_bytes=10)
            self.assertEqual(list(Path(d).iterdir()),[])


class CertificateTests(unittest.TestCase):
    def test_download_has_trusted_roots_when_python_has_none(self):
        import ssl
        content=importlib.import_module('canvas_content')
        empty=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats()['x509_ca'],0)
        with patch.object(ssl,'create_default_context',return_value=empty):
            context=content.download_ssl_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'],0)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(context.check_hostname)


class DownloadAuthTests(unittest.TestCase):
    def test_canvas_download_can_authenticate_without_forwarding_credentials(self):
        import io
        from urllib.error import HTTPError
        content=importlib.import_module('canvas_content')
        seen=[]
        class Opener:
            def open(self, request, timeout):
                seen.append((request.full_url, request.get_header('Authorization'), request.get_header('Cookie')))
                if len(seen)==1:
                    raise HTTPError(request.full_url,302,'redirect',{'Location':'https://files.test/document'},io.BytesIO())
                result=io.BytesIO(b'%PDF-test');result.headers={'Content-Length':'9','Content-Type':'application/pdf'}
                return result
        with tempfile.TemporaryDirectory() as d,patch.object(content,'build_opener',return_value=Opener()):
            target=Path(d)/'notes.pdf'
            content.download_file({'id':7,'url':'https://school.test/files/7/download','size':9},target,'https://school.test',credential_headers=lambda url:{'Authorization':'Bearer TEST-ONLY','Cookie':'canvas_session=FAKE'})
            self.assertEqual(target.read_bytes(),b'%PDF-test')
        self.assertEqual(seen,[('https://school.test/files/7/download','Bearer TEST-ONLY','canvas_session=FAKE'),('https://files.test/document',None,None)])
    def test_login_html_is_not_saved_as_a_document(self):
        import io
        content=importlib.import_module('canvas_content')
        class Opener:
            def open(self, request, timeout):
                result=io.BytesIO(b'<html>login</html>');result.headers={'Content-Type':'text/html'};return result
        with tempfile.TemporaryDirectory() as d,patch.object(content,'build_opener',return_value=Opener()):
            target=Path(d)/'notes.pdf'
            with self.assertRaises(CanvasError):content.download_file({'url':'https://school.test/files/7/download','content-type':'application/pdf'},target,'https://school.test')
            self.assertFalse(target.exists())

if __name__ == "__main__": unittest.main()
