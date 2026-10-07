import { useId, useState } from 'react';
import type { LocalPreview } from './types';
import { Icon } from './icons';

function previewDate(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.valueOf())
    ? date
        .toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        })
        .replaceAll('/', '-')
    : '更新时间未记录';
}

function ArtistCandidate({ artist }: { artist: LocalPreview['artists'][number] }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  return (
    <article className="preview-artist">
      <button
        className="preview-artist-toggle"
        aria-label={`查看${artist.name}的候选曲目`}
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded(!expanded)}
      >
        <span className="preview-artist-icon">
          <Icon name="music" size={18} />
        </span>
        <span className="preview-artist-label">
          <strong>{artist.name}</strong>
          <small>红心候选</small>
        </span>
        <span className="preview-artist-count">{artist.count} 首</span>
        <span className={`preview-chevron ${expanded ? 'expanded' : ''}`}>
          <Icon name="arrow" size={16} />
        </span>
      </button>
      {expanded && (
        <div id={contentId} className="preview-tracks">
          {artist.tracks.length ? (
            <ol>
              {artist.tracks.map((track, index) => (
                <li key={`${track.name}-${index}`}>
                  <span className="preview-track-number">{index + 1}</span>
                  <span>
                    <strong>{track.name}</strong>
                    <small>{track.artists.join(' / ') || '歌手未记录'}</small>
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="preview-empty-note">本地记录没有保存候选曲目明细。</p>
          )}
          {artist.tracks.length > 0 && artist.tracks.length < artist.count && (
            <p className="preview-empty-note">
              本地记录显示 {artist.tracks.length} / {artist.count} 首候选。
            </p>
          )}
        </div>
      )}
    </article>
  );
}

export function PreviewPanel({
  preview,
  artistsCompleted,
}: {
  preview?: LocalPreview | null;
  artistsCompleted: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  return (
    <section className="preview-panel" aria-label="本地整理预览">
      <div className="preview-heading">
        <span className="preview-heading-icon">
          <Icon name="file" size={20} />
        </span>
        <div className="preview-heading-copy">
          <h2>本地整理预览</h2>
          <p>
            {preview ? `本地预览 · ${previewDate(preview.updated_at)}` : '还没有可用的整理预览'}
          </p>
        </div>
        {preview && (
          <span className="status-pill">
            {preview.scope === 'names' ? '快速名称清单' : '完整红心清单'}
          </span>
        )}
        {preview && (
          <button
            className="button secondary"
            aria-expanded={expanded}
            aria-controls={contentId}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? '收起整理内容' : '查看整理内容'}
            <span className={`preview-chevron ${expanded ? 'expanded' : ''}`}>
              <Icon name="arrow" size={16} />
            </span>
          </button>
        )}
      </div>
      {preview ? (
        <>
          <div className="preview-summary">
            <span>
              <strong>{preview.rename_count}</strong> 项名称调整
            </span>
            {preview.scope === 'full' && (
              <span>
                <strong>{preview.artists.length}</strong> 个歌手候选
              </span>
            )}
            <p>
              {preview.scope === 'names'
                ? `目录 ${preview.liked.expected} 首 · 未读取红心明细`
                : `目录 ${preview.liked.expected} 首 · ${preview.liked.observed === null ? '读取数量未记录' : `已读取 ${preview.liked.observed} 首`} · ${preview.liked.missing === null ? '缺失情况未记录' : `缺失 ${preview.liked.missing} 首`}`}
            </p>
          </div>
          {expanded && (
            <div id={contentId} className="preview-content">
              {artistsCompleted && (
                <p className="preview-history-note">
                  此前获批的五个精选已完成；下方为当前本地候选。
                </p>
              )}
              <p className="preview-check-note">
                <Icon name="shield" size={16} />
                执行前会重新核验账号中的歌单与歌曲。
              </p>
              <div className="preview-columns">
                <div className="preview-renames">
                  <h3>
                    名称调整 <span>{preview.rename_count} 项</span>
                  </h3>
                  {preview.renames.length ? (
                    <ul>
                      {preview.renames.map((rename) => (
                        <li key={rename.key}>
                          <span className="preview-old-name">{rename.old_name}</span>
                          <Icon name="arrow" size={15} />
                          <strong>{rename.name}</strong>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="preview-empty-note">本地清单没有名称调整。</p>
                  )}
                </div>
                <div className="preview-candidates">
                  <h3>
                    歌手精选 <span>{preview.artists.length} 个候选</span>
                  </h3>
                  {preview.scope === 'names' ? (
                    <p className="preview-empty-note">读取完整红心清单后可查看歌手候选。</p>
                  ) : preview.artists.length ? (
                    preview.artists.map((artist, index) => (
                      <ArtistCandidate
                        key={`${preview.updated_at}-${artist.name}-${index}`}
                        artist={artist}
                      />
                    ))
                  ) : (
                    <p className="preview-empty-note">本地清单没有歌手候选。</p>
                  )}
                </div>
              </div>
              {preview.limitations.length > 0 && (
                <ul className="preview-limitations">
                  {preview.limitations.map((message, index) => (
                    <li key={index}>{message}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      ) : (
        <p className="preview-empty-note">手动更新歌单清单后，可在这里查看名称调整与歌手候选。</p>
      )}
    </section>
  );
}
