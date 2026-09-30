"""Make sure the Pod has a Python the installer supports, without touching the system Python.

Some templates ship Python 3.10 (or 3.13). The installer needs 3.11 or 3.12 for its virtualenvs
(PyTorch/SageAttention wheels are pinned to them). Replacing the system ``python3`` would break the
template's own tools, so a second interpreter is installed side by side:

1. reuse any acceptable Python already there (``python3``, ``python3.12``, ``python3.11``, or the one
   Historia installed earlier under ``<root>/python``);
2. otherwise install a standalone CPython with ``uv`` into ``<root>/python`` (on the persistent volume,
   so virtualenvs built from it survive a Pod restart; ``uv`` itself comes from PyPI, else astral.sh);
3. on Ubuntu only, as a last resort, the deadsnakes PPA (this lives on the container disk, so it is
   reported as a warning).

Nothing here runs at preview time: the preview only *reports* what would happen.
"""
import re
import shlex

WANT = '3.12'
ACCEPT = '3.11 3.12'
NO_PYTHON_EXIT = 45

# POSIX sh (dash on Ubuntu): $1=install root  $2=version to install  $3=space separated accepted minors.
SCRIPT = r'''
ROOT="$1"; WANT="${2:-3.12}"; ACCEPT="${3:-3.11 3.12}"
PYDIR="$ROOT/python"; MARK="$PYDIR/.historia-python"

minor() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null; }
full()  { "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null; }
accepted() {
  m=$(minor "$1") || return 1
  for a in $ACCEPT; do [ "$m" = "$a" ] && return 0; done
  return 1
}
# ready <interpreter> <source>: report and stop if the interpreter is usable.
ready() {
  p=$(command -v "$1" 2>/dev/null) || return 1
  accepted "$p" || return 1
  echo "PYTHON_READY=$p"; echo "PYTHON_SOURCE=$2"; echo "PYTHON_VERSION=$(full "$p")"
  echo "PYTHON_INSTALLED=${INSTALLED:-0}"
  exit 0
}
INSTALLED=0

for c in python3 python3.12 python3.11; do ready "$c" system; done
if [ -f "$MARK" ]; then ready "$(cat "$MARK")" managed; fi

echo "PYTHON_NEED=$WANT"
mkdir -p "$PYDIR" 2>/dev/null || { echo "Không tạo được thư mục $PYDIR để cài Python." >&2; exit 45; }
INSTALLED=1

# --- 1) standalone CPython through uv, inside the install root -------------------------------
uv_python() {
  UV_PYTHON_INSTALL_DIR="$PYDIR" UV_PYTHON_BIN_DIR="$PYDIR/bin" UV_PYTHON_PREFERENCE=only-managed \
    "$1" python install "$WANT" || return 1
  P=$(UV_PYTHON_INSTALL_DIR="$PYDIR" UV_PYTHON_PREFERENCE=only-managed "$1" python find "$WANT" 2>/dev/null)
  if [ -z "$P" ] || ! accepted "$P"; then
    P=""
    for d in "$PYDIR"/cpython-"$WANT".*/bin/python3; do [ -x "$d" ] && P="$d"; done
  fi
  [ -n "$P" ] && accepted "$P" || return 1
  echo "$P" > "$MARK" && ready "$P" managed
}
get_uv() {
  if [ "$1" = system ]; then
    command -v uv >/dev/null 2>&1 && UV=$(command -v uv) && return 0
    return 1
  fi
  for c in "$ROOT/uv-pkg/bin/uv" "$ROOT/uv/uv"; do [ -x "$c" ] && UV="$c" && return 0; done
  echo "Đang tải uv (trình cài Python độc lập) vào $ROOT/uv-pkg"
  for py in python3 python; do
    command -v "$py" >/dev/null 2>&1 || continue
    "$py" -m pip install --quiet --disable-pip-version-check --target "$ROOT/uv-pkg" uv >/dev/null 2>&1 \
      && [ -x "$ROOT/uv-pkg/bin/uv" ] && UV="$ROOT/uv-pkg/bin/uv" && return 0
  done
  mkdir -p "$ROOT/uv"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf --max-time 180 https://astral.sh/uv/install.sh 2>/dev/null \
      | env UV_UNMANAGED_INSTALL="$ROOT/uv" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- --timeout=180 https://astral.sh/uv/install.sh 2>/dev/null \
      | env UV_UNMANAGED_INSTALL="$ROOT/uv" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
  fi
  [ -x "$ROOT/uv/uv" ] && UV="$ROOT/uv/uv" && return 0
  return 1
}
# A uv already on the Pod first; if it is too old to install $WANT, fall back to a private copy.
for source in system private; do
  if get_uv "$source"; then
    echo "Đang cài Python $WANT cạnh Python hệ thống bằng uv (không thay python3 của Pod)."
    uv_python "$UV" || echo "uv ($source) không cài được Python $WANT." >&2
  fi
done

# --- 2) Ubuntu fallback: deadsnakes PPA (container disk: lost when the Pod is recreated) ----------
OSREL="${HISTORIA_OS_RELEASE:-/etc/os-release}"
if command -v apt-get >/dev/null 2>&1 && [ -r "$OSREL" ] && grep -qi '^ID=ubuntu' "$OSREL"; then
  echo "Thử cài python$WANT qua deadsnakes PPA."
  S=""; [ "$(id -u)" = 0 ] || S="sudo -n"
  export DEBIAN_FRONTEND=noninteractive
  $S apt-get update -y >/dev/null 2>&1
  $S apt-get install -y software-properties-common >/dev/null 2>&1
  if $S add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 \
     && $S apt-get update -y >/dev/null 2>&1 \
     && $S apt-get install -y "python$WANT" "python$WANT-venv" >/dev/null 2>&1; then
    echo "PYTHON_WARN=container-disk"
    ready "python$WANT" apt
  fi
fi

echo "Không cài được Python $WANT. Cần mạng tới pypi.org và github.com (uv), hoặc cài thủ công python$WANT rồi chạy lại." >&2
exit 45
'''

