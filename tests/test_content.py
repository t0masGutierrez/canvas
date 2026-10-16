import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_agent as support
from test_agent import Response

class ContentTests(support.AgentTests):
    def test_page_fallback_keeps_received_pages_and_avoids_duplicate_reads(self):
        routes = {
            '/api/v1/courses?enrollment_state=active&per_page=100': Response([{'id': 42}]),
            '/api/v1/courses/42/pages?per_page=100': Response(
                [{'page_id': 7, 'url': 'guide'}, {'page_id': 8, 'url': 'extra'}],
                headers={'link': '</api/v1/next-pages>; rel="next"'}),
            '/api/v1/next-pages': Response({}, 403),
            '/api/v1/courses/42/modules?per_page=100': Response([{'id': 4}]),
            '/api/v1/courses/42/modules/4/items?per_page=100': Response([
                {'type': 'Page', 'page_url': 'guide'},
                {'type': 'Page', 'page_url': 'new'}]),
            '/api/v1/courses/42/pages/guide': Response({'page_id': 7, 'url': 'guide'}),
            '/api/v1/courses/42/pages/new': Response({'page_id': 9, 'url': 'new'}),
        }
        code, out, _, req = self.invoke(['pages', '42', '--agent'], routes)
        result = json.loads(out)
        self.assertEqual(code, 4)
        self.assertFalse(result['complete'])
        self.assertEqual([page['page_id'] for page in result['data']], [7, 8, 9])
        self.assertNotIn('/api/v1/courses/42/pages/guide', [path for path, _ in req.calls])

    def test_duplicate_links_keep_order_and_distinct_labels(self):
        from canvas_content import html_content
        _, links = html_content(
            '<a href="/a">A</a><a href="/b">B</a><a href="/a">Again</a>'
            '<img src="/a" alt="First"><img src="/a" alt="Second">',
            'https://school.test')
        self.assertEqual(links, [
            {'url': 'https://school.test/a', 'type': 'a'},
            {'url': 'https://school.test/b', 'type': 'a'},
            {'url': 'https://school.test/a', 'type': 'img', 'label': 'First'},
            {'url': 'https://school.test/a', 'type': 'img', 'label': 'Second'},
        ])

    def test_read_page_keeps_html_text_and_links(self):
        code,out,_,_=self.invoke(['read','https://school.test/courses/42/pages/guide','--agent'],{'/api/v1/courses/42/pages/guide':Response({'page_id':7,'title':'Guide','body':'<h1>Read this</h1><p>A <a href="/courses/42/files/8">file</a></p><img alt="A diagram" src="/image.png">'})})
        self.assertEqual(code,0);page=json.loads(out)['data'];self.assertIn('<h1>',page['data']['body']);self.assertIn('Read this',page['text']);self.assertIn('A diagram',page['text']);self.assertEqual(page['links'][0]['url'],'https://school.test/courses/42/files/8')
    def test_read_assignment_includes_submission_feedback(self):
        routes={'/api/v1/courses/42/assignments/7?include[]=submission':Response({'id':7,'description':'Do this'}),'/api/v1/courses/42/assignments/7/submissions/self?include[]=submission_comments&include[]=rubric_assessment&include[]=submission_history':Response({'score':8,'submission_comments':[{'comment':'Feedback'}]})}
        code,out,_,_=self.invoke(['read','/courses/42/assignments/7','--agent'],routes)
        self.assertEqual(code,0);self.assertEqual(json.loads(out)['data']['submission']['submission_comments'][0]['comment'],'Feedback')
    def test_read_external_url_reports_external_access(self):
        code,out,_,_=self.invoke(['read','https://external.test/tool','--agent'],{})
        self.assertEqual(code,2);self.assertEqual(json.loads(out)['errors'][0]['code'],'EXTERNAL_RESOURCE')
    def test_module_pages_remain_available_when_page_list_is_disabled(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42,'course_code':'PHY'}]),'/api/v1/courses/42/pages?per_page=100':Response({},404),'/api/v1/courses/42/modules?per_page=100':Response([{'id':4,'name':'Week'}]),'/api/v1/courses/42/modules/4/items?per_page=100':Response([{'type':'Page','page_url':'guide','title':'Guide'}]),'/api/v1/courses/42/pages/guide':Response({'page_id':7,'title':'Guide','body':'Full text'})}
        code,out,_,_=self.invoke(['pages','42','--agent'],routes)
        self.assertEqual(code,4);result=json.loads(out);self.assertEqual(result['data'][0]['page_id'],7);self.assertFalse(result['complete']);self.assertEqual(len(result['errors']),1)
    def test_outline_reads_items_and_keeps_access_errors(self):
        routes={'/api/v1/courses/42?include[]=syllabus_body':Response({'id':42,'name':'Course','syllabus_body':'Read this'}),'/api/v1/courses/42/tabs?per_page=100':Response([{'id':'modules'}]),'/api/v1/courses/42/modules?per_page=100':Response([{'id':4,'name':'Week'}]),'/api/v1/courses/42/modules/4/items?per_page=100':Response([{'type':'Page','page_url':'guide','title':'Guide'}]),'/api/v1/courses/42/pages?per_page=100':Response({},404),'/api/v1/courses/42/files?per_page=100':Response([],200)}
        code,out,_,_=self.invoke(['course','42','outline','--agent'],routes)
        self.assertEqual(code,4);result=json.loads(out);self.assertEqual(result['data']['modules'][0]['items'][0]['page_url'],'guide');self.assertEqual(result['data']['pages'][0]['url'],'guide');self.assertFalse(result['complete'])
    def test_partial_module_lists_do_not_become_page_records(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42}]),'/api/v1/courses/42/pages?per_page=100':Response({},403),'/api/v1/courses/42/modules?per_page=100':Response([{'id':4,'name':'Week'}],headers={'link':'<https://school.test/api/v1/modules-next>; rel="next"'}),'/api/v1/modules-next':Response({},403),'/api/v1/courses/42/modules/4/items?per_page=100':Response([{'type':'Page','page_url':'guide'}]),'/api/v1/courses/42/pages/guide':Response({'page_id':7,'title':'Guide','url':'guide','body':'Read this'})}
        code,out,_,_=self.invoke(['pages','42','--agent'],routes)
        self.assertEqual(code,4);result=json.loads(out);self.assertEqual(result['data'][0]['page_id'],7);self.assertEqual(len(result['errors']),2)
    def test_syllabus_json_returns_body(self):
        routes={'/api/v1/courses?enrollment_state=active&per_page=100':Response([{'id':42,'course_code':'PHY'}]),'/api/v1/courses/42?include[]=syllabus_body':Response({'id':42,'syllabus_body':'<p>Reading</p>'})}
        code,out,_,_=self.invoke(['syllabus','42','--json'],routes)
        self.assertEqual(code,0);self.assertEqual(json.loads(out)['syllabus_body'],'<p>Reading</p>')

# The parent provides setup and invocation helpers; do not rerun its cases.
for name in support.AgentTests.__dict__:
    if name.startswith('test_'):setattr(ContentTests,name,None)


if __name__ == "__main__": unittest.main()
