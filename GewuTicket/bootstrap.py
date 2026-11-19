"""Offline App bootstrap (standalone-first, zero GewuTrack requirement)."""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, uuid
from pathlib import Path
if sys.platform != 'win32':
    import fcntl
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def _lock_ex_nb(fd):
    if sys.platform == 'win32':
        import ctypes, ctypes.wintypes as w, msvcrt
        class _OV(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t), ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        ov = _OV()
        if not k32.LockFileEx(w.HANDLE(msvcrt.get_osfhandle(fd)), 3, 0, 1, 0, ctypes.byref(ov)):
            e = ctypes.get_last_error()
            if e == 33:
                raise BlockingIOError(e, 'lock busy')
            raise OSError(e, ctypes.FormatError(e))
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

APP = Path(__file__).resolve().parent
ROOT = APP.parent
PROFILE = APP / 'contracts/bootstrap-profile.json'


def raw(v):
    return (json.dumps(v, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def need(v, m):
    if not v:
        raise ValueError(m)


def safe(p):
    p = Path(p)
    need(p.is_absolute() and '..' not in p.parts, 'canonical path required')
    for q in [*p.parents, p]:
        need(not q.is_symlink(), 'linked control path refused')
    return p


def read(p):
    safe(p)
    return p.read_bytes()


def version(p):
    return sha(read(p)) if p.exists() else 'absent'


def atomic(p, b):
    safe(p)
    fd, t = tempfile.mkstemp(dir=p.parent, prefix='.bootstrap-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
        os.replace(t, p)
    finally:
        if os.path.exists(t):
            os.unlink(t)


def is_standalone(cfg):
    return cfg.get('schema') == 'forge.bootstrap-profile/2' or cfg.get('track_integrated') is False


def package():
    cfg = json.loads(read(PROFILE))
    need(cfg.get('schema') in ('forge.bootstrap-profile/2', 'forge.bootstrap-profile/1'), 'profile schema')
    if is_standalone(cfg):
        return cfg, None
    p = safe(ROOT / cfg['track_path'])
    manifest = read(p / 'MANIFEST.json')
    need(sha(manifest) == cfg['track_manifest_sha256'], 'GewuTrack manifest drift')
    for row in json.loads(manifest)['files']:
        rel = Path(row['path'])
        need(not rel.is_absolute() and '..' not in rel.parts, 'manifest path')
        need(sha(read(p / rel)) == row['sha256'].removeprefix('sha256:'), 'GewuTrack file drift')
    return cfg, p


APP_ID = json.loads(read(PROFILE))['app_id']
need(type(APP_ID) is str and re.fullmatch('[a-z][a-z0-9-]{0,63}', APP_ID), 'invalid App identity')
def _open_lock_rw(path):
    safe(path)
    try:
        return os.open(path, os.O_RDWR | _O_NOFOLLOW)
    except FileNotFoundError:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | _O_NOFOLLOW, 0o600)
        except FileExistsError:
            return os.open(path, os.O_RDWR | _O_NOFOLLOW)


def _state_base():
    raw_home = os.environ.get('GEWU_STATE_HOME', '').strip()
    if not raw_home:
        return ROOT, False
    home = safe(Path(raw_home).resolve())
    return safe(home / ('ws-' + hashlib.sha256(str(ROOT).encode('utf-8')).hexdigest()[:16])), True


STATE_BASE, EXTERNAL_STATE = _state_base()
CONTROL = STATE_BASE / '.runtime' / APP_ID / 'bootstrap-v1'
CURRENT = CONTROL / 'current.json'


def venv_python(env):
    return Path(env) / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def resolve_state(ref):
    p = Path(ref)
    if p.is_absolute():
        res = safe(p)
        need(EXTERNAL_STATE and res.is_relative_to(STATE_BASE), 'state path outside GEWU_STATE_HOME')
        return res
    return safe(ROOT / p)


def environment_ok(binding):
    try:
        need(binding['root'] == str(ROOT), 'relocated')
        env = resolve_state(binding['environment'])
        p = venv_python(env)
        result = subprocess.run([str(p), '-I', '-B', '-c', 'import sys,json;print(json.dumps([sys.prefix,sys.base_prefix]))'], capture_output=True, text=True, timeout=10)
        prefix, base = json.loads(result.stdout)
        need(result.returncode == 0 and Path(prefix) == env and prefix != base, 'environment probe')
        need(sha(read(env / 'pyvenv.cfg')) == binding['pyvenv_cfg_sha256'], 'environment cfg drift')
        need(sha(p.resolve().read_bytes()) == binding['interpreter_sha256'], 'interpreter drift')
        if 'instances' in binding:
            need(resolve_state(binding['instances']).is_dir(), 'instance namespace missing')
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def _security_profile():
    p = APP / 'contracts/app-security-profile.json'
    if not p.is_file():
        return {}
    return json.loads(read(p))


def _compile_layer2(src_c, out_bin):
    src_c = safe(src_c)
    out_bin = safe(out_bin)
    need(src_c.is_file(), 'Layer-2 C source missing')
    cc = shutil.which('clang') or shutil.which('cc') or '/usr/bin/clang'
    need(cc and Path(cc).exists(), 'clang/cc compiler required for Layer-2 security tier')
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out_bin.parent, 0o700)
    tmp = out_bin.parent / f'.tmp-layer2-{os.getpid()}'
    try:
        subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(src_c), '-o', str(tmp)], check=True, capture_output=True, text=True)
        strip_bin = shutil.which('strip')
        if strip_bin:
            s_cmd = [strip_bin, '-x', str(tmp)] if sys.platform == 'darwin' else [strip_bin, str(tmp)]
            subprocess.run(s_cmd, check=False, capture_output=True, text=True)
        os.chmod(tmp, 0o700)
        os.replace(tmp, out_bin)
    finally:
        if tmp.exists():
            tmp.unlink()
    probe = subprocess.run([str(out_bin), '--self-test'], capture_output=True, text=True, timeout=10)
    need(probe.returncode == 0 and json.loads(probe.stdout).get('ok') is True, 'Layer-2 self-test failed')
    return sha(read(out_bin))


