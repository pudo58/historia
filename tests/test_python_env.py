"""The Pod's Python: reuse a suitable one, otherwise install one beside it (never touch the system Python)."""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from ghm.executors.base import CommandResult
from ghm.schemas import HostOptions
from studio.install_checks import CACHE_HELPERS, INVENTORY, python_notes, summarize
from studio.installer import install_failure
from studio.python_env import NO_PYTHON_EXIT, SCRIPT, ensure_command, ensure_python, parse_result

posix = pytest.mark.skipif(sys.platform == 'win32' or not shutil.which('sh'), reason='needs a POSIX shell')

FAKE_PYTHON = """#!/bin/sh
case "$*" in
  *'%d.%d.%d'*) echo {full} ;;
  *'%d.%d'*) echo {minor} ;;
esac
"""


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def sandbox(tmp_path, *, tools=('mkdir', 'cat', 'grep', 'id', 'env', 'chmod', 'cp')):
    """A PATH holding only basic utilities, so the test decides which Pythons/uv/apt exist."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for tool in tools:
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def run_script(tmp_path, bin_dir, *, want='3.99', accept='3.99', env=None):
    root = tmp_path / 'historia'
    root.mkdir(exist_ok=True)
    result = subprocess.run(['/bin/sh', '-c', SCRIPT, 'sh', str(root), want, accept], capture_output=True, text=True,
                            env={'PATH': str(bin_dir), 'HOME': str(tmp_path / 'home'), **(env or {})}, timeout=60)
    return root, result


def fake_uv(bin_dir, log, *, version='3.99.1', find=True, fail=False):
    """A uv that 'installs' a fake interpreter into $UV_PYTHON_INSTALL_DIR."""
    minor = version.rsplit('.', 1)[0]
    python_body = FAKE_PYTHON.format(full=version, minor=minor)
    return executable(bin_dir / 'uv', f"""#!/bin/sh
echo "$@ | dir=$UV_PYTHON_INSTALL_DIR bin=$UV_PYTHON_BIN_DIR" >> {log}
[ {'1' if fail else '0'} = 1 ] && exit 3
d="$UV_PYTHON_INSTALL_DIR/cpython-{minor}.7-linux-x86_64-gnu/bin"
case "$2" in
  install) mkdir -p "$d" && cat > "$d/python3" <<'PY'
{python_body}PY
    chmod +x "$d/python3" ;;
  find) {'echo "$UV_PYTHON_INSTALL_DIR/cpython-' + minor + '.7-linux-x86_64-gnu/bin/python3"' if find else 'exit 1'} ;;
