#!/usr/bin/env python3
"""Rewrite portal.py install_singbox() for multi-mirror + faster failover."""
from __future__ import annotations

import re
from pathlib import Path

NEW_FUNC = r'''
def install_singbox():
    """从 SagerNet/sing-box 官方 Release 安装 sing-box，返回 (ok, message)。

    sing-box 的 Release 没有 checksums 文件，预期摘要取 GitHub API 给每个
    资产记录的 sha256 digest（只信 api.github.com，镜像只作传输候选）。

    2026-11-02：多镜像 + 较短单源超时 + API 重试，避免面板长时间卡在
    「正在下载并校验」（仅 github.com / 单次 180s 时在部分 VPS 上几乎必挂）。
    """
    current = _singbox_version()
    if current and current >= ANYTLS_MIN_SINGBOX:
        return True, 'sing-box %s 已安装' % '.'.join(map(str, current))
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
            'armv7l': 'armv7', 'armv7': 'armv7'}.get(machine)
    if not arch:
        return False, '不支持的 CPU 架构: %s' % machine

    release = None
    api_errors = []
    api_url = 'https://api.github.com/repos/SagerNet/sing-box/releases/latest'
    for attempt in range(3):
        try:
            req = urllib.request.Request(api_url, headers={'User-Agent': 'hy2-portal'})
            with urllib.request.urlopen(req, timeout=20) as r:
                release = json.loads(r.read().decode('utf-8'))
            break
        except Exception as exc:
            api_errors.append('try%s: %s' % (attempt + 1, exc))
            time.sleep(1.5 * (attempt + 1))
    if release is None:
        return False, '取不到 sing-box 最新版本信息（已重试）—— ' + ' ｜ '.join(api_errors)

    tag = str(release.get('tag_name') or '')
    ver = tag.lstrip('v')
    if not ver:
        return False, 'GitHub 返回的 tag_name 为空，无法安装 sing-box'
    asset_name = f'sing-box-{ver}-linux-{arch}.tar.gz'
    asset = next((a for a in release.get('assets') or [] if a.get('name') == asset_name), None)
    digest = str((asset or {}).get('digest') or '')
    expected = digest[len('sha256:'):] if digest.startswith('sha256:') else ''
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        return False, '取不到 %s 的官方 SHA256，无法校验下载内容，已放弃安装' % asset_name

    official = f'https://github.com/SagerNet/sing-box/releases/download/{tag}/{asset_name}'
    # 多源轮询：单源超时缩短，总等待可控；镜像顺序与 Xray 安装保持一致并多一个备用。
    dl_urls = [
        official,
        'https://ghfast.top/' + official,
        'https://github.moeyy.xyz/' + official,
        'https://gh-proxy.com/' + official,
        'https://mirror.ghproxy.com/' + official,
    ]
    errors = []
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = Path(tmpdir) / 'sing-box.tar.gz'
        for url in dl_urls:
            host = url.split('/')[2] if '//' in url else url
            try:
                if archive.exists():
                    archive.unlink()
                req = urllib.request.Request(url, headers={'User-Agent': 'hy2-portal'})
                # 单源 60s：失败立刻换源，避免 UI 卡死数分钟
                with urllib.request.urlopen(req, timeout=60) as resp, open(archive, 'wb') as out:
                    shutil.copyfileobj(resp, out)
                if not archive.exists() or archive.stat().st_size < 1024:
                    errors.append('%s: 下载文件过小或为空' % host)
                    continue
                if not verify_sha256(archive, expected):
                    errors.append('%s: SHA256 与官方不一致' % host)
                    continue
                with tarfile.open(str(archive), 'r:gz') as tar:
                    member_name = safe_tar_members([m.name for m in tar.getmembers() if m.isfile()],
                                                   'sing-box')
                    member = tar.getmember(member_name)
                    staged = Path(ANYTLS_BINARY + '.new')
                    with tar.extractfile(member) as src, open(staged, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                os.chmod(str(staged), 0o755)
                version = _singbox_version(str(staged))
                if not version or version < ANYTLS_MIN_SINGBOX:
                    staged.unlink(missing_ok=True)
                    return False, 'sing-box 版本 %s 不支持 AnyTLS（需要 1.12.0+）' % ver
                os.replace(str(staged), ANYTLS_BINARY)
                return True, 'sing-box %s 安装完成（来源 %s）' % (ver, host)
            except Exception as exc:
                errors.append('%s: %s: %s' % (host, type(exc).__name__, exc))
    return False, '下载 sing-box 失败 —— ' + ' ｜ '.join(errors)
'''


def main() -> None:
    path = Path('portal.py')
    text = path.read_text(encoding='utf-8')
    if '多镜像 + 较短单源超时' in text and 'github.moeyy.xyz' in text:
        print('Already patched')
        return
    start = text.find('def install_singbox():')
    if start < 0:
        raise SystemExit('install_singbox not found')
    m = re.search(r'\ndef [a-zA-Z_]', text[start + 1:])
    if not m:
        raise SystemExit('cannot find end of install_singbox')
    end = start + 1 + m.start()
    text = text[:start] + NEW_FUNC.lstrip('\n') + text[end:]
    if not re.search(r'(?m)^import time\b', text):
        if 'import json\n' in text:
            text = text.replace('import json\n', 'import json\nimport time\n', 1)
        else:
            text = 'import time\n' + text
    path.write_text(text, encoding='utf-8')
    out = path.read_text(encoding='utf-8')
    assert 'github.moeyy.xyz' in out
    assert 'timeout=60' in out
    assert 'for attempt in range(3):' in out
    print('Patched portal.py install_singbox OK')


if __name__ == '__main__':
    main()
