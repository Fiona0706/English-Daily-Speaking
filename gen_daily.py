# -*- coding: utf-8 -*-
"""
gen_daily.py — 每日英语口语素材自动生成（路线 B 核心脚本）

由 GitHub Actions 每天北京时间 07:00 触发：
  1. 调 DeepSeek 生成当日新料（词/句/对话/造句/跟读）
  2. 强制校验（英文不含中文、长度、词性/等级合规）
  3. 与现有池去重后追加进 content.js
  4. 用 edge-tts 生成新条目的真人音频到 audio/en/
  5. 提交回仓库，CloudStudio 站点运行时拉取最新 content.js + 音频

字段结构与 dashboard_9594_v8_exercises/build_content.py 完全一致。
"""

import os
import io
import re
import sys
import json
import time
import asyncio

# ---------- 配置 ----------
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTENT_JS = os.path.join(OUT_DIR, "content.js")
AUDIO_DIR = os.path.join(OUT_DIR, "audio", "en")
VOICE = "en-US-AriaNeural"

POOL_NAMES = ["DAILY5_WORDS", "DAILY5_SENTENCES", "DAILY5_DIALOGUES", "DAILY5_PROMPTS", "SHADOW_PASSAGES"]

POS_SET = {"名词", "形容词", "动词", "副词", "短语动词", "习语"}
LEVEL_SET = {"初级", "中级", "高级"}
SCENE_SET = {"生活", "社交", "外贸物流", "职场"}

# DeepSeek
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# ---------- 工具 ----------
CJK = re.compile(r"[\u4e00-\u9fff]")


def log(*a):
    print("[gen_daily]", *a, flush=True)


def load_pools(path):
    code = open(path, encoding="utf-8").read()
    pools = {}
    for name in POOL_NAMES:
        m = re.search(r"var\s+" + name + r"\s*=\s*(\[.*?\];)", code, re.S)
        if not m:
            raise SystemExit("无法解析 " + name)
        arr_txt = m.group(1)[:-1]  # 去掉结尾 ;
        pools[name] = json.loads(arr_txt)
    return pools


def write_js(pools, path):
    out = []
    for name in POOL_NAMES:
        out.append("var %s = %s;\n" % (name, json.dumps(pools[name], ensure_ascii=False, indent=1)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))
    log("content.js 已写出，共", sum(len(v) for v in pools.values()), "条")


# ---------- LLM ----------
def llm(messages, max_tokens=2000, temperature=0.9):
    import urllib.request
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise SystemExit("LLM_API_KEY 环境变量缺失")
    data = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    })
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp["choices"][0]["message"]["content"]
        except Exception as e:  # noqa
            last_err = e
            log("LLM 请求失败(重试 %d): %s" % (attempt + 1, e))
            time.sleep(3)
    raise SystemExit("LLM 调用最终失败: %s" % last_err)


