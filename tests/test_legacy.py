"""Canvas review tests. No live requests or changes to the source project.
Run with a Python environment that has Playwright installed.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import sys
SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'canvas.py'
sys.path.insert(0, str(SOURCE.parent))
spec = importlib.util.spec_from_file_location('canvas_under_review', SOURCE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
COURSE = {'id': 42, 'course_code': 'PHY 315', 'name': 'Waves (12345)'}
ASSIGNMENT = {'id': 7, 'name': 'Homework', 'description': '<p>Read chapter 1</p>', 'html_url': 'https://school.test/courses/42/assignments/7', 'due_at': '2026-10-04T05:00:00Z', 'points_possible': 10, 'submission': {'score': 8, 'submitted_at': '2026-10-03T12:00:00Z'}}

class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}
    def dispose(self): pass
    def json(self): return self.data
    def text(self): return json.dumps(self.data)

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        c.set_canvas_base_url('https://school.test')
    def main_run(self, args, fetch=None, raw=None):
        state = self.root/'state.json'; state.write_text('{}')
        req = Mock()
        def get(endpoint, **kwargs):
            if raw is not None: return raw
            if endpoint.startswith('/api/v1/test') or endpoint.startswith('/api/v1/courses/42?'): return Response({'id':42})
            if '/items?' in endpoint: return Response([])
            return Response((fetch or self.fixture_fetch)(req, endpoint))
        req.get.side_effect = get
        output = io.StringIO()
        with patch.object(c, 'STATE_FILE', state), patch.object(c, 'CONFIG_FILE', self.root/'config.json'), patch.object(c, 'CANVAS_FILES_ROOT', self.root/'files'), patch.object(c, 'sync_playwright'), patch.object(c, 'CanvasRequestSession', return_value=req), patch.object(c, 'fetch_paginated', side_effect=fetch or self.fixture_fetch), patch.object(c, 'fetch_course_root_folder', return_value={'id': 1}), patch.object(c, 'open_url') as opened, contextlib.redirect_stdout(output):
            c.main(args)
        return output.getvalue(), req, opened
    def fixture_fetch(self, req, endpoint):
        if endpoint.startswith('/api/v1/courses?'): return [COURSE]
        if '/assignments?' in endpoint: return [ASSIGNMENT]
        if '/assignment_groups?' in endpoint: return [{'name':'Homeworks','assignments':[ASSIGNMENT]}]
        if '/enrollments?' in endpoint: return [{'course_id':42,'grades':{'current_score':80}}]
        if '/announcements?' in endpoint: return [{'id':8,'context_code':'course_42','title':'Notice','message':'<p>Hello <a href="https://school.test/file">File</a></p>','posted_at':'2026-10-03T12:00:00Z'}]
        if '/users?' in endpoint: return [{'id':5,'name':'Person'}]
        if '/pages?' in endpoint: return [{'page_id':9,'title':'Guide','url':'guide','body':'Instructions'}]
        if '/modules?' in endpoint: return [{'id':4,'name':'Week 1','position':1,'items_count':2}]
        if '/folders/' in endpoint and '/folders?' in endpoint: return []
        if '/folders/' in endpoint and '/files?' in endpoint: return [{'id':3,'filename':'notes.pdf'}]
        raise AssertionError(endpoint)
    def test_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e: c.parse_args(['--help'])
        self.assertEqual(e.exception.code,0); self.assertIn('assignments',out.getvalue())
    def test_no_arguments(self): self.assertIn('usage:',self.main_run([])[0])
    def test_unknown_command(self):
        with self.assertRaises(SystemExit): c.resolve_shortcut_command('nonsense')
    def test_conflicting_sort_flags(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(c.CanvasError): c.parse_args(['assignments','--date','--name'])
    def test_intermixed_options(self): self.assertEqual(c.parse_args(['assignments','--json','PHY 315']).filters,['PHY 315'])
    def test_setup(self):
        out,_,_=self.main_run(['setup','school.test']); self.assertIn('Saved',out)
        self.assertEqual(json.loads((self.root/'config.json').read_text())['base_url'],'https://school.test')
        self.assertEqual((self.root/'config.json').stat().st_mode&0o777,0o600)
    def test_setup_stdin(self):
        with patch('sys.stdin',io.StringIO('school.test\n')), contextlib.redirect_stdout(io.StringIO()): c.setup_canvas_institution([],{},self.root/'config.json')
        self.assertTrue((self.root/'config.json').exists())
    def test_url_normalization(self): self.assertEqual(c.normalize_canvas_base_url(' school.test/courses '),'https://school.test')
    def test_invalid_url(self):
        with self.assertRaises(SystemExit): c.normalize_canvas_base_url('ftp://school.test')
    def test_external_endpoint_rejected(self):
        with self.assertRaises(c.CanvasError): c.normalize_endpoint('https://other.test/api/v1/courses')
    def test_external_pagination_rejected(self):
        with self.assertRaises(c.CanvasError): c.next_url_from_link('<https://other.test/next>; rel="next"')
    def test_endpoint_normalization(self): self.assertEqual(c.normalize_endpoint('api/v1/courses'),'/api/v1/courses')
    def test_pagination(self):
        req=Mock(); req.get.side_effect=[Response([1],headers={'link':'<https://school.test/api/v1/next>; rel="next"'}),Response([2])]
        self.assertEqual(c.fetch_paginated(req,'/api/v1/first'),[1,2]); self.assertEqual(req.get.call_count,2)
    def test_pagination_requires_list(self):
        req=Mock(); req.get.return_value=Response({})
        with self.assertRaises(c.CanvasError): c.fetch_paginated(req,'/api/v1/first')
    def test_forbidden(self):
        with self.assertRaisesRegex(c.CanvasError,'access denied'): c.ensure_ok(Response({'errors':[{'message':'user not authorized'}]},403))
    def test_server_error(self):
        with self.assertRaisesRegex(c.CanvasError,'HTTP 500'): c.ensure_ok(Response({},500))
    def test_invalid_json(self): self.assertIsNone(c.parse_response_json('<html>'))
    def test_course_filter(self): self.assertEqual(c.select_courses([COURSE],['phy315']),[COURSE])
    def test_course_no_match(self):
        with self.assertRaises(SystemExit): c.select_courses([COURSE],['missing'])
    def test_course_ambiguous(self):
        with self.assertRaises(SystemExit): c.select_single_course([COURSE,{'id':43,'course_code':'PHY 316'}],['phy'])
    def test_course_unquoted(self):
        with self.assertRaises(SystemExit): c.select_courses([COURSE],['PHY','315'])
    def test_hidden_courses(self): self.assertEqual(c.filter_hidden_courses([COURSE],{'hidden_courses':['315']}),[])
    def test_section(self): self.assertEqual(c.course_section_number(COURSE),'12345')
    def test_teacher(self): self.assertEqual(c.course_instructors({'teachers':[{'name':'TA','role':'TaEnrollment'},{'name':'Professor'}]}),'Professor')
    def test_sort_name(self): self.assertEqual(c.sort_assignment_rows([{'name':'Z'},{'name':'a'}],'name')[0]['name'],'a')
    def test_sort_date(self): self.assertEqual(c.sort_assignment_rows([{'due_date':''},{'due_date':'2026-01-01'},{'due_date':'2026-10-01'}],'date')[0]['due_date'],'2026-10-01')
    def test_grade_authoritative_total(self): self.assertEqual(c.grade_rows([{'assignments':[ASSIGNMENT]}],{'grades':{'current_score':95}},COURSE)[-1]['grade_percent'],'95%')
    def test_grade_null(self): self.assertEqual(c.format_percent(None,10),'-')
    def test_html(self): self.assertEqual(c.strip_html('<p>A &amp; B</p>'),'A & B')
    def test_file_link(self):
        target=self.root/'notes.pdf'; c.write_webloc_link('https://school.test/?a=1&b=2',target)
        self.assertTrue(target.is_symlink()); self.assertTrue(c.target_webloc_matches_url(target,'https://school.test/?a=1&b=2'))
        self.assertIn('<plist',target.read_text())
    def test_file_unchanged(self):
        target=self.root/'notes.pdf'; info={'id':3}
        self.assertEqual(c.write_canvas_file(None,info,target,self.root,42),'linked')
        self.assertEqual(c.write_canvas_file(None,info,target,self.root,42),'unchanged')
    def test_file_backup(self):
        target=self.root/'notes.pdf'; target.write_bytes(b'original')
        with patch.object(Path,'home',return_value=self.root/'home'):
            c.write_canvas_file(None,{'id':3},target,self.root,42)
        self.assertEqual(next((self.root/'home').rglob('notes.pdf')).read_bytes(),b'original')
    def test_file_missing_url(self): self.assertEqual(c.write_canvas_file(None,{},self.root/'x',self.root),'skipped')
    def test_file_sanitize(self): self.assertEqual(c.sanitize_path_component('../x:y'),' -x-y'.strip())
    def test_name_collision(self):
        names=set(); self.assertEqual(c.uniquify_path(self.root,'a.pdf',names).name,'a.pdf'); self.assertEqual(c.uniquify_path(self.root,'a.pdf',names).name,'a (2).pdf')
    def test_empty_files_root(self):
        with self.assertRaises(SystemExit): c.download_canvas_folder_tree(None,{},self.root)
    def test_auth_token(self):
        f=self.root/'state'; f.write_text(json.dumps({'canvas_cli_auth':{'type':'bearer_token','access_token':'TEST-ONLY'}}))
        self.assertEqual(c.canvas_request_context_kwargs(f)['extra_http_headers']['Authorization'],'Bearer TEST-ONLY')
    def test_auth_cookie(self):
        self.assertIn('storage_state',c.canvas_request_context_kwargs(self.root/'missing'))
    def test_auth_import_transaction(self):
        f=self.root/'state'; f.write_text('old')
        with patch.object(c,'verify_canvas_state_file',side_effect=SystemExit('invalid')), self.assertRaises(SystemExit): c.import_canvas_storage_state({},None,f)
        self.assertEqual(f.read_text(),'old'); self.assertFalse((self.root/'state.tmp').exists())
    def test_auth_import_success(self):
        f=self.root/'state'
        with patch.object(c,'verify_canvas_state_file',return_value={'id':1}): c.import_canvas_storage_state({'cookies':[]},None,f)
        self.assertEqual(f.stat().st_mode&0o777,0o600)
    def test_auth_refresh(self):
        p=Mock(); p.request.new_context.return_value.get.side_effect=[Response({},401),Response([1])]
        session=c.CanvasRequestSession(p,self.root/'missing')
        with patch.object(session,'refresh_auth_from_browser_session',return_value=True): self.assertEqual(session.get('/api/v1/x').status,200)
    def test_auth_noninteractive(self):
        with self.assertRaises(c.CanvasError) as error, patch.object(c,'open_canvas_in_chrome') as opened: c.wait_for_canvas_authentication(None,config={'auto_open_chrome_for_login':False})
        self.assertEqual(error.exception.code, 'AUTH_REQUIRED')
        opened.assert_not_called()
    def test_cookie_time(self): self.assertEqual(c.chrome_time_to_unix((c.CHROME_COOKIE_EPOCH_OFFSET+100)*1000000),100)
    def test_cookie_plaintext(self): self.assertEqual(c.decrypt_chrome_cookie_value('school.test','value',b'',None),'value')
    def test_profiles(self): self.assertEqual(c.normalize_profile_names('Default, Profile 1',['Default']),['Default','Profile 1'])
    def test_cookie_missing_db(self): self.assertEqual(c.read_canvas_cookies_from_chrome_db(self.root/'missing'),[])
    def test_config_invalid(self):
        f=self.root/'config'; f.write_text('invalid'); self.assertEqual(c.load_config(f),{})
    def test_private_keys(self): self.assertEqual(c.strip_private_keys({'__x':1,'a':[{'__y':2,'z':3}]}),{'a':[{'z':3}]})
    def test_render_all_shapes(self):
        for value in [[],{},[1,2],{'id':1,'name':'A'},[{'id':1,'name':'A'}],None]: self.assertIsInstance(c.render_human(value),str)
    def test_raw_json(self): self.assertEqual(json.loads(self.main_run(['/api/v1/test','--json'])[0]),{'id':42})
    def test_home_open(self): self.main_run(['home'])[2].assert_called_once_with('https://school.test')
    def test_syllabus_open(self): self.main_run(['syllabus','42'])[2].assert_called_once_with('https://school.test/courses/42/assignments/syllabus')

# Test each public command in both output formats, using fixed Canvas responses.
def command_test(command,json_mode):
    def test(self):
        args=[command]
        if command in ('files','pages','syllabus'): args.append('42')
        if json_mode: args.append('--json')
        output,_,_=self.main_run(args)
        if json_mode: self.assertIsInstance(json.loads(output),(list,dict))
        else: self.assertTrue(output)
    return test
for command in ['home', 'courses', 'announcements', 'assignments', 'grades', 'people', 'pages', 'files', 'syllabus', 'modules']:
    if command=='setup': continue
    for json_mode in (False,True): setattr(ReviewTests,f'test_command_{command}_{"json" if json_mode else "text"}',command_test(command,json_mode))



if __name__ == "__main__": unittest.main()
