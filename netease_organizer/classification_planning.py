"""Combine reviewed recording judgments with derived original-lyric evidence."""
import math

SCENES={'focus':'学习专注','commute':'通勤散步','relax':'放松睡前',
        'workout':'运动提神','groove':'快乐律动','solitude':'独处释怀'}
STYLES={'pop':'流行抒情','rnb':'R&B 与灵魂','funk':'Funk 与 Disco','jazz':'爵士与 Lo-Fi',
        'hiphop':'嘻哈说唱','rock':'摇滚与独立','folk':'民谣与木吉他',
        'electronic':'电子舞曲','instrumental':'纯音乐与影视配乐','classical':'古典与合唱'}
LANGUAGES={'mandarin':'国语','cantonese':'粤语','en':'英语','ja':'日语','ko':'韩语',
           'ru':'俄语','other':'其他与多语'}


def validate_rows(rows,count):
    if type(rows) is not list or len(rows)!=count:raise ValueError('分类行数不完整。')
    ordered=sorted(rows,key=lambda row:row[0])
    for pos,row in enumerate(ordered,1):
        if (type(row) is not list or len(row)!=6 or type(row[0]) is not int or row[0]!=pos
            or type(row[1]) is not list or not 1<=len(row[1])<=2
            or len(set(row[1]))!=len(row[1]) or not set(row[1])<=set(STYLES)|{'unknown'}
            or 'unknown' in row[1] and row[1]!=['unknown']
            or type(row[2]) is not list or len(row[2])>3 or len(set(row[2]))!=len(row[2])
            or not set(row[2])<=set(SCENES)
            or row[3] is not None and row[3] not in set(LANGUAGES)|{'instrumental'}
            or type(row[4]) not in (float,int) or not math.isfinite(row[4]) or not 0<=row[4]<=1
            or type(row[5]) is not str or not row[5].strip()):raise ValueError('分类行格式不兼容。')
    return ordered


def infer_language(model,features):
    """Return label, provenance note, review flag; no country/title inference."""
    s=features.get('script_counts',{});h=features.get('language_hints',[])
    # Older derived caches suppressed hints and the body flag whenever a
    # provider marker existed. Their retained counts can still reveal a conflict.
    substantial_body=features.get('original_line_count',0)>=8 and sum(s.values())>=150
    has_body=bool(features.get('has_original_lyric_text')) or substantial_body
    if features.get('instrumental_evidence'):
        if model not in (None,'instrumental'):
            return model,'具体录音语种判断；与平台纯音乐标记冲突，已保留复核提示',True
        if has_body:
            return model,'平台纯音乐标记与原歌词正文并存；可能为采样、译文或错挂，需核验具体录音',True
        return 'instrumental','平台提供纯音乐标记；不能证明无任何人声采样',False
    han=s.get('han',0);kana=s.get('kana',0);latin=s.get('latin',0)
    # Keep a label separate from the compatible recording judgments. A script
    # can contradict a known language without proving which language it is.
    evidence=[]
    ambiguous=[]
    japanese=kana>=max(6,han*.04)
    if japanese:evidence.append(('ja',{'ja'}))
    if s.get('hangul',0)>=10 or 'ko' in h:evidence.append(('ko',{'ko'}))
    if not japanese and (han>=20 or 'zh_or_yue' in h):
        if features.get('cantonese_specific_character_count',0)>=3:
            evidence.append(('cantonese',{'mandarin','cantonese'}))
        else:
            evidence.append((None,{'mandarin','cantonese','other'}))
            ambiguous.append('普通汉字不能区分国语、粤语及其他汉字书写语言')
    if s.get('cyrillic',0)>=10 or 'ru_or_other_cyrillic' in h:
        if features.get('ukrainian_specific_character_count',0)>=3:
            evidence.append(('other',{'other'}))
        else:
            evidence.append((None,{'ru','other'}))
            ambiguous.append('西里尔文字不能单独确定俄语')
    if s.get('arabic',0)>=10 or s.get('thai',0)>=10 or 'other_script' in h:
        evidence.append(('other',{'other'}))
    latin_hint=next((x for x in h if x in ('en','es','fr','pt','de','it')),None)
    # Small foreign refrains and credits do not replace the dominant script.
    script_total=han+kana+s.get('hangul',0)+s.get('cyrillic',0)+s.get('arabic',0)+s.get('thai',0)
    if not evidence or latin>=max(100,script_total*.45):
        if latin_hint:
            label='en' if latin_hint=='en' else 'other'
            evidence.append((label,{label}))
        elif 'latin_undetermined' in h or latin>=50:
            evidence.append((None,{'en','other'}))
            ambiguous.append('显著拉丁文字缺少足够的具体语言证据')
    if model=='instrumental':
        if substantial_body:
            return model,'原歌词字段存在文字；可能为采样、译文或错挂，保留具体录音判断并标记复核',True
        return model,'具体录音器乐判断；短文本可能为说明或采样，保留复核提示',bool(features.get('has_original_lyric_text'))
    if model is not None:
        compatible=(all(model in allowed for _,allowed in evidence)
                    or model=='other' and len(evidence)>1)
        if not compatible:
            return model,'具体录音与原歌词文字提示冲突；文字可能含译文，保留演唱版本并标记复核',True
        return model,'具体演唱版本判断；原始歌词文字作为补充核对',False
    labels={label for label,_ in evidence}
    if len(labels)>1:
        return None,'原歌词字段含多种显著文字，无法排除内嵌译文；待辨识',True
    if ambiguous:
        return None,'；'.join(ambiguous)+'；待辨识，需核验具体录音',True
    if labels:
        inferred=next(iter(labels))
        note='原始歌词文字与功能词判断；未经音频听辨'
        if inferred in ('mandarin','cantonese'):note+='；汉字本身不能严格区分国语与粤语'
        return inferred,note,False
    return None,'原始歌词文字不足以判定语种；保留待辨识',False