def extract_json(text):
    """从模型回复中抽取 JSON（兼容 ```json 围栏）"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 找最外层 [ 或 {
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        return None
    # 用括号配平截取
    opener, closer = text[start], ("}" if text[start] == "{" else "]")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def gen_json(prompt, max_tokens=2200):
    for _ in range(3):
        raw = llm([{"role": "system", "content": "你是严谨的英语教材编辑，只输出符合要求的 JSON，不要任何解释。"},
                   {"role": "user", "content": prompt}], max_tokens=max_tokens)
        js = extract_json(raw)
        if js:
            try:
                return json.loads(js)
            except Exception as e:  # noqa
                log("JSON 解析失败，重试:", e)
    raise SystemExit("无法从模型回复解析出 JSON")


# ---------- 各池生成 + 校验 ----------
def make_words(n=12):
    prompt = (
        "生成 %d 个日常英语口语词汇，作为 JSON 数组，每个元素："
        '{"word":"英文单词","phonetic":"美式音标(用/包裹, 如 /ˈwɔːtər/)","zh":"中文释义",'
        '"example":"用英文写的自然例句(5-12词, 不含中文)","exampleZh":"例句中文翻译","pos":"词性"}。\n'
        "词性 pos 必须是以下之一：名词、形容词、动词、副词、短语动词、习语，且 12 个里尽量均匀分布各词性。"
        "词汇要是真实生活里常用的（厨房、通勤、办公、情绪、购物等），不要生僻词。"
        "音标尽量准确。只返回 JSON 数组，不要其他内容。" % n
    )
    items = gen_json(prompt)
    good, dropped = [], 0
    for it in items:
        try:
            w = str(it.get("word", "")).strip()
            ph = str(it.get("phonetic", "")).strip()
            zh = str(it.get("zh", "")).strip()
            ex = str(it.get("example", "")).strip()
            exz = str(it.get("exampleZh", "")).strip()
            pos = str(it.get("pos", "")).strip()
            if not w or not ex or pos not in POS_SET:
                dropped += 1; continue
            if CJK.search(ex):
                dropped += 1; continue
            wc = len(ex.split())
            if not (4 <= wc <= 14):
                dropped += 1; continue
            good.append({"word": w, "phonetic": ph, "zh": zh, "example": ex, "exampleZh": exz, "pos": pos})
        except Exception:
            dropped += 1
    log("words 生成 %d，保留 %d，丢弃 %d" % (len(items), len(good), dropped))
    return good


def make_sentences(n=5):
    prompt = (
        "生成 %d 句地道美式日常短句，作为 JSON 数组，每个元素："
        '{"en":"英文短句(4-10词, 不含中文)","zh":"中文意思","scene":"场景","tag":"场景标签"}。'
        "scene 必须是：生活、社交、外贸物流、职场 之一。句子要口语化、真实可用。"
        "只返回 JSON 数组。" % n
    )
    items = gen_json(prompt)
    good, dropped = [], 0
    for it in items:
        try:
            en = str(it.get("en", "")).strip()
            zh = str(it.get("zh", "")).strip()
            scene = str(it.get("scene", "")).strip()
            tag = str(it.get("tag", scene)).strip()
            if not en or scene not in SCENE_SET:
                dropped += 1; continue
            if CJK.search(en):
                dropped += 1; continue
            if not (3 <= len(en.split()) <= 12):
                dropped += 1; continue
            good.append({"en": en, "zh": zh, "scene": scene, "tag": tag})
        except Exception:
            dropped += 1
    log("sentences 生成 %d，保留 %d，丢弃 %d" % (len(items), len(good), dropped))
    return good


def make_dialogues(n=3):
    prompt = (
        "生成 %d 段简短真实场景英文对话，作为 JSON 数组，每个元素："
        '{"theme":"中文主题","level":"难度","lines":[{"role":"说话人A","en":"英文句子","zh":"中文"}]}。'
        "level 必须是：初级、中级、高级；请按顺序让第1段初级、第2段中级、第3段高级。"
        "每段 4-8 句，单一日常主题（如点咖啡、问路、退换货、请假）。en 要自然口语、不含中文。"
        "只返回 JSON 数组。" % n
    )
    items = gen_json(prompt)
    good, dropped = [], 0
    for it in items:
        try:
            theme = str(it.get("theme", "")).strip()
            level = str(it.get("level", "")).strip()
            lines = it.get("lines", [])
            if not theme or level not in LEVEL_SET:
                dropped += 1; continue
            clean_lines = []
            for ln in lines:
                en = str(ln.get("en", "")).strip()
                zh = str(ln.get("zh", "")).strip()
                role = str(ln.get("role", "A")).strip() or "A"
                if not en or CJK.search(en):
                    dropped += 1; break
                clean_lines.append({"role": role, "en": en, "zh": zh, "collect": False})
            else:
                if 3 <= len(clean_lines) <= 10:
                    good.append({"theme": theme, "level": level, "lines": clean_lines})
                    continue
            if len(clean_lines) < 3:
                dropped += 1
        except Exception:
            dropped += 1
    log("dialogues 生成 %d，保留 %d，丢弃 %d" % (len(items), len(good), dropped))
    return good


def make_prompts(n=5):
    prompt = (
        "生成 %d 个用于“每日2句造句”的中文情境，作为 JSON 数组，每个元素："
        '{"zh":"中文情境描述","en":"一句地道美式英文参考表达(4-12词, 不含中文)","hint":"中文语法/用法提示"}。'
        "情境要贴近外贸/物流/职场/留学生活。en 自然口语。只返回 JSON 数组。" % n
    )
    items = gen_json(prompt)
    good, dropped = [], 0
    for it in items:
        try:
            zh = str(it.get("zh", "")).strip()
            en = str(it.get("en", "")).strip()
            hint = str(it.get("hint", "")).strip()
            if not zh or not en or CJK.search(en):
                dropped += 1; continue
            if not (3 <= len(en.split()) <= 14):
                dropped += 1; continue
            good.append({"zh": zh, "en": en, "hint": hint})
        except Exception:
            dropped += 1
    log("prompts 生成 %d，保留 %d，丢弃 %d" % (len(items), len(good), dropped))
    return good


def make_passages(n=1):
    prompt = (
        "生成 %d 段用于“影子跟读”的英文短文，作为 JSON 数组，每个元素："
        '{"theme":"中文主题","text":"英文正文"}。'
        "text 要求：4-8 句、60-120 词、一个连贯的真实日常场景（如通勤、做饭、开会、逛街），"
        "语言自然口语、不含任何中文字符。只返回 JSON 数组。" % n
    )
    items = gen_json(prompt)
    good, dropped = [], 0
    for it in items:
        try:
            theme = str(it.get("theme", "")).strip()
            text = str(it.get("text", "")).strip()
            if not theme or not text:
                dropped += 1; continue
            if CJK.search(text):
                dropped += 1; continue
            sentences = re.split(r"[.!?]+", text)
            sentences = [s for s in sentences if s.strip()]
            words = len(text.split())
            if not (3 <= len(sentences) <= 10) or not (40 <= words <= 140):
                dropped += 1; continue
            good.append({"theme": theme, "text": text})
        except Exception:
            dropped += 1
    log("passages 生成 %d，保留 %d，丢弃 %d" % (len(items), len(good), dropped))
    return good


# ---------- 去重 ----------
def dedup(pools, new_words, new_sent, new_dlg, new_prompt, new_pass):
    exist_words = {w["word"].lower() for w in pools["DAILY5_WORDS"]}
    exist_sent = {s["en"].lower() for s in pools["DAILY5_SENTENCES"]}
    exist_dlg_themes = {d["theme"] for d in pools["DAILY5_DIALOGUES"]}
    exist_prompt_en = {p["en"].lower() for p in pools["DAILY5_PROMPTS"]}
    exist_pass_themes = {p["theme"] for p in pools["SHADOW_PASSAGES"]}

    w = [x for x in new_words if x["word"].lower() not in exist_words]
    for x in w:
        exist_words.add(x["word"].lower())
    s = [x for x in new_sent if x["en"].lower() not in exist_sent]
    for x in s:
        exist_sent.add(x["en"].lower())
    d = [x for x in new_dlg if x["theme"] not in exist_dlg_themes]
    for x in d:
        exist_dlg_themes.add(x["theme"])
    p = [x for x in new_prompt if x["en"].lower() not in exist_prompt_en]
    for x in p:
        exist_prompt_en.add(x["en"].lower())
    pp = [x for x in new_pass if x["theme"] not in exist_pass_themes]
    for x in pp:
        exist_pass_themes.add(x["theme"])
    log("去重后新增 words=%d sent=%d dlg=%d prompt=%d passage=%d" % (len(w), len(s), len(d), len(p), len(pp)))
    return w, s, d, p, pp


# ---------- 音频 ----------
async def _tts_one(text, path, sem):
    import edge_tts
    async with sem:
        for attempt in range(3):
            try:
                c = edge_tts.Communicate(text, VOICE)
                await c.save(path)
                if os.path.getsize(path) > 0:
                    return True
            except Exception as e:  # noqa
                await asyncio.sleep(2)
        return False


async def gen_audio(tasks):
    """tasks: list of (text, path)"""
    sem = asyncio.Semaphore(16)
    real_tasks = [t for t in tasks if not os.path.exists(t[1]) or os.path.getsize(t[1]) == 0]
    if not real_tasks:
        return 0
    results = await asyncio.gather(*[_tts_one(t, p, sem) for t, p in real_tasks])
    ok = sum(1 for r in results if r)
    log("音频生成 %d/%d 成功" % (ok, len(real_tasks)))
    return ok


def build_audio_tasks(pools, w, s, d, p, pp):
    tasks = []
    base_w = len(pools["DAILY5_WORDS"])
    for i, it in enumerate(w):
        idx = base_w + i
        tasks.append((it["word"], os.path.join(AUDIO_DIR, "w%d.mp3" % idx)))
        tasks.append((it["example"], os.path.join(AUDIO_DIR, "we%d.mp3" % idx)))
    base_s = len(pools["DAILY5_SENTENCES"])
    for i, it in enumerate(s):
        tasks.append((it["en"], os.path.join(AUDIO_DIR, "s%d.mp3" % (base_s + i))))
    base_d = len(pools["DAILY5_DIALOGUES"])
    for i, it in enumerate(d):
        idx = base_d + i
        for li, ln in enumerate(it["lines"]):
            tasks.append((ln["en"], os.path.join(AUDIO_DIR, "dlg%d_%d.mp3" % (idx, li))))
        all_text = " ".join(ln["en"] for ln in it["lines"])
        tasks.append((all_text, os.path.join(AUDIO_DIR, "dlg%d_all.mp3" % idx)))
    base_p = len(pools["DAILY5_PROMPTS"])
    for i, it in enumerate(p):
        tasks.append((it["en"], os.path.join(AUDIO_DIR, "p%d.mp3" % (base_p + i))))
    base_pp = len(pools["SHADOW_PASSAGES"])
    for i, it in enumerate(pp):
        tasks.append((it["text"], os.path.join(AUDIO_DIR, "sp%d.mp3" % (base_pp + i))))
    return tasks


# ---------- 主流程 ----------
def main():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    pools = load_pools(CONTENT_JS)
    log("当前池: words=%d sent=%d dlg=%d prompt=%d passage=%d" % (
        len(pools["DAILY5_WORDS"]), len(pools["DAILY5_SENTENCES"]),
        len(pools["DAILY5_DIALOGUES"]), len(pools["DAILY5_PROMPTS"]), len(pools["SHADOW_PASSAGES"])))

    new_words = make_words(12)
    new_sent = make_sentences(5)
    new_dlg = make_dialogues(3)
    new_prompt = make_prompts(5)
    new_pass = make_passages(1)

    w, s, d, p, pp = dedup(pools, new_words, new_sent, new_dlg, new_prompt, new_pass)

    pools["DAILY5_WORDS"].extend(w)
    pools["DAILY5_SENTENCES"].extend(s)
    pools["DAILY5_DIALOGUES"].extend(d)
    pools["DAILY5_PROMPTS"].extend(p)
    pools["SHADOW_PASSAGES"].extend(pp)

    write_js(pools, CONTENT_JS)

    tasks = build_audio_tasks(pools, w, s, d, p, pp)
    try:
        asyncio.run(gen_audio(tasks))
    except Exception as e:  # noqa
        log("音频生成异常(已忽略，content.js 仍更新):", e)

    total = sum(len(v) for v in pools.values())
    log("完成。新增 %d 条，总计 %d 条。" % (len(w) + len(s) + len(d) + len(p) + len(pp), total))


if __name__ == "__main__":
    main()