READY = re.compile(r'^PYTHON_READY=(/[A-Za-z0-9_./+@-]{1,300})$', re.MULTILINE)
FIELD = re.compile(r'^PYTHON_(SOURCE|VERSION|INSTALLED|WARN)=([A-Za-z0-9_.-]{1,40})$', re.MULTILINE)


def ensure_command(root: str, want: str = WANT, accept: str = ACCEPT) -> str:
    return ' '.join(['sh', '-c', shlex.quote(SCRIPT), 'sh', shlex.quote(root), shlex.quote(want), shlex.quote(accept)])


def parse_result(stdout: str) -> dict | None:
    """The interpreter the script settled on, or None if it printed nothing usable."""
    ready = READY.findall(stdout or '')
    if not ready:
        return None
    fields = {key.lower(): value for key, value in FIELD.findall(stdout)}
    return {'path': ready[-1], 'source': fields.get('source', 'system'), 'version': fields.get('version', ''),
            'installed': fields.get('installed') == '1', 'warning': fields.get('warn')}


def describe(found: dict, root: str) -> str:
    version = found['version'] or '?'
    if found['source'] == 'system':
        return f"Python {version} của Pod đáp ứng (3.11/3.12); dùng {found['path']}."
    if found['source'] == 'apt':
        return (f"Đã cài Python {version} bằng deadsnakes cạnh Python hệ thống ({found['path']}). "
                'Bản này nằm trên ổ container: nếu Pod bị tạo lại, chạy cài lại để dựng lại môi trường.')
    where = root.rstrip('/') + '/python'
    if found['installed']:
        return f"Đã cài Python {version} cạnh Python hệ thống trong {where} (python3 của Pod không bị thay đổi)."
    return f"Dùng Python {version} đã cài trước đó trong {where}."


async def ensure_python(command, root: str, log, want: str = WANT, accept: str = ACCEPT) -> str:
    """Run the script through the installer's locked ``command`` helper; return the interpreter path."""
    log('Kiểm tra phiên bản Python trên Pod (cần 3.11 hoặc 3.12).')
    result = await command(ensure_command(root, want, accept), 1500, stage='Chuẩn bị Python')
    found = parse_result(result.stdout)
    if not found:
        raise ValueError('Chuẩn bị Python: không xác định được bản Python 3.11/3.12 để dùng. Các tệp cũ được giữ nguyên.')
    log(describe(found, root))
    return found['path']
