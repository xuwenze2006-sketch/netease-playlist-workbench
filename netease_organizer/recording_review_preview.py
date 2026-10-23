"""Static, escaped presentation of a source-bound recording review.

The HTML has no scripts or account actions. It represents a local snapshot,
not a statement that the source report or online playlists were changed.
"""

from html import escape
from urllib.parse import urlsplit


def _text(value):
    return escape(str(value), quote=True)


def _link(url, title):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or any(ord(c) < 32 for c in url)):
        return _text(title)
    return f'<a href="{_text(url)}" target="_blank" rel="noopener noreferrer">{_text(title)}</a>'


def render_preview(preview):
    """Render an already validated build_preview result with no active content."""
    summary = preview['summary']
    status_names = {'unresolved': '录音身份待核对', 'metadata_matched': '目录资料匹配，尚未完成试听',
                    'audio_verified': '已记录音频核验依据'}
    decisions = {'suitable': '适合', 'unsuitable': '不适合', 'uncertain': '未核验 / 证据不足'}
    cards = []
    for row in preview['records']:
        recording = row['recording']
        sources = ''.join(
            f'<li>{_link(item["url"], item["title"])}'
            f'<p>{_text(item["note"])}</p></li>' for item in recording['evidence_sources'])
        segments = ''.join(
            f'<li>{_text(item["start_seconds"])}–{_text(item["end_seconds"])} 秒：'
            f'{_text(item["note"])}</li>' for item in recording['listened_segments'])
        scene_rows = ''.join(
            '<tr><th scope="row">' + _text(item['scene']) + '</th><td>'
            + decisions[item['decision']] + '</td><td>' + _text(item['reason']) + '</td></tr>'
            for item in row['scene_decisions'])
        before = '、'.join(row['before_scenes']) or '无场景标签'
        after = '、'.join(row['after_scenes']) or '无场景标签'
        added = '、'.join(row['added_scenes']) or '无'
        removed = '、'.join(row['removed_scenes']) or '无'
        cards.append(f'''<article aria-labelledby="song-{row['position']}">
<h2 id="song-{row['position']}">{row['position']}. {_text(row['name'])}</h2>
<p class="artist">{_text(row['artists'])}</p>
<p class="status">{status_names[recording['status']]}</p>
<p>版本：{_text(recording['version']) or '未确认'}</p>
<p>{_text(recording['note']) or '尚未完成试听；目录资料不能确认场景适配。'}</p>
<dl><dt>原场景</dt><dd>{_text(before)}</dd><dt>预览场景</dt><dd>{_text(after)}</dd>
<dt>预计加入</dt><dd>{_text(added)}</dd><dt>预计移出</dt><dd>{_text(removed)}</dd></dl>
<table><caption>逐场景核验结论</caption><thead><tr><th>场景</th><th>结论</th><th>依据</th></tr></thead>
<tbody>{scene_rows or '<tr><td colspan="3">未提交场景判断，保持原分类。</td></tr>'}</tbody></table>
<details><summary>核验来源与试听范围</summary><h3>来源</h3>
<ul>{sources or '<li>没有记录外部来源。</li>'}</ul><h3>实际试听范围</h3>
<ul>{segments or '<li>无试听记录；尚未完成试听。</li>'}</ul></details></article>''')
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>具体录音核验与场景预览</title>
<style>
:root{{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#1c2833;background:#f3f5f7;line-height:1.65}}
body{{margin:0}} main{{max-width:960px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:1.65rem}} h2{{font-size:1.2rem;margin-bottom:4px}} h3{{font-size:1rem}}
article,header{{background:white;border:1px solid #d7dfe7;border-radius:12px;padding:22px;margin-bottom:18px;overflow-wrap:anywhere}}
p{{white-space:pre-wrap;margin:8px 0}} .artist{{color:#4c6070}} .notice{{background:#fff6dd;padding:12px;border-left:4px solid #a77713}}
.status{{font-weight:600}} .metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}} .metrics span{{background:#edf2f7;padding:8px 12px;border-radius:8px}}
code{{word-break:break-all}} dl{{display:grid;grid-template-columns:6em 1fr;gap:6px}} dt{{color:#526474}} dd{{margin:0}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}} caption{{text-align:left;font-weight:600;margin:8px 0}}
th,td{{padding:10px 8px;border-bottom:1px solid #dce3e9;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}}
th:first-child{{width:20%}} th:nth-child(2){{width:24%}} details{{margin-top:16px}} summary{{cursor:pointer;padding:8px 0}}
a{{color:#17608c}} :focus-visible{{outline:3px solid #196db4;outline-offset:3px}} li{{overflow-wrap:anywhere}}
@media(max-width:480px){{main{{padding:14px 10px 35px}} article,header{{padding:15px}} th,td{{padding:8px 4px;font-size:.9rem}} h1{{font-size:1.4rem}}}}
</style></head><body><main><header>
<h1>具体录音核验与场景预览</h1>
<p class="notice">这是本地核验快照，不是账号执行计划。未核验场景保持原成员；来源资料匹配不等于音频版本或听感已确认。</p>
<div class="metrics"><span>样本 {summary['reviewed_records']} 首</span>
<span>目录匹配 {summary['metadata_matched_records']} 首</span><span>音频核验 {summary['audio_verified_records']} 首</span>
<span>所列场景均已核验 {summary['scene_verified_records']} 首</span><span>仍有未核验项 {summary['uncertain_records']} 首</span>
<span>预计加入 {summary['added_count']} 项 / 移出 {summary['removed_count']} 项</span></div>
<p>数量表示本次样本记录，不代表整库准确率。部分明确结论只影响对应场景，其他场景保留。</p>
<p>来源 SHA256：<code>{_text(preview['source_sha256'])}</code></p>
<p>此页面不会自动更新。来源或核验结果变化后，请重新读取并生成新的预览。</p>
</header>{''.join(cards)}</main></body></html>'''
