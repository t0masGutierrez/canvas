"""Tests for agent reads. All requests use fixed responses or local HTTP."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import canvas as c

class Response:
    def __init__(self, data=None, status=200, headers=None, text=None):
        self.data, self.status, self.headers, self.raw = data, status, headers or {}, text
    def json(self):
        if self.raw is not None: return json.loads(self.raw)
        return self.data
    def text(self): return self.raw if self.raw is not None else json.dumps(self.data)
    def dispose(self): pass

class Request:
    def __init__(self, routes): self.routes, self.calls = routes, []
    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        value = self.routes[path]
        if isinstance(value, list): value = value.pop(0)
        if isinstance(value, Exception): raise value
        return value
    def dispose(self): pass

class Playwright:
    def __init__(self, req): self.request = self; self.req = req
    def new_context(self, **kwargs): return self.req
    def __enter__(self): return self
    def __exit__(self, *args): pass

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root/'state.json'; self.state.write_text('{}')
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(c,'STATE_FILE', self.state))
        self.stack.enter_context(patch.object(c,'CONFIG_FILE', self.root/'config.json'))
        c.set_canvas_base_url('https://school.test')
    def invoke(self, args, routes):
        req=Request(routes); output=io.StringIO(); error=io.StringIO(); code=0
        with patch.object(c,'sync_playwright', return_value=Playwright(req)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try: c.main(args)
            except SystemExit as e: code=e.code if isinstance(e.code,int) else 1
        return code, output.getvalue(), error.getvalue(), req
    def test_no_input_home_and_syllabus_do_not_open_windows(self):
        for arguments in (['home'], ['home', '42'], ['syllabus', '42']):
            with self.subTest(arguments=arguments), patch.object(c, 'open_url') as opened:
                code, out, _, _ = self.invoke(arguments + ['--no-input'], {
                    '/api/v1/courses?enrollment_state=active&per_page=100':
                        Response([{'id': 42, 'name': 'Course'}]),
                })
                self.assertEqual(code, 0)
                opened.assert_not_called()
                self.assertIn('https://school.test', out)
                self.assertNotIn('Opened', out)

    def test_protocol_relative_host_is_rejected(self):
        with self.assertRaises((Exception,SystemExit)): c.normalize_endpoint('//other.test/api/v1/users/self')
    def test_backslash_host_is_rejected(self):
        with self.assertRaises((Exception,SystemExit)): c.normalize_endpoint('/\\other.test/api/v1/users/self')
    def test_browser_logout_is_not_an_api_read(self):
        with self.assertRaises((Exception,SystemExit)): c.normalize_endpoint('/logout')
    def test_numeric_course_id_is_exact(self):
        self.assertEqual(c.select_courses([{'id':42,'course_code':'PHY'},{'id':142,'course_code':'MATH'}],['42']),[{'id':42,'course_code':'PHY'}])
    def test_users_with_same_name_are_not_combined(self):
        self.assertEqual(len(c.people_rows([{'id':1,'name':'Alex'},{'id':2,'name':'Alex'}])),2)
    def test_pages_with_same_title_are_not_combined(self):
        self.assertEqual(len(c.page_rows([{'page_id':1,'title':'Guide'},{'page_id':2,'title':'Guide'}])),2)
    def test_raw_request_follows_next_page(self):
        code,out,_,_=self.invoke(['/api/v1/test','--json'],{'/api/v1/test':Response([{'id':1}],headers={'link':'<https://school.test/api/v1/test?page=2>; rel="next"'}),'/api/v1/test?page=2':Response([{'id':2}])})
        self.assertEqual(code,0);self.assertEqual(json.loads(out),[{'id':1},{'id':2}])
    def test_json_error_is_structured(self):
        code,out,_,_=self.invoke(['/api/v1/test','--json'],{'/api/v1/test':Response(status=200,text='<html>Login</html>')})
        self.assertNotEqual(code,0);self.assertEqual(json.loads(out)['errors'][0]['code'],'INVALID_RESPONSE')
    def test_no_input_does_not_read_browser_or_wait(self):
        self.state.unlink()
        with patch.object(c,'import_canvas_state_from_browser_session',side_effect=AssertionError('browser import')),patch.object(c,'open_canvas_in_chrome',side_effect=AssertionError('browser open')):
            code,out,_,_=self.invoke(['courses','--agent'],{})
        self.assertEqual(code,3);self.assertEqual(json.loads(out)['errors'][0]['code'],'AUTH_REQUIRED')
    def test_capabilities_requires_no_config_or_sign_in(self):
        self.state.unlink();c.BASE_URL=''
        code,out,_,_=self.invoke(['capabilities','--json'],{})
        self.assertEqual(code,0);self.assertIn('read',json.loads(out)['commands'])
    def test_full_assignment_json_keeps_id_description_and_submission(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42,'course_code':'PHY'}]),'/api/v1/courses/42/assignments?include[]=submission&per_page=100':Response([{'id':7,'name':'Task','description':'<p>Full text</p>','submission':{'score':8},'due_at':None}])}
        code,out,_,_=self.invoke(['assignments','42','--json'],routes)
        self.assertEqual(code,0);data=json.loads(out)[0];self.assertEqual(data['id'],7);self.assertEqual(data['description'],'<p>Full text</p>');self.assertEqual(data['submission']['score'],8)
    def test_agent_partial_results_are_not_reported_complete(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42,'course_code':'PHY'},{'id':43,'course_code':'MATH'}]),'/api/v1/courses/42/assignments?include[]=submission&per_page=100':Response([{'id':7,'name':'Task'}]),'/api/v1/courses/43/assignments?include[]=submission&per_page=100':Response({},403)}
        code,out,_,_=self.invoke(['assignments','--agent'],routes)
        self.assertEqual(code,4);data=json.loads(out);self.assertFalse(data['complete']);self.assertEqual(data['data'][0]['id'],7);self.assertEqual(data['errors'][0]['code'],'ACCESS_DENIED')
    def test_cookie_key_search_does_not_stop_at_unencrypted_cookie(self):
        plain={'name':'other','domain':'school.test','path':'/'}; auth={'name':'canvas_session','domain':'school.test','path':'/'}
        with patch.object(c,'read_canvas_cookies_from_chrome_db',side_effect=lambda path,key=None:[plain,auth] if key else [plain]): state=c.storage_state_from_cookie_dbs([self.root/'db'],[b'key'])
        self.assertIn('canvas_session',[x['name'] for x in state['cookies']])
    def test_weighted_fallback_is_not_labeled_as_actual_total(self):
        rows=c.grade_rows([{'group_weight':100,'assignments':[{'name':'Task','points_possible':10,'submission':{'score':8}}]},{'group_weight':0,'assignments':[{'name':'Practice','points_possible':100,'submission':{'score':0}}]}],course={'apply_assignment_group_weights':True})
        self.assertFalse(any(row['name']=='Total' for row in rows))


class BoundaryTests(AgentTests):
    def test_encoded_inbox_paths_also_disable_mark_as_read(self):
        from canvas_api import api_path
        for path in ['/api/v1/conversations/%37?auto_mark_as_read=true','/api/v1/conversations/7.json?auto_mark_as_read=true']:
            self.assertTrue(api_path(path,'https://school.test').endswith('auto_mark_as_read=false'))
    def test_partial_grades_keep_enrollment_and_group_structure(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42,'course_code':'PHY'}]),'/api/v1/users/self/enrollments?type[]=StudentEnrollment&state[]=active&per_page=100':Response([{'course_id':42,'grades':{'current_score':91}}]),'/api/v1/courses/42/assignment_groups?include[]=assignments&include[]=submission&per_page=100':Response([{'id':4,'name':'Homework','assignments':[]}],headers={'link':'<https://school.test/api/v1/groups-next>; rel="next"'}),'/api/v1/groups-next':Response({},403)}
        code,out,_,_=self.invoke(['grades','42','--agent'],routes)
        self.assertEqual(code,4);record=json.loads(out)['data'][0]
        self.assertEqual(record['enrollment']['grades']['current_score'],91);self.assertEqual(record['assignment_groups'][0]['id'],4)
    def test_invalid_option_is_json_in_agent_mode(self):
        code,out,_,_=self.invoke(['courses','--agent','--unknown-option'],{})
        self.assertEqual(code,2);self.assertEqual(json.loads(out)['errors'][0]['code'],'INVALID_USAGE')
    def test_401_in_agent_mode_never_opens_browser(self):
        with patch.object(c,'open_canvas_in_chrome',side_effect=AssertionError('browser opened')):
            code,out,_,_=self.invoke(['api','/api/v1/users/self/profile','--agent'],{'/api/v1/users/self/profile':Response({},401)})
        self.assertEqual(code,3);self.assertEqual(json.loads(out)['errors'][0]['code'],'AUTH_REQUIRED')
    def test_inbox_read_disables_mark_as_read(self):
        code,out,_,req=self.invoke(['inbox','7','--agent'],{'/api/v1/conversations/7?auto_mark_as_read=false':Response({'id':7,'messages':[{'body':'Full message'}]})})
        self.assertEqual(code,0);self.assertEqual(json.loads(out)['data']['messages'][0]['body'],'Full message')
    def test_all_courses_can_include_hidden_courses(self):
        (self.root/'config.json').write_text(json.dumps({'hidden_courses':['PHY']}))
        code,out,_,_=self.invoke(['courses','--all','--agent'],{'/api/v1/courses?include[]=sections&include[]=teachers&per_page=100':Response([{'id':42,'course_code':'PHY'}])})
        self.assertEqual(code,0);self.assertEqual(len(json.loads(out)['data']),1)
    def test_modules_json_contains_items(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42}]),'/api/v1/courses/42/modules?per_page=100':Response([{'id':4,'name':'Week'}]),'/api/v1/courses/42/modules/4/items?per_page=100':Response([{'id':5,'type':'Page','page_url':'guide'}])}
        code,out,_,_=self.invoke(['modules','42','--agent'],routes)
        self.assertEqual(code,0);self.assertEqual(json.loads(out)['data'][0]['items'][0]['page_url'],'guide')
    def test_auth_status_does_not_attempt_login(self):
        code,out,_,_=self.invoke(['auth','status','--agent'],{'/api/v1/users/self/profile':Response({'id':1})})
        self.assertEqual(code,0);self.assertTrue(json.loads(out)['data']['authenticated'])
    def test_calendar_batches_more_than_ten_courses(self):
        from urllib.parse import parse_qs, urlsplit
        courses=[{'id':index} for index in range(1,13)]
        calls=[]
        def respond(request, path, **kwargs):
            calls.append(path)
            if path.startswith('/api/v1/courses?'):return Response(courses)
            if path=='/api/v1/users/self/profile':return Response({'id':90})
            if path.startswith('/api/v1/calendar_events?'):
                codes=parse_qs(urlsplit(path).query)['context_codes[]']
                self.assertLessEqual(len(codes),10)
                return Response([{'id':value} for value in codes])
            raise AssertionError(path)
        with patch.object(Request,'get',respond):code,out,_,_=self.invoke(['calendar','--agent'],{})
        self.assertEqual(code,0);self.assertEqual(len(json.loads(out)['data']),13)

for name in AgentTests.__dict__:
    if name.startswith('test_'):setattr(BoundaryTests,name,None)

if __name__ == "__main__": unittest.main()
