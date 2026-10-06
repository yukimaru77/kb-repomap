import argparse, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
import kb_api, kb_decrypt

class RRConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.config=self.root/'rr.json'; self.key=self.root/'client.key'; self.key.write_text('test-key\n')
    def write_config(self, **extra):
        self.config.write_text(json.dumps({'base_url':'https://rr.invalid/v1','key_file':'client.key',**extra}))
        return {'KB_RR_CONFIG':str(self.config)}
    def test_json_and_explicit_fields(self):
        self.assertEqual(kb_api.rr_configuration(self.write_config()),('https://rr.invalid/v1','test-key'))
        self.assertEqual(kb_api.rr_configuration({'KB_RR_BASE_URL':'http://127.0.0.1:18473/_pool/rr','KB_RR_KEY_FILE':str(self.key)})[0],'http://127.0.0.1:18473/_pool/rr')
    def test_missing_config_and_invalid_shape_fail(self):
        with self.assertRaises(ValueError): kb_api.rr_configuration({})
        for payload in ([], {'base_url':1}, {'key_file':[]}, {'private_http':'false'}):
            self.config.write_text(json.dumps(payload))
            with self.assertRaises(ValueError): kb_api.rr_configuration({'KB_RR_CONFIG':str(self.config)})
    def test_private_http_validation(self):
        env=self.write_config(base_url='http://private.invalid:18473/rr',private_http=True)
        self.assertEqual(kb_api.rr_configuration(env)[0],'http://private.invalid:18473/rr')
        with self.assertRaisesRegex(ValueError,'remote HTTP'): kb_api.rr_configuration({**env,'KB_RR_PRIVATE_HTTP':'0'})
    def test_decrypt_namespace_uses_current_names(self):
        self.write_config(); args=argparse.Namespace(rr_config=str(self.config),rr_base_url=None,rr_key_file=None,rr_private_http=False)
        with mock.patch.dict(os.environ,{},clear=True):
            kb_decrypt.apply_rr({},args); self.assertEqual(kb_api.rr_configuration()[0],'https://rr.invalid/v1')
if __name__=='__main__': unittest.main()
