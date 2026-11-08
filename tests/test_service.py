import pathlib
import plistlib
import tempfile
import unittest
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import service


class ServiceTests(unittest.TestCase):
    def test_independent_launch_config_preserves_project_and_tools(self):
        with tempfile.TemporaryDirectory(prefix='studio test ') as folder:
            root=pathlib.Path(folder).resolve()
            config=service.config(root,8790,[root/'source'],python='/usr/bin/python3',environment={'FFMPEG_BIN':'/tools/ffmpeg','UNRELATED_SECRET':'must-not-copy'})
            restored=plistlib.loads(plistlib.dumps(config))
            self.assertTrue(restored['RunAtLoad'])
            self.assertTrue(restored['KeepAlive'])
            self.assertIn(str(root),restored['ProgramArguments'])
            self.assertIn(str(root/'source'),restored['ProgramArguments'])
            self.assertEqual(restored['EnvironmentVariables']['FFMPEG_BIN'],'/tools/ffmpeg')
            self.assertNotIn('UNRELATED_SECRET',restored['EnvironmentVariables'])
            self.assertTrue(pathlib.Path(restored['StandardErrorPath']).is_relative_to(root))
            self.assertEqual(restored['Label'],service.config(root,8790,[])['Label'])
            self.assertNotEqual(restored['Label'],service.config(root/'other',8790,[])['Label'])

if __name__=='__main__':unittest.main()