def _data_layout_profile():
    p = APP / 'contracts/data-layout-profile.json'
    if not p.is_file():
        return {}
    return json.loads(read(p))


def init_runtime_dirs():
    local = safe(APP / '.local')
    for sub in (local, local / 'state', local / 'logs', local / 'logs/human-only', local / 'bin', local / 'backups'):
        sub.mkdir(parents=True, exist_ok=True)
        os.chmod(sub, 0o700)
    data_layout = _data_layout_profile()
    data_dirs_created = []
    if data_layout.get('separation') == 'embedded':
        for rel in data_layout.get('data_dirs', []):
            dp = safe(APP / rel)
            dp.mkdir(parents=True, exist_ok=True)
            os.chmod(dp, 0o750)
            data_dirs_created.append(rel)
    out = {'effect': 'runtime-initialized', 'state_mode': '0700', 'logs_mode': '0700', 'backups_mode': '0700', 'data_dirs_initialized': data_dirs_created, 'warning_readme': 'omitted-by-rule'}
    sec = _security_profile()
    if sec.get('security_tier') in ('binary-hybrid', 'binary-direct'):
        wb = safe(APP / sec.get('whitebox_script', 'scripts/app_core/layer2/whitebox_exec.py'))
        need(wb.is_file() and sha(read(wb)) == sec.get('whitebox_sha256'), 'whitebox SHA-256 mismatch')
        bin_path = local / 'bin/gewu_vault_exec'
        bin_sha = _compile_layer2(APP / 'scripts/app_core/layer2/gewu_vault_exec.c', bin_path)
        out.update({'security_tier': sec['security_tier'], 'layer2_binary': str(bin_path.relative_to(APP)), 'layer2_binary_sha256': bin_sha})
    return out


