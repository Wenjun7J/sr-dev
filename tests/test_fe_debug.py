"""Check the actual JVM-option transformations used by sr-deploy."""
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / 'files/sr-deploy').read_text()
START = SOURCE.index('append_java_opt(){')
FUNCTIONS = SOURCE[START:SOURCE.index('\nif [ "$WANT_BE" = 1 ]; then', START)]


class FeDebugTest(unittest.TestCase):
    def options(self, text, debug='1'):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp) / 'fe.conf'
            conf.write_text(text)
            script = 'FE_DEBUG_PORT=28008\n' + FUNCTIONS + '\nfe_java_opts_lines "$1" 2g "$2"\n'
            result = subprocess.run(['bash', '-c', script, '_', str(conf), debug],
                                    capture_output=True, text=True, check=True)
            return result.stdout

    def test_debug_enabled_once_and_old_jdk_options(self):
        source = '''JAVA_OPTS="-Xmx8g -Dfoo=bar -agentlib:jdwp=transport=dt_socket,address=*:5005"
JAVA_OPTS_FOR_JDK_11='-Xmx4g -Xrunjdwp:transport=dt_socket,address=5006'
# >>> sr-deploy managed
JAVA_OPTS="-Xmx99g -agentlib:jdwp=address=9999"
# <<< sr-deploy managed
'''
        output = self.options(source)
        self.assertEqual(output.count('-agentlib:jdwp='), 2)
        self.assertEqual(output.count('server=y,suspend=n,address=127.0.0.1:28008'), 2)
        self.assertNotIn('5005', output)
        self.assertNotIn('9999', output)
        self.assertNotIn('-Xrunjdwp', output)
        self.assertIn('-Dfoo=bar', output)
        self.assertEqual(self.options(output), output)
        # Both quoting forms must remain valid configuration assignments.
        subprocess.run(['bash', '-n'], input=output, text=True, check=True)

    def test_be_options_unchanged(self):
        source = 'JAVA_OPTS="-Xmx8g -agentlib:jdwp=transport=dt_socket,address=*:5005 -Dfoo=bar"\n'
        be = self.options(source, '')
        self.assertIn('address=*:5005', be)
        self.assertNotIn('28008', be)

    def test_missing_opts_get_defaults(self):
        self.assertIn('-Xmx2g -agentlib:jdwp=', self.options(''))
        self.assertEqual(self.options('', '0'), 'JAVA_OPTS="-Xmx2g"\n')


if __name__ == '__main__':
    unittest.main()
