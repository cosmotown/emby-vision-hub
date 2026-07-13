import ast
import unittest
from pathlib import Path


class MediaImageProxySecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source_path = Path(__file__).resolve().parents[1] / 'routes' / 'media.py'
        cls.source = source_path.read_text(encoding='utf-8')
        tree = ast.parse(cls.source)
        cls.proxy_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'proxy_emby_image'
        )
        cls.proxy_source = ast.unparse(cls.proxy_function)

    def test_api_key_is_passed_in_header_not_embedded_in_url(self):
        self.assertNotIn('target_url_with_key', self.proxy_source)
        self.assertIn('params=query_params', self.proxy_source)
        self.assertIn("headers={'X-Emby-Token': emby_api_key}", self.proxy_source)
        self.assertNotIn("query_params.append(('api_key', emby_api_key))", self.proxy_source)

    def test_proxy_logs_never_reference_api_key_or_query_params(self):
        for node in ast.walk(self.proxy_function):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != 'logger':
                continue
            rendered = ast.unparse(node)
            self.assertNotIn('emby_api_key', rendered)
            self.assertNotIn('query_params', rendered)
            self.assertNotIn('exc_info=True', rendered)

    def test_unavailable_upstream_images_return_404_for_frontend_fallback(self):
        self.assertIn('emby_response.status_code in (404, 500)', self.proxy_source)
        self.assertGreaterEqual(self.proxy_source.count('Response(status=404'), 3)


if __name__ == '__main__':
    unittest.main()
