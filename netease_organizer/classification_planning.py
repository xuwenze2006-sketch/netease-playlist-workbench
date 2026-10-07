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
    if features.get('instrumental_evidence'):
        if model not in (None,'instrumental'):
            return model,'具体录音语种判断；与平台纯音乐标记冲突，已保留复核提示',True
        return 'instrumental','平台提供纯音乐标记；不能证明无任何人声采样',False
    s=features.get('script_counts',{});h=features.get('language_hints',[])
    han=s.get('han',0);kana=s.get('kana',0);latin=s.get('latin',0)
    major=[]
    if 'ja' in h and kana>=max(6,han*.04):major.append('ja')
    if 'ko' in h:major.append('ko')
    if 'zh_or_yue' in h:
        major.append('cantonese' if features.get('cantonese_specific_character_count',0)>=3 else 'mandarin')
    if 'ru_or_other_cyrillic' in h:
        major.append('other' if features.get('ukrainian_specific_character_count',0)>=3 else 'ru')
    if 'other_script' in h:major.append('other')
    latin_hint=next((x for x in h if x in ('en','es','fr','pt','de','it')),None)
    # Small foreign refrains and credits do not replace the dominant script.
    script_total=han+kana+s.get('hangul',0)+s.get('cyrillic',0)+s.get('arabic',0)+s.get('thai',0)
    if latin_hint and (not major or latin>=max(100,script_total*.45)):
        major.append('en' if latin_hint=='en' else 'other')
    inferred='other' if len(set(major))>1 else major[0] if major else None
    if model=='instrumental':
        if features.get('original_line_count',0)>=8 and sum(s.values())>=150 and inferred:
            return model,'原歌词字段存在文字；可能为采样、译文或错挂，保留具体录音判断并标记复核',True
        return model,'具体录音器乐判断；短文本可能为说明或采样，保留复核提示',bool(features.get('has_original_lyric_text'))
    if model is not None:
        compatible=(inferred in (None,model) or model in ('mandarin','cantonese') and inferred in ('mandarin','cantonese'))
        if not compatible:
            return model,'具体录音与原歌词文字提示冲突；文字可能含译文，保留演唱版本并标记复核',True
        return model,'具体演唱版本判断；原始歌词文字作为补充核对',False
    if inferred:
        if len(set(major))>1:
            return None,'原歌词字段含多种显著文字，无法排除内嵌译文；待辨识',True
        note='原始歌词文字与功能词判断；未经音频听辨'
        if inferred in ('mandarin','cantonese'):note+='；汉字本身不能严格区分国语与粤语'
        return inferred,note,False
    return None,'原始歌词文字不足以判定语种；保留待辨识',False
