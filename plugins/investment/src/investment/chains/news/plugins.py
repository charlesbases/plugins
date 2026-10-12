"""按受信目录发现独立来源脚本，未选择的来源不会被导入。"""
from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

from investment.contracts import canonical, digest, require
from investment.chains.news.protocol import Collector, FrozenSourceConfig


class PluginLoadError(Exception):
    """已安装来源不能执行；不能伪装成一次 HTTP 失败。"""

    def __init__(self, source_id: str, reason: str):
        super().__init__(reason)
        self.source_id = source_id


def discover(plugins_dir: Path) -> dict[str, Path]:
    """目录即注册表；完整移除的来源不会存在，局部损坏保留为加载错误。"""
    root = Path(plugins_dir).resolve()
    if not root.is_dir():
        return {}
    return {path.name: path for path in sorted(root.iterdir()) if path.is_dir()}


def load(source_id: str, directory: Path, frozen: FrozenSourceConfig, schemas) -> Collector:
    """核对冻结身份与当前安装字节，再导入所选目录自己的异步 collect。"""
    try:
        manifest = json.loads(frozen.manifest_json)
        profile = json.loads(frozen.profile_json)
        schemas.validate('investment.collector_config', {
            'source_id': source_id, 'manifest_ref': frozen.manifest_ref, 'profile_ref': frozen.profile_ref,
            'manifest': manifest, 'profile': profile})
        require(manifest['source_id'] == profile['source_id'] == frozen.source_id == source_id,
                'N03', '/source_id', 'Installed source identity mismatch', 'identity_mismatch')
        require((directory / 'manifest.json').read_bytes() == frozen.manifest_json
                and (directory / 'extraction.json').read_bytes() == frozen.profile_json
                and manifest['profile_sha256'] == digest(frozen.profile_json),
                'N03', '/profile_ref', 'Installed source differs from frozen bytes', 'identity_mismatch')
        script = (directory / manifest['module']).resolve()
        require(script.parent == directory.resolve() and script.is_file(),
                'N03', '/module', 'Installed collector script is unavailable', 'missing_evidence')
        name = '_investment_source_' + source_id + '_' + digest(canonical(str(script)))[:24]
        specification = importlib.util.spec_from_file_location(name, script)
        if specification is None or specification.loader is None:
            raise ValueError('Collector module cannot be loaded')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        collector = getattr(module, manifest['callable'], None)
        if not inspect.iscoroutinefunction(collector):
            raise ValueError('Collector callable must be async')
        return collector
    except Exception as error:
        raise PluginLoadError(source_id, f'{type(error).__name__}: {error}') from error
