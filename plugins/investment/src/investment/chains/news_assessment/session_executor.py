"""SC03脚本拥有的Codex子session：并发执行、真实输出和不可变审计。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

from jsonschema import Draft202012Validator, ValidationError

from investment.contracts import ContractError, canonical, now, require
from investment.research.host import create_host_request
from investment.runtime.runs import atomic
from .host_adapter import open_host_work, record_host_result


COMPLETION_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['status', 'reason'], 'properties': {
        'status': {'type': 'string', 'enum': ['completed', 'failed']},
        'reason': {'type': 'string'}}}


def _prepare(ctx, step_id, references, work_key):
    references = list({ref['uri']: ref for ref in references}.values())
    request = create_host_request(ctx, step_id, references, work_key=work_key)
    prefix = 'host-' + step_id + '-' + work_key
    request_path = ctx.attempt_dir / (prefix + '-request.json')
    _, reader = open_host_work(ctx.root, request_path, mode=ctx.mode)
    directory = ctx.attempt_dir / ('session-' + step_id + '-' + work_key)
    directory.mkdir()
    packet = {'request': request,
        'input': reader.resolve(request['input_ref']),
        'method': reader.resolve(request['method_ref']),
        'artifacts': [{'reference': ref, 'value': reader.resolve(ref)} for ref in references],
        'output_schema_bundle': reader.resolve(request['output_schema_ref']),
        'schema_resources': reader.schemas.documents,
        'python_executable': str(Path(sys.executable).resolve())}
    packet_path = directory / 'packet.json'
    atomic(packet_path, canonical(packet), immutable=True)
    atomic(directory / 'completion.schema.json', canonical(COMPLETION_SCHEMA), immutable=True)
    bash = shutil.which('bash')
    require(bash is not None, 'RS05', '/executor', 'Bash is required for the Codex session', 'execution_failed')
    # 正式模板是唯一指令；追加的对象只绑定本轮程序生成的文件位置。
    prompt = reader.resolve(request['prompt_ref']) + '\n' + canonical({
        'packet_path': packet_path.as_posix(), 'result_path': (directory / 'result.json').as_posix(),
        'bash_executable': bash}).decode('utf-8')
    atomic(directory / 'input.txt', prompt.encode('utf-8'), immutable=True)
    return request, request_path, prefix, directory, bash, prompt


async def _execute(ctx, step_id, references, work_key):
    request, request_path, prefix, directory, bash, prompt = _prepare(ctx, step_id, references, work_key)
    arguments = ['codex', '-a', 'never', 'exec',
        '--model', ctx.method['batch_policy']['session_model'],
        '--config', 'model_reasoning_effort=' + ctx.method['batch_policy']['session_reasoning_effort'],
        '--config', 'model_provider="sc03_no_retry"',
        '--config', 'model_providers.sc03_no_retry={name="SC03 OpenAI single pass",requires_openai_auth=true,request_max_retries=0,stream_max_retries=0}',
        '--ephemeral', '--json', '--skip-git-repo-check', '--sandbox', 'workspace-write',
        '--cd', str(directory), '--output-schema', str(directory / 'completion.schema.json'),
        '--output-last-message', str(directory / 'completion.json'), '-']
    # Windows原生命令行与Bash的引号规则不同；参数只在Bash脚本内解析一次。
    script = directory / 'run.sh'
    atomic(script, ('exec ' + shlex.join(arguments) + '\n').encode('utf-8'), immutable=True)
    command = [bash, '--noprofile', '--norc', str(script)]
    atomic(directory / 'command.json', canonical(command), immutable=True)
    started, error, process = now(), None, None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (directory / 'stdout.jsonl').open('xb') as stdout, (directory / 'stderr.txt').open('xb') as stderr:
            process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                stdin=asyncio.subprocess.PIPE, stdout=stdout, stderr=stderr, creationflags=flags,
                env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'))
            await process.communicate(prompt.encode('utf-8'))
        atomic(directory / 'exit.txt', str(process.returncode).encode('ascii'), immutable=True)
        if process.returncode != 0:
            reason = ((directory / 'stderr.txt').read_text(encoding='utf-8', errors='replace')[-600:] + '\n' +
                      (directory / 'stdout.jsonl').read_text(encoding='utf-8', errors='replace')[-1000:]).strip()
            error = {'code': 'model_execution_failed', 'reason': reason or 'Codex exited with status ' + str(process.returncode)}
        else:
            completion = json.loads((directory / 'completion.json').read_text(encoding='utf-8'))
            Draft202012Validator(COMPLETION_SCHEMA).validate(completion)
            if completion['status'] == 'failed':
                error = {'code': 'model_execution_failed', 'reason': completion['reason'] or 'Codex reported an incomplete task'}
    except asyncio.CancelledError as exc:
        if process is not None and process.returncode is None:
            if os.name == 'nt':
                killer = await asyncio.create_subprocess_exec('taskkill.exe', '/PID', str(process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await killer.wait()
            else:
                process.kill()
            await process.wait()
        error = {'code': 'cancelled',
                 'reason': str(exc) or 'Another source session failed; this session was cancelled'}
    except (OSError, ValueError) as exc:
        error = {'code': 'model_execution_failed', 'reason': str(exc)[:2048] or type(exc).__name__}
    except ValidationError as exc:
        # 外部完成消息必须如实保留为模型输出错误，不能回填默认成功。
        error = {'code': 'invalid_model_output', 'reason': str(exc)[:2048] or type(exc).__name__}
    finished = now()
    result_path = directory / 'result.json'
    if result_path.exists():
        atomic(ctx.attempt_dir / (prefix + '-raw.txt'), result_path.read_bytes(), immutable=True)
    elif error is None:
        error = {'code': 'invalid_model_output', 'reason': 'Codex completed without its declared result.json'}
    contexts = []
    log = directory / 'stdout.jsonl'
    if log.exists():
        with log.open(encoding='utf-8', errors='replace') as stream:
            for text in stream:
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started' and event.get('thread_id'):
                    contexts.append(event['thread_id'])
    _, response = record_host_result(ctx.root, request_path, mode=ctx.mode,
        started_at=started, finished_at=finished,
        context_id=contexts[0] if len(contexts) == 1 else None, tool_call_id=None, error=error)
    # 仅开放本role实际产生且已经核过绑定的响应，供后续同role整合使用。
    ctx.resolver.allow(response['event_ref'])
    for field in ('raw_response_ref', 'output_ref'):
        if response[field]['status'] == 'known':
            ctx.resolver.allow(response[field]['value'])
    return ctx.host_response(step_id, work_key=work_key)


def execute_sessions(ctx, jobs):
    """按冻结上限并发；首个失败取消其余任务，实际执行记录全部收尾。"""
    async def run():
        semaphore = asyncio.Semaphore(ctx.method['batch_policy']['max_concurrent_sessions'])
        failure = None

        async def execute(job):
            nonlocal failure
            async with semaphore:
                if failure is not None:
                    raise failure
                try:
                    return await _execute(ctx, *job)
                except Exception as exc:
                    failure = exc
                    raise

        tasks = [asyncio.create_task(execute(job)) for job in jobs]
        try:
            return await asyncio.gather(*tasks)
        except Exception as exc:
            for task in tasks:
                if not task.done():
                    task.cancel('Concurrent source failed: ' + str(exc)[:1024])
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    return asyncio.run(run())