esac
""")


def fields(stdout):
    return dict(line.split('=', 1) for line in stdout.splitlines() if line.startswith('PYTHON_'))


@posix
def test_suitable_system_python_is_used_and_nothing_is_installed(tmp_path):
    bin_dir = sandbox(tmp_path)
    executable(bin_dir / 'python3', FAKE_PYTHON.format(full='3.12.3', minor='3.12'))
    root, result = run_script(tmp_path, bin_dir, want='3.12', accept='3.11 3.12')
    assert result.returncode == 0, result.stderr
    found = fields(result.stdout)
    assert found['PYTHON_READY'] == str(bin_dir / 'python3') and found['PYTHON_SOURCE'] == 'system'
    assert found['PYTHON_INSTALLED'] == '0' and found['PYTHON_VERSION'] == '3.12.3'
    assert list(root.iterdir()) == []


@posix
def test_python_310_gets_a_second_python_beside_it_and_it_is_reused(tmp_path):
    bin_dir = sandbox(tmp_path)
    executable(bin_dir / 'python3', FAKE_PYTHON.format(full='3.10.12', minor='3.10'))   # the Pod's own
    calls = tmp_path / 'uv.log'
    fake_uv(bin_dir, calls)
    root, result = run_script(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    found = fields(result.stdout)
    assert found['PYTHON_SOURCE'] == 'managed' and found['PYTHON_INSTALLED'] == '1'
    assert found['PYTHON_READY'].startswith(str(root / 'python')) and found['PYTHON_VERSION'] == '3.99.1'
    # Installed into the install root, on the persistent volume, nothing outside it.
    line = calls.read_text().splitlines()[0]
    assert f'dir={root}/python' in line and f'bin={root}/python/bin' in line
    assert (root / 'python' / '.historia-python').read_text().strip() == found['PYTHON_READY']
    # The system python3 is untouched.
    assert (bin_dir / 'python3').read_text().count('3.10.12') == 1
    # A second run reuses it without calling uv again.
    calls.unlink()
    _, again = run_script(tmp_path, bin_dir)
    assert again.returncode == 0 and fields(again.stdout)['PYTHON_INSTALLED'] == '0'
    assert fields(again.stdout)['PYTHON_SOURCE'] == 'managed' and not calls.exists()


@posix
def test_interpreter_is_found_without_uv_find(tmp_path):
    bin_dir = sandbox(tmp_path)
    fake_uv(bin_dir, tmp_path / 'uv.log', find=False)
    root, result = run_script(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    assert fields(result.stdout)['PYTHON_READY'] == str(root / 'python' / 'cpython-3.99.7-linux-x86_64-gnu' / 'bin' / 'python3')


@posix
def test_failure_exits_45_with_an_actionable_message(tmp_path):
    bin_dir = sandbox(tmp_path)
    fake_uv(bin_dir, tmp_path / 'uv.log', fail=True)
    _, result = run_script(tmp_path, bin_dir)
    assert result.returncode == NO_PYTHON_EXIT == 45
    assert 'PYTHON_READY' not in result.stdout and 'Không cài được Python 3.99' in result.stderr
    assert 'pypi.org' in install_failure(CommandResult(45, result.stdout, result.stderr))


@posix
def test_uv_is_fetched_privately_when_the_pod_has_none(tmp_path):
    bin_dir = sandbox(tmp_path)
    # A python3 whose 'pip install --target' drops a working uv into the target directory.
    executable(bin_dir / 'python3', """#!/bin/sh