def deploy_runtime(target_dir):
    """Migrate hidden app_core and .local runtime out of workspace (plaintext-py §2.3)."""
    core_src = safe(APP / 'scripts/app_core')
    need(core_src.is_dir(), 'scripts/app_core missing')
    dest = Path(target_dir).expanduser().resolve() if target_dir else (Path.home() / '.local/share' / APP_ID / 'runtime')
    safe(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    core_dst = dest / 'app_core'
    if core_dst.exists():
        shutil.rmtree(core_dst)
    shutil.copytree(core_src, core_dst)
    for sub in ('state', 'logs', 'logs/human-only', 'bin', 'backups', 'data'):
        d = dest / sub
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    data_layout = _data_layout_profile()
    if data_layout.get('separation', 'separated') == 'separated':
        for rel in data_layout.get('data_dirs', []):
            dp = safe(dest / 'data' / rel)
            dp.mkdir(parents=True, exist_ok=True)
            os.chmod(dp, 0o700)
    atomic(dest / '.app_id', (APP_ID + '\n').encode('utf-8'))
    os.chmod(dest / '.app_id', 0o600)
    sec_src = APP / 'contracts/app-security-profile.json'
    layer2_info = {}
    if sec_src.is_file():
        sec_bytes = read(sec_src)
        atomic(dest / 'app-security-profile.json', sec_bytes)
        os.chmod(dest / 'app-security-profile.json', 0o600)
        sec = json.loads(sec_bytes)
        if sec.get('security_tier') in ('binary-hybrid', 'binary-direct'):
            bin_sha = _compile_layer2(core_dst / 'layer2/gewu_vault_exec.c', dest / 'bin/gewu_vault_exec')
            layer2_info = {'security_tier': sec['security_tier'], 'layer2_binary_sha256': bin_sha}
    local_state = APP / '.local/state'
    if local_state.is_dir():
        for item in local_state.iterdir():
            if item.is_file() and not item.is_symlink():
                dst_file = dest / 'state' / item.name
                if not dst_file.exists():
                    shutil.copy2(item, dst_file)
                    os.chmod(dst_file, 0o600)
    loc_file = safe(STATE_BASE / '.runtime' / APP_ID / 'runtime-location.json')
    loc_file.parent.mkdir(parents=True, exist_ok=True)
    atomic(loc_file, raw({'schema': 'forge.app-runtime-location/1', 'app_id': APP_ID, 'runtime_dir': str(dest), 'core_dir': str(core_dst)}))
    return {'effect': 'runtime-deployed-outside-workspace', 'app_id': APP_ID, 'location_record': str(loc_file) if EXTERNAL_STATE else str(loc_file.relative_to(ROOT)), **layer2_info}


def inspect():
    cfg, p = package()
    standalone = p is None
    binding = json.loads(read(CURRENT)) if CURRENT.exists() else None
    loc_file = STATE_BASE / '.runtime' / APP_ID / 'runtime-location.json'
    out = {
        'schema': 'forge.bootstrap-inspection/2' if cfg.get('schema') == 'forge.bootstrap-profile/2' else 'forge.bootstrap-inspection/1',
        'app_id': APP_ID,
        'root': str(ROOT),
        'profile_digest': sha(read(PROFILE)),
        'current_digest': version(CURRENT),
        'ready': bool(binding and environment_ok(binding)),
        'binding': binding,
        'track_integrated': not standalone,
        'runtime_mode': 'out-of-workspace' if loc_file.exists() else 'in-workspace-hidden-core',
        'entry_registration': 'not modified by bootstrap',
    }
    if not standalone:
        out['track_version'] = cfg['track_version']
    return out


def plan(mode, idle):
    info = inspect()
    need(mode in ('initialize', 'reset'), 'mode')
    return {
        'schema': 'forge.bootstrap-plan/1',
        'operation_id': uuid.uuid4().hex,
        'root': str(ROOT),
        'mode': mode,
        'idle_confirmed': idle,
        'profile_digest': info['profile_digest'],
        'expected_current': info['current_digest'],
        'effect': 'unchanged' if mode == 'initialize' and info['ready'] else 'prepare-new-environment-and-task-space',
        'source_binding': info['binding'],
    }


def check_idle(previous, confirmed):
    if not previous or previous.get('root') != str(ROOT) or 'instances' not in previous:
        return
    tasks = resolve_state(previous['instances'])
    records = list(tasks.glob('*/state/*/task.json'))
    if records:
        need(confirmed, 'existing tasks require explicit cooperative idle confirmation; nothing reset')
    for path in records:
        record = json.loads(read(path))
        need(not record.get('operation'), 'unfinished GewuTrack operation blocks reset')
        need(not any(r.get('status') in ('dispatching', 'unknown') for r in record.get('action_requests', [])), 'unresolved business request blocks reset')


def apply(p, authorization):
    cfg, pkg = package()
    need(p['schema'] == 'forge.bootstrap-plan/1' and p['root'] == str(ROOT), 'plan root/schema')
    need(re.fullmatch('[a-f0-9]{32}', p['operation_id']), 'invalid operation id')
    need(authorization.strip(), 'authorization source required')
    safe(CONTROL).mkdir(parents=True, exist_ok=True)
    lock = safe(CONTROL / 'lock')
    fd = _open_lock_rw(lock)
    try:
        _lock_ex_nb(fd)
        op = safe(CONTROL / 'operations' / p['operation_id'])
        need(not op.is_symlink(), 'linked operation')
        if op.exists():
            need(read(op / 'plan.json') == raw(p), 'operation identity conflict')
            if (op / 'result.json').exists():
                return json.loads(read(op / 'result.json'))
            return {'effect': 'unknown', 'operation_id': p['operation_id'], 'next': 'inspect retained operation and current binding; do not restart'}
        need(sha(read(PROFILE)) == p['profile_digest'] and version(CURRENT) == p['expected_current'], 'stale plan')
        current = json.loads(read(CURRENT)) if CURRENT.exists() else None
        need(current == p['source_binding'], 'source binding changed')
        need(p['effect'] == ('unchanged' if p['mode'] == 'initialize' and current and environment_ok(current) else 'prepare-new-environment-and-task-space'), 'effect mismatch')
        if p['effect'] != 'unchanged':
            check_idle(current, p['idle_confirmed'])
        op.mkdir(parents=True)
        atomic(op / 'plan.json', raw(p))
        atomic(op / 'authorization.json', raw({'source': authorization}))
        if p['effect'] == 'unchanged':
            result = {'effect': 'unchanged', 'operation_id': p['operation_id'], 'binding': current}
        else:
            env = safe(STATE_BASE / '.runtime' / APP_ID / 'environments' / p['operation_id'])
            env.parent.mkdir(parents=True, exist_ok=True)
            need(not env.exists(), 'environment target occupied')
            subprocess.run([sys.executable, '-I', '-B', '-m', 'venv', '--without-pip', str(env)], check=True, capture_output=True)
            env_ref = str(env) if EXTERNAL_STATE else str(env.relative_to(ROOT))
            binding = {
                'schema': 'forge.bootstrap-current/2' if cfg.get('schema') == 'forge.bootstrap-profile/2' else 'forge.bootstrap-current/1',
                'app_id': APP_ID,
                'root': str(ROOT),
                'environment': env_ref,
                'generation_id': p['operation_id'],
                'track_integrated': pkg is not None,
                'pyvenv_cfg_sha256': sha(read(env / 'pyvenv.cfg')),
                'interpreter_sha256': sha(venv_python(env).resolve().read_bytes()),
            }
            if pkg is not None:
                generation = safe(STATE_BASE / '.runtime/instances' / APP_ID / p['operation_id'])
                generation.mkdir(parents=True, exist_ok=False)
                binding['instances'] = str(generation) if EXTERNAL_STATE else str(generation.relative_to(ROOT))
                binding['track_manifest_sha256'] = cfg['track_manifest_sha256']
            need(environment_ok(binding), 'new environment not ready')
            need(version(CURRENT) == p['expected_current'] and sha(read(PROFILE)) == p['profile_digest'], 'pre-switch drift')
            atomic(CURRENT, raw(binding))
            init_runtime_dirs()
            result = {'effect': 'applied', 'operation_id': p['operation_id'], 'binding': binding, 'retained_old_data': True, 'entry_registration': 'not modified'}
        atomic(op / 'result.json', raw(result))
        return result
    finally:
        os.close(fd)


def task(name, goal, authorization):
    cfg, pkg = package()
    need(pkg is not None, 'new-task is disabled in standalone mode (track_integrated=false)')
    need(re.fullmatch('[a-z][a-z0-9-]{0,63}', name), 'invalid task name')
    need(goal.strip() and authorization.strip(), 'goal and authorization required')
    cfg, pkg = package()
    safe(CONTROL)
    fd = os.open(safe(CONTROL / 'lock'), os.O_RDWR | _O_NOFOLLOW)
    try:
        _lock_ex_nb(fd)
        info = inspect()
        need(info['ready'], 'initialize environment first')
        b = info['binding']
        instance = safe(resolve_state(b['instances']) / name)
        instance.mkdir(exist_ok=False)
        work = instance / 'workspace'
        work.mkdir()
        (instance / 'logs').mkdir()
        spec = {'goal': goal, 'approval_source': authorization, 'workspace': str(work), 'files': [], 'protected_paths': [], 'isolation': 'cooperative', 'checks': []}
        atomic(instance / 'spec.json', raw(spec))
        cmd = [str(venv_python(resolve_state(b['environment']))), '-I', '-B', str(pkg / 'track.py'), 'create', '--state-root', str(instance / 'state'), '--task', name, '--spec', str(instance / 'spec.json')]
        result = subprocess.run(cmd, capture_output=True, text=True)
        atomic(instance / 'logs/create.json', raw({'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}))
        need(result.returncode == 0, 'GewuTrack task creation failed; instance retained, do not repeat')
        return {'task': str(instance / 'state' / name / 'TASK.md'), 'instance': str(instance), 'track': json.loads(result.stdout)}
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('inspect')
    q = sub.add_parser('plan')
    q.add_argument('--mode', choices=['initialize', 'reset'], default='initialize')
    q.add_argument('--idle-confirmed', action='store_true')
    q = sub.add_parser('apply')
    q.add_argument('--plan', type=Path, required=True)
    q.add_argument('--authorization-note', required=True)
    q = sub.add_parser('receipt')
    q.add_argument('--operation-id', required=True)
    q = sub.add_parser('new-task')
    q.add_argument('--name', required=True)
    q.add_argument('--goal', required=True)
    q.add_argument('--authorization-note', required=True)
    sub.add_parser('init-runtime')
    q = sub.add_parser('deploy-runtime')
    q.add_argument('--target', default='')
    a = p.parse_args()
    try:
        if a.cmd == 'inspect':
            r = inspect()
        elif a.cmd == 'plan':
            r = plan(a.mode, a.idle_confirmed)
        elif a.cmd == 'apply':
            r = apply(json.loads(read(a.plan.resolve())), a.authorization_note)
        elif a.cmd == 'new-task':
            r = task(a.name, a.goal, a.authorization_note)
        elif a.cmd == 'init-runtime':
            r = init_runtime_dirs()
        elif a.cmd == 'deploy-runtime':
            r = deploy_runtime(a.target)
        else:
            need(re.fullmatch('[a-f0-9]{32}', a.operation_id), 'operation id')
            op = CONTROL / 'operations' / a.operation_id
            r = json.loads(read(op / 'result.json')) if (op / 'result.json').exists() else {'effect': 'unknown' if op.exists() else 'not_found', 'retry_authorized': False}
        print(json.dumps(r, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'effect': 'unverified', 'reason': 'configuration, precondition or execution failure; retain files and inspect original operation', 'retry_authorized': False}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
