"""Opt-in CLI run -> verified private Markdown proposals, never canonical writes."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from capture_outcomes import CaptureError, MAX_BYTES, load_config, privacy_check, stage_report, verify_profile
from migrate import digest, json_bytes, private_root, write_new
from selected_memory import checked_path

SCHEMA = Path(__file__).resolve().parents[1] / 'examples/capture-output.schema.json'
REPORT_INSTRUCTIONS = '''\n\nReturn the required structured capture report as the final response. Include only meaningful
outcomes, decisions, lessons or milestones from this session. At most eight items; omit
routine chatter. Do not invent success or dates. For an exact statement from the user's
request, basis may be user-stated only when text and source_quote both equal the exact
quoted request text. Otherwise use assistant-reported, or inferred for interpretations.
Use an empty source_quote for those latter bases. A completed command is not proof of
product acceptance. Return no items if there is nothing meaningful to propose.
'''


def safe_directory(root, path):
    if not path.is_relative_to(root):
        raise CaptureError('OUTPUT_OUTSIDE_PRIVATE_WORKSPACE')
    for part in [path, *path.parents]:
        if part.exists() or part.is_symlink():
            if (part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                    or getattr(part.lstat(), 'st_reparse_tag', 0) & 0x20000000):
                raise CaptureError('REDIRECTED_OUTPUT')
    path.mkdir(parents=True, exist_ok=True)
    checked_path(path, directory=True)


@contextmanager
def capture_lock(root):
    directory = root / 'staging/codex-capture'
    safe_directory(root, directory)
    path = directory / '.capture-lock'
    token = str(uuid.uuid4()).encode()
    try:
        with path.open('xb') as output:
            output.write(token)
    except FileExistsError:
        raise CaptureError('CAPTURE_BUSY_OR_STALE_LOCK') from None
    try:
        yield
    finally:
        # Do not remove another process's lock or silently recover a crash lock.
        if checked_path(path).read_bytes() != token:
            raise CaptureError('CAPTURE_LOCK_CHANGED')
        path.unlink()


def save_job(run_dir, job):
    # Mutable execution receipt only, never a candidate or canonical note.
    path = run_dir / 'job.json'
    if path.exists():
        checked_path(path)
    pending = run_dir / ('job.' + str(uuid.uuid4()) + '.tmp')
    write_new(pending, json_bytes(job))
    os.replace(pending, path)


def invoke_codex(command, request, cwd, run_dir, timeout):
    with (run_dir / 'events.jsonl').open('xb') as out, (run_dir / 'stderr.txt').open('xb') as err:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        try:
            child.communicate(request.encode('utf-8'), timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                child.kill()
            child.wait(timeout=10)
            raise CaptureError('CODEX_TIMEOUT_OR_INTERRUPTED') from None
        return child.returncode


def finish(config_path, config, root, config_sha, run_dir, job):
    current, current_root, current_sha = load_config(config_path)
    if not current['enabled']:
        return {'status': 'DISABLED_AFTER_RUN', 'proposals': 0}
    if current_root != root or current_sha != config_sha:
        raise CaptureError('CONFIG_CHANGED_DURING_RUN')
    verify_profile(current)
    events = checked_path(run_dir / 'events.jsonl')
    final = checked_path(run_dir / 'final.json')
    if events.stat().st_size > MAX_BYTES or final.stat().st_size > 16000:
        raise CaptureError('OVERSIZED_OUTPUT')
    raw, final_raw = events.read_bytes(), final.read_bytes()
    prompt_raw = checked_path(run_dir / 'prompt.txt').read_bytes()
    if digest(prompt_raw) != job['prompt_sha256']:
        raise CaptureError('PROMPT_CHANGED')
    if job.get('events_sha256') and (digest(raw) != job['events_sha256']
                                   or digest(final_raw) != job['final_sha256']):
        raise CaptureError('RAW_SOURCE_CHANGED')
    job.update(events_sha256=digest(raw), final_sha256=digest(final_raw))
    return stage_report(config, root, raw, final_raw, prompt_raw.decode('utf-8'), job['codex_exit_code'])


def run_capture(config_path, cwd, prompt_path, timeout=300, launcher=invoke_codex):
    config, root, config_sha = load_config(config_path)
    if not config['enabled']:
        return {'status': 'DISABLED', 'proposals': 0}
    verify_profile(config)
    cwd = checked_path(cwd, directory=True).resolve()
    if cwd not in [Path(value).resolve() for value in config['allowed_cwds']]:
        raise CaptureError('CWD_NOT_AUTHORIZED')
    prompt_file = checked_path(prompt_path).resolve()
    if not prompt_file.is_relative_to(root) or prompt_file.stat().st_size > 32000:
        raise CaptureError('PROMPT_NOT_IN_PRIVATE_WORKSPACE_OR_TOO_LARGE')
    prompt = prompt_file.read_text(encoding='utf-8')
    if not prompt.strip():
        raise CaptureError('EMPTY_PROMPT')
    privacy_check(prompt)
    if not 1 <= timeout <= 600:
        raise CaptureError('INVALID_TIMEOUT')
    executable = shutil.which('codex.cmd' if os.name == 'nt' else 'codex')
    if not executable:
        raise CaptureError('CODEX_NOT_FOUND')
    with capture_lock(root):
        run_dir = root / 'staging/codex-capture/runs' / str(uuid.uuid4())
        safe_directory(root, run_dir)
        write_new(run_dir / 'prompt.txt', prompt.encode('utf-8'))
        job = {'state': 'RUNNING', 'config_sha256': config_sha, 'cwd': str(cwd),
               'prompt_sha256': digest(prompt.encode('utf-8')), 'proposals': 0}
        save_job(run_dir, job)
        command = [executable, 'exec', '--profile', config['profile'], '--ephemeral',
                   '--skip-git-repo-check', '--cd', str(cwd), '--json', '--color', 'never',
                   '--output-schema', str(SCHEMA), '--output-last-message', str(run_dir / 'final.json'), '-']
        try:
            job['codex_exit_code'] = launcher(command, prompt + REPORT_INSTRUCTIONS, cwd, run_dir, timeout)
            if job['codex_exit_code'] != 0:
                raise CaptureError('CODEX_PROCESS_FAILED')
            result = finish(config_path, config, root, config_sha, run_dir, job)
        except Exception as error:
            code = str(error) if isinstance(error, CaptureError) else 'CAPTURE_IO_OR_FORMAT_FAILURE'
            result = {'status': 'FAILED', 'error_code': code, 'proposals': 0}
            job['error_code'] = code
        job.update(state=result['status'], proposals=result['proposals'], result=result)
        save_job(run_dir, job)
        return {**result, 'run_directory': str(run_dir)}


def replay_capture(config_path, run_directory):
    config, root, config_sha = load_config(config_path)
    if not config['enabled']:
        return {'status': 'DISABLED', 'proposals': 0}
    run_dir = checked_path(run_directory, directory=True).resolve()
    if run_dir.parent != root / 'staging/codex-capture/runs':
        raise CaptureError('RUN_DIRECTORY_NOT_AUTHORIZED')
    with capture_lock(root):
        job = json.loads(checked_path(run_dir / 'job.json').read_bytes())
        if (job.get('config_sha256') != config_sha
                or Path(job.get('cwd', '')).resolve() not in [Path(value).resolve() for value in config['allowed_cwds']]):
            raise CaptureError('REPLAY_CONFIG_OR_CWD_MISMATCH')
        if job.get('codex_exit_code') != 0:
            raise CaptureError('FAILED_RUN_NOT_REPLAYABLE')
        result = finish(config_path, config, root, config_sha, run_dir, job)
        save_job(run_dir, {**job, 'last_replay_status': result['status'], 'last_replay_result': result})
        return {**result, 'run_directory': str(run_dir)}


def set_enabled(config_path, enabled):
    config, root, _ = load_config(config_path)
    config['enabled'] = enabled
    # Only the explicit owner-selected private config is changed, not Codex globals.
    path = checked_path(config_path)
    pending = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    write_new(pending, json_bytes(config))
    checked_path(path)
    os.replace(pending, path)
    return {'status': 'ENABLED' if enabled else 'DISABLED', 'proposals': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    run = commands.add_parser('run')
    run.add_argument('--cwd', required=True)
    run.add_argument('--prompt-file', required=True)
    run.add_argument('--timeout', type=int, default=300)
    replay = commands.add_parser('replay')
    replay.add_argument('--run-directory', required=True)
    commands.add_parser('enable')
    commands.add_parser('disable')
    args = parser.parse_args()
    try:
        if args.operation == 'run':
            result = run_capture(args.config, args.cwd, args.prompt_file, args.timeout)
        elif args.operation == 'replay':
            result = replay_capture(args.config, args.run_directory)
        else:
            result = set_enabled(args.config, args.operation == 'enable')
        print(json.dumps(result, indent=2))
        return 1 if result['status'] == 'FAILED' else 0
    except CaptureError as error:
        print(json.dumps({'status': 'REFUSED', 'error_code': str(error), 'proposals': 0}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError):
        # No source text or credential-bearing exception string is emitted.
        print(json.dumps({'status': 'REFUSED', 'proposals': 0}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