case "$*" in
  *'%d.%d.%d'*) echo 3.10.12 ;;
  *'%d.%d'*) echo 3.10 ;;
  *'-m pip install'*) target=""; while [ $# -gt 0 ]; do [ "$1" = --target ] && target="$2"; shift; done
     mkdir -p "$target/bin" && cp "$UV_TEMPLATE" "$target/bin/uv" && chmod +x "$target/bin/uv" ;;
esac
""")
    template = fake_uv(tmp_path / 'template', tmp_path / 'uv.log')
    root, result = run_script(tmp_path, bin_dir, env={'UV_TEMPLATE': str(template)})
    assert result.returncode == 0, result.stderr
    assert (root / 'uv-pkg' / 'bin' / 'uv').exists() and fields(result.stdout)['PYTHON_SOURCE'] == 'managed'


@posix
def test_ubuntu_fallback_uses_deadsnakes_when_uv_is_unavailable(tmp_path):
    bin_dir = sandbox(tmp_path)
    log = tmp_path / 'apt.log'
    executable(bin_dir / 'add-apt-repository', f'#!/bin/sh\necho "add-apt-repository $*" >> {log}\nexit 0\n')
    executable(bin_dir / 'apt-get', f'''#!/bin/sh
echo "apt-get $*" >> {log}
case "$*" in
  *python3.99-venv*) cp {tmp_path}/py99 {bin_dir}/python3.99 ;;
esac
exit 0
''')
    executable(tmp_path / 'py99', FAKE_PYTHON.format(full='3.99.2', minor='3.99'))
    (tmp_path / 'os-release').write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
    executable(bin_dir / 'sudo', '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
    root, result = run_script(tmp_path, bin_dir, env={'HISTORIA_OS_RELEASE': str(tmp_path / 'os-release')})
    assert result.returncode == 0, result.stderr
    found = fields(result.stdout)
    assert found['PYTHON_SOURCE'] == 'apt' and found['PYTHON_WARN'] == 'container-disk'
    text = log.read_text()
    assert 'add-apt-repository -y ppa:deadsnakes/ppa' in text and 'install -y python3.99 python3.99-venv' in text
    # Not Ubuntu: no apt at all.
    (tmp_path / 'os-release').write_text('ID=debian\n')
    (bin_dir / 'python3.99').unlink()
    log.unlink()
    _, other = run_script(tmp_path, bin_dir, env={'HISTORIA_OS_RELEASE': str(tmp_path / 'os-release')})
    assert other.returncode == 45 and not log.exists()


def test_parse_result_reads_the_last_ready_line_only():
    out = 'noise\nPYTHON_READY=/a/b/python3.12\nPYTHON_SOURCE=managed\nPYTHON_VERSION=3.12.5\nPYTHON_INSTALLED=1\n'
    assert parse_result(out) == {'path': '/a/b/python3.12', 'source': 'managed', 'version': '3.12.5',
                                 'installed': True, 'warning': None}
    assert parse_result('PYTHON_READY=relative/python\n') is None and parse_result('') is None
    assert parse_result('PYTHON_READY=/x; rm -rf /\n') is None


def test_ensure_command_quotes_every_argument():
    command = ensure_command("/work space/it's", '3.12', '3.11 3.12')
    assert command.startswith('sh -c ') and "'\"'\"'" in command
    assert command.endswith("'3.11 3.12'")


def test_ensure_python_logs_what_happened_and_rejects_no_answer():
    async def go():
        logs = []

        async def command(value, timeout, stage=''):
            assert stage == 'Chuẩn bị Python' and value.startswith('sh -c ')
            return CommandResult(0, 'PYTHON_READY=/w/python/x/bin/python3\nPYTHON_SOURCE=managed\n'
                                    'PYTHON_VERSION=3.12.5\nPYTHON_INSTALLED=1\n', '')

        assert await ensure_python(command, '/w', logs.append) == '/w/python/x/bin/python3'
        assert any('cạnh Python hệ thống' in line and '/w/python' in line for line in logs)

        async def silent(value, timeout, stage=''):
            return CommandResult(0, 'nothing useful', '')

        with pytest.raises(ValueError, match='Chuẩn bị Python'):
            await ensure_python(silent, '/w', logs.append)
    asyncio.run(go())


def report(**change):
    return {'python': [3, 12, 3], 'pythons': {}, **change}


def test_unsuitable_python_is_a_note_not_a_blocker():
    options = HostOptions(root='/workspace/historia')
    assert python_notes(report(), options) == []
    assert python_notes(report(python=[3, 11, 9]), options) == []
    for version in ([3, 10, 12], [3, 13, 1], [3, 9, 18]):
        (note,) = python_notes(report(python=version), options)
        assert f'{version[0]}.{version[1]}.{version[2]}' in note and '/workspace/historia/python' in note
        assert 'tự tải Python 3.12' in note and 'không bị thay đổi' in note
    (usable,) = python_notes(report(python=[3, 10, 12], pythons={'python3.12': '3.12.3'}), options)
    assert 'Python 3.12.3 có sẵn (python3.12)' in usable
    (own,) = python_notes(report(python=[3, 10, 12], pythons={'historia': '3.12.5'}), options)
    assert 'đã cài trước đó bởi Historia' in own


def test_summary_carries_python_notes_but_no_blocker():
    base = {'files': [], 'free_bytes': 200 * 1024**3, 'filesystem_path': '/workspace', 'writable': True,
            'comfy_exists': False, 'managed': False, 'root_nonempty': False, 'port_busy': False,
            'python': [3, 10, 12], 'architecture': 'x86_64', 'tools': {'flock': True}}
    summary = summarize(base, HostOptions())
    assert summary['blockers'] == [] and len(summary['notes']) == 1
    assert summarize({**base, 'python': [3, 12, 1]}, HostOptions())['notes'] == []


@posix
def test_inventory_reports_available_pythons_and_stays_readable_for_old_pythons(tmp_path):
    root = tmp_path / 'historia'
    (root / 'python').mkdir(parents=True)
    own = executable(tmp_path / 'own' / 'python3', FAKE_PYTHON.format(full='3.12.4', minor='3.12'))
    (root / 'python' / '.historia-python').write_text(str(own))
    payload = json.dumps({'root': str(root), 'cache': '.cache.json', 'comfy': str(root / 'ComfyUI'), 'port': 59999, 'files': []})
    out = subprocess.run([sys.executable, '-c', INVENTORY.replace('__CACHE__', CACHE_HELPERS)], input=payload,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data['pythons']['historia'] == '3.12.4' and data['python'][0] == 3
    assert 'is_relative_to' not in INVENTORY   # Python 3.8 has no Path.is_relative_to
    assert os.access(root, os.W_OK)
