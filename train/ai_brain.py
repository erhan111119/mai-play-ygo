"""逐步问 AI 的大脑：读执行器写下的问题，用模型（或固定策略）给出答复。

通道与执行器的约定（见 ``train/windbot/MaiBotBrain.cs``；它挂在 WindBot 基类的
``Executor.AddExecutor`` 上，所以**任何**出牌脚本都能问 AI，不必换成通用执行器）：

* ``<前缀>.q``：执行器写的问题，``key=value`` 若干行（``id`` / ``kind`` / ``card`` /
  ``choice_ids`` …）；
* ``<前缀>.a``：我们写的答复，同样 ``key=value``（``id=<同一个 id>`` + ``answer=<yes|no|序号>``）。

三条规矩都是踩出来的：

1. **答复必须带对 id**：执行器只认自己那次问的 id，对不上的旧答复会被丢掉——
   否则"上一个问题的答案"会被当成"这一次的回答"，比不答还糟。
2. **答不出来就不写答复**（模型答非所问、超时、报错）：执行器等不到就会按脚本自己的判断继续，
   绝不会卡住整局。
3. **问题要带够上下文**：这里用卡库把卡名与效果文本补上（执行器只给卡号），
   模型才知道这张牌是干什么的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import json
import logging
import time

# 轮询间隔与单次答复的时间上限。
# **必须比执行器那边的等待（20 秒）小一点、但不能太小**：实测并发几局时 API 尾延迟能到十几秒，
# 12 秒就放弃会导致"一次都没答上"（那一臂等于白跑）。
POLL_INTERVAL = 0.05
ANSWER_DEADLINE = 35.0

# 允许的决策类型（与执行器一致；不认识的一律不答复）
KIND_ACTIVATE = "activate"
KIND_ATTACK_TARGET = "attack_target"
KIND_IDLE = "idle_action"   # "这一步做什么"：候选是主要阶段的全部合法动作（AI 打牌的入口）
KIND_TURN_PLAN = "turn_plan"

SCOPE_ALL = "all"
"""每一问都交给模型（默认口径，历史行为）。"""

SCOPE_HIGH_STAKES = "high_stakes"
"""只问高压决策：该不该交坑 / 打谁 / 这回合走哪条线；本家卡要不要发动交回脚本。

实测依据见 :func:`needs_model` 的说明（AI 的发动否决率 41%~86%，开 AI 反而少打 10%~13%）。
"""

SCOPE_INTERRUPT_ONLY = "interrupt_only"
"""只留"对手回合的应对"与"打谁"：连「这一步做什么」也交回脚本。

为什么还要这一档：``high_stakes`` 实测**没把动作数救回来**（80 局/腿：不问 23.1、
每问都问 20.7、只问高压 21.0），而它确实拦下了 47% 的发动提问——说明"否决发动"不是动作数下降的
原因。剩下的嫌疑是「这一步做什么」那个菜单（模型在替脚本决定整条线路）。
这一档用来把两者分开：它 ≈ 不问 AI 的话，锅就在菜单上（规矩：脚本本来就能打的牌别让模型掌舵，
脚本一步都走不出来的牌才需要它——见 0.11.0 的实测）。
"""
"""**不是执行器问的**，是我们自己在每个回合开头问一次："这一回合的目标是什么"。

为什么要它（实测教训）：AI 一直是"每一步局部选择"，做完 5 步之后它自己也不知道目标是什么，
于是经常出现"该做终场的一步被省下来"。现在回合开始先要一句话目标，后续每一步都带着它决策。
"""

# 大脑模型配置文件名（放在**插件数据目录**下，不进插件仓库、也不动宿主配置）
BRAIN_MODEL_FILE = "brain_model.toml"

# 卡组攻略目录：``<数据目录>/combos/<卡组编号>.txt``（展开流程/关键件/注意事项）
COMBO_DIR = "combos"


def load_combo_guide(data_dir: Path, deck_id: int, *, limit: int = 6000) -> str:
    """读这副牌的攻略要点（没有就返回空串）。

    存在的意义：AI 不知道这副牌的展开链时，会凭常识"省牌"——实测把展开件省下来不发，
    连基础展开都断了。攻略是由检索/人工整理出来的文字（见 ``combos/<编号>.txt``）。

    上限 6000（原来是 4000）：整理得好的攻略很容易写到三四千字（「升辉月」那份补上决策清单后
    3739 字），四千的线太容易**静默截断**——截断后模型看到的是半篇文章，还以为那就是全部。
    **截断会打一行提示**，不静默。
    """

    path = Path(data_dir) / COMBO_DIR / f"{int(deck_id)}.txt"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > limit:
        print(f"[提示] 攻略要点 {path.name} 有 {len(text)} 字，超过 {limit} 字上限，只喂前 {limit} 字")
        return text[:limit]
    return text


def condense_combo_guide(text: str) -> str:
    """把攻略要点压成开头那段"决策清单"（给"要不要发动/打谁"这类单点问题用）。

    理由：整份攻略（三千多字）**每一问都塞进提示词**，而大多数问题是"这张手坑现在交不交"——
    通篇牌组战略对它多半是噪音，还白等模型读完。清单（开头 ``★★★`` 那段）才是每问都该看的；
    「这一步做什么」与"定回合目标"仍然喂全篇（那里需要完整流程与自肃细节）。

    认不出清单标记（开头不是 ``★``、或那一段太短）就**原样返回全文**：
    宁可多喂，也不要因为格式变了就把资料悄悄删掉。
    """

    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip().startswith("★")), -1)
    if start < 0:
        return text
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip().startswith(("■", "★")):
            end = index
            break
    block = "\n".join(lines[start:end]).strip()
    return block if block.count("\n") >= 4 else text

# 预算不够（思考把 max_tokens 吃光、回来是空内容）时的翻倍上限
MAX_TOKENS_CEILING = 4096


@dataclass
class BrainModelSettings:
    """"问 AI"用哪个模型：base_url + key + 模型名（插件自带，见 :data:`BRAIN_MODEL_FILE`）。"""

    base_url: str
    api_key: str
    model: str
    max_tokens: int = 128
    temperature: float = 0.0
    timeout: int = 30

    def chat_url(self) -> str:
        """聊天补全的完整地址（base_url 末尾斜杠可有可无）。"""

        return self.base_url.rstrip("/") + "/chat/completions"


def _settings_from(data: dict, *, fallback_model: str = "") -> Optional[BrainModelSettings]:
    """从一段 TOML 里读模型设置（``brain_model.toml`` 顶层与 ``[strong]`` 段共用这段）。"""

    base_url = str(data.get("base_url") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    model = (str(data.get("model") or "").strip()) or fallback_model
    if not (base_url and api_key and model):
        return None
    return BrainModelSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=int(data.get("max_tokens") or 128),
        temperature=float(data.get("temperature") or 0.0),
        timeout=int(data.get("timeout") or 30),
    )


def load_brain_models(data_dir: Path) -> Tuple[Optional[BrainModelSettings], Optional[BrainModelSettings]]:
    """读模型配置；返回 ``(默认模型, 强模型或 None)``。

    ``[strong]`` 段是可选的"会思考的模型"，只给最需要想清楚的那类决策用
    （见 :func:`make_model_decider` 的按类型路由）。段里没写 ``base_url``/``api_key``
    时沿用顶层的（同一个服务商，省得重复填）；写了就覆盖。
    """

    import tomllib

    path = Path(data_dir) / BRAIN_MODEL_FILE
    if not path.is_file():
        return None, None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None, None

    default = _settings_from(data)
    strong_data = data.get("strong") or {}
    strong = None
    if isinstance(strong_data, dict) and str(strong_data.get("model") or "").strip():
        merged = {**data, **strong_data}
        merged.pop("strong", None)
        strong = _settings_from(merged)
    return default, strong


def load_brain_model_settings(data_dir: Path) -> Optional[BrainModelSettings]:
    """读 ``<数据目录>/brain_model.toml`` 的默认模型；没有或字段不全时返回 ``None``。

    **为什么放数据目录**：插件仓库是会被提交的，密钥不能进去；宿主的模型配置又不想动。
    数据目录在 ``<MaiBot>/data/plugins/<插件 id>/`` 下，既不在仓库里，也能被插件直接读。
    """

    return load_brain_models(data_dir)[0]


def chat_with_settings(
    settings: BrainModelSettings,
    prompt: str,
    *,
    request: Callable[..., bytes],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """用**插件自带**的模型配置发一次普通对话请求，返回模型正文。

    给"写打法数据 / 整理攻略"这类**工具**用：它们原先只认宿主模型注册表里的任务
    （宿主的 ``ygo_script`` 没配就直接失败），而我们已经有了插件自带的 ``brain_model.toml``
    ——工具也该用它，不必再依赖宿主配置。

    Raises:
        RuntimeError: 请求失败或返回空内容（原因原样带出去，不吞）。
    """

    budget = int(max_tokens or settings.max_tokens)
    body = json.dumps(
        {
            "model": settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": budget,
            "temperature": settings.temperature if temperature is None else temperature,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    raw = request(settings.chat_url(), data=body, headers=headers, timeout=settings.timeout)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"模型返回的不是 JSON：{raw[:200]!r}") from exc
    choice = (data.get("choices") or [{}])[0]
    content = str((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        usage = data.get("usage") or {}
        raise RuntimeError(
            f"模型返回空内容（finish_reason={choice.get('finish_reason')}，"
            f"completion_tokens={usage.get('completion_tokens')}）"
            "——多半是推理模型把预算花在思考上，换非推理模型或加大预算"
        )
    return content


def needs_model(question: Question, knowledge: Optional[object], *, scope: str) -> Tuple[bool, str]:
    """这一问要不要交给模型（``all`` 之外的三档见 :data:`SCOPE_HIGH_STAKES` 等常量）。

    实测的来龙去脉（都是 80 局/腿、卡组 #89 + ``PlanAware``、对手固定）：

    * 不问 AI **23.1** 动作/局；每一问都问 **20.7**；只问高压决策 **21.0**；
    * 只问高压**确实拦下了 47% 的发动提问**，可动作数一点没回来
      （与"每一问都问"差 0.2 次 = 0.2 个标准误）——**说明"否决发动"不是动作数下降的原因**；
    * 所以第一版的判据（"这张卡像不像阻抗" ``card_facts.is_interaction``）被推翻：
      它只覆盖 13% 的提问，而问得最多的恰恰是本家引擎件（`盈彩月夜之朔` 问 23 次、83% 被否），
      它们因为有速攻类效果被标成了"阻抗"；
    * 换成"**谁的回合**"（执行器写在问题里的 ``my_phase``）之后仍然没救回来，
      于是剩下的嫌疑指向「这一步做什么」那个菜单（模型在替脚本决定整条线路）——
      :data:`SCOPE_INTERRUPT_ONLY` 就是用来把它摘掉、跟基线对比的。

    Returns:
        ``(要不要问, 不要问的原因)``。
    """

    if scope not in (SCOPE_HIGH_STAKES, SCOPE_INTERRUPT_ONLY):
        return True, ""
    if scope == SCOPE_INTERRUPT_ONLY and question.kind == KIND_IDLE:
        # 连"这一步做什么"也交回脚本：只留下"对手回合的应对"与"打谁"
        return False, "只应对不掌舵：这一步做什么交回脚本"
    if question.kind != KIND_ACTIVATE:
        # 「这一步做什么」定整条线、「打谁」定这一拳——都是高压，留着问
        return True, ""
    if question.is_my_turn is None:
        return True, "问题里没写这是谁的回合（旧版执行器），照旧问"
    if question.is_my_turn:
        return False, "我的回合：本家卡要不要发动交回脚本"
    return True, ""


def make_model_decider(
    settings: BrainModelSettings,
    *,
    request: Callable[..., bytes],
    card_db: Optional[object] = None,
    logger: Optional[logging.Logger] = None,
    playbook: str = "",
    deck_name: str = "",
    combo_guide: str = "",
    knowledge: Optional[object] = None,
    deck_id: int = 0,
    decision_log: Optional[object] = None,
    strong_settings: Optional[BrainModelSettings] = None,
    scope: str = SCOPE_ALL,
) -> Callable[["Question"], str]:
    """造一个"把问题交给模型"的答复函数。

    Args:
        settings: 模型设置（base_url/密钥/模型名/预算）。
        request: 发 HTTP 请求的函数（**必填**：由调用方按自己的导入体系给——插件用相对导入、
            工具用绝对导入；本模块自己不去 import ``duel.*``，否则两种加载方式必有一种会崩
            （实测：插件按包加载时 ``from duel.netguard import …`` 直接 ModuleNotFound））。
        card_db: 卡库，用来把卡号补成"卡名 + 效果文本"。
        logger: 日志器（每次答复的耗时都会记下来，好判断值不值）。
        knowledge: 知识库（``duel/knowledge.py`` 的 :class:`~duel.knowledge.Knowledge`）。
            给了就在每次决策前按**这一问的局面**检索几条知识塞进提示词；没有就照原样
            （这一步是纯本机查询，毫秒级、不花钱）。
        deck_id: 我这副牌在卡组池里的编号（检索"我这副牌的线路"要用）。

    **预算不够会自动重问**：推理模型会把 max_tokens 全花在"思考"上、回来是空内容
    （实测 deepseek-flash 2048 都不够），所以这里把预算翻倍重问，直到拿到答复或到上限；
    每次决策的耗时都写日志——这直接决定"逐步问 AI 值不值"。
    """

    def do_request(url: str, *, data: bytes, headers: dict, timeout: int) -> bytes:
        """发一次请求（走调用方给的通道）。"""

        return request(url, data=data, headers=headers, timeout=timeout)

    def ask_model(prompt: str, active: Optional[BrainModelSettings] = None) -> str:
        """发一次请求并取回正文（空回时自动翻倍重问，直到上限）。

        ``active`` 是这次要用哪个模型：默认用传进来的快模型；
        "这一步做什么"会走 ``strong_settings``（会思考的那种，慢但更准）。
        实测：思考型模型必须给到 4096 才答得出来（2048 全被思考吃光）。
        """

        active = active or settings
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {active.api_key}",
        }
        budget = max(16, active.max_tokens)
        while True:
            body = json.dumps(
                {
                    "model": active.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": budget,
                    "temperature": active.temperature,
                }
            ).encode("utf-8")
            started = time.monotonic()
            raw = do_request(active.chat_url(), data=body, headers=headers, timeout=active.timeout)
            took = time.monotonic() - started
            data = json.loads(raw.decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            content = str((choice.get("message") or {}).get("content") or "").strip()
            finish = str(choice.get("finish_reason") or "")
            if logger is not None:
                logger.info(
                    "问 AI：%s｜%.1fs｜预算 %s｜%s",
                    active.model,
                    took,
                    budget,
                    "收到答复" if content else f"空回（finish={finish}）",
                )
            if content:
                return content
            if budget >= MAX_TOKENS_CEILING:
                return ""
            budget = min(MAX_TOKENS_CEILING, budget * 2)

    # 每个对局（每次构造这个 decider）一份的本局状态
    state: Dict[str, object] = {
        "turn": -1,
        "plan": "",
        "choices": [],
        "answered": {},   # 本回合 {(签名): 答复}——同一个问题只问一次模型
        "idle_count": 0,  # 本回合问了几次"这一步做什么"（第一次才用强模型）
    }

    def decide(question: Question) -> str:
        retrieved: List[str] = []
        if knowledge is not None:
            try:
                # 对手的卡：场上/魔陷区 + **已经亮出来的**（墓地/除外里露过的）。
                # 拿它推断"他在打什么体系"——≥3 张同系列才算主轴（见 Knowledge.retrieve）
                opponent_cards = [
                    card.card_id
                    for card in (
                        *question.zone("theirs"),
                        *question.zone("their_spell"),
                        *question.seen_cards(),
                    )
                    if card.card_id
                ]
                retrieved = list(
                    knowledge.retrieve(
                        deck_id=deck_id,
                        card_id=question.card_id,
                        opponent_cards=opponent_cards,
                        # "这一步做什么"要把整份展开流程喂进去：顺序性知识截成两行就等于丢了顺序
                        full_deck_plan=(question.kind == KIND_IDLE),
                    )
                )
            except Exception as exc:  # noqa: BLE001  知识库出错绝不能影响对局
                retrieved = []
                if logger is not None:
                    logger.warning("知识库检索失败（照原样问模型）：%s", exc)
            if logger is not None and retrieved:
                logger.info("知识库供料 %s 条：%s", len(retrieved), "｜".join(retrieved)[:200])

        # ---- 同一回合里重复出现的问题**直接复用上次的答复**（不再问模型）
        # 为什么必须缓存：思考型模型一次答复约 13 秒，而执行器会把同一个"要不要发动"在
        # 一个决策点里问好几遍（脚本的多条规则都指向它）——重复问就是在白等
        # （实测：一局里同一张灰流丽被问了 6 次，每次 1 秒；换成思考模型就是每次 13 秒）。
        # 签名覆盖**全部**选项：只看前几项时，选项表稍微变一变就会被误判成同一个问题。
        signature = f"{question.turn}|{question.kind}|{question.card_id}|{'/'.join(question.options)}"
        cache: Dict[str, str] = state["answered"]  # type: ignore[assignment]
        if signature in cache:
            cached = cache[signature]
            if logger is not None:
                logger.info("同一个问题本回合问过了，直接复用答复 %r", cached)
            return cached

        # ---- 范围闸门：不是高压决策就不花这次调用（`scope=high_stakes`）
        # 空答复的含义是"我不插手"，执行器就按脚本自己的判断走——正是我们要的"交回脚本"
        wanted, why_skip = needs_model(question, knowledge, scope=scope)
        if not wanted:
            if logger is not None:
                logger.info("这一步不问模型：%s（%s）", question.card_name or question.card_id, why_skip)
            if decision_log is not None:
                try:
                    decision_log.add(
                        kind=question.kind,
                        card_id=question.card_id,
                        board=(
                            f"turn={question.turn} my_lp={question.my_lp} opp_lp={question.opp_lp}"
                            f" hand={len(question.zone('hand'))} mine={len(question.zone('mine'))}"
                            f" theirs={len(question.zone('theirs'))} chain={question.chain_depth}"
                        ),
                        answer="",
                        audit=f"跳过：{why_skip}",
                        cost_ms=0,
                    )
                except Exception as exc:  # noqa: BLE001  落库失败绝不影响对局
                    if logger is not None:
                        logger.debug("决策日志写入失败：%s", exc)
            return ""

        # ---- 回合开始：先让模型自己定这一回合的目标（只多一次调用，但后面每一步都受益）
        if question.kind == KIND_IDLE and question.turn != state["turn"]:
            state["turn"] = question.turn
            state["choices"] = []
            state["plan"] = ""
            state["answered"] = {}
            state["idle_count"] = 0
            plan_prompt = build_prompt(
                Question(
                    question_id=question.question_id,
                    kind=KIND_TURN_PLAN,
                    my_lp=question.my_lp,
                    opp_lp=question.opp_lp,
                    turn=question.turn,
                    raw=question.raw,
                ),
                card_db,
                playbook=playbook,
                deck_name=deck_name,
                combo_guide=combo_guide,
                retrieved=retrieved,
            )
            if plan_prompt:
                try:
                    # 回合目标用**快模型**：实测思考型模型在这种开放式问题上会一直想，
                    # 4096 额度全被 reasoning 吃光还是空回（13~24 秒白等）
                    state["plan"] = extract_target_line(ask_model(plan_prompt))[:120]
                except Exception as exc:  # noqa: BLE001  计划问不出来不影响决策
                    if logger is not None:
                        logger.warning("回合目标没问出来（照常决策）：%s", exc)

        prompt = build_prompt(
            question,
            card_db,
            playbook=playbook,
            deck_name=deck_name,
            combo_guide=combo_guide,
            retrieved=retrieved,
            turn_plan=str(state["plan"]),
            my_choices=list(state["choices"]),  # type: ignore[arg-type]
        )
        if not prompt:
            return ""
        # 强模型（会思考的那种）只用在**这一回合的第一次**"这一步做什么"上：
        # 那一次决定整条展开线、最值得花 13 秒；之后的每一步都要快，否则一回合只问得起 4 次，
        # 出牌次数会掉（实测：全都用 flash → 特召 2.6→1.5、时长 50→89 秒）。
        idle_index = int(state.get("idle_count", 0))
        active = (
            strong_settings
            if (strong_settings is not None and question.kind == KIND_IDLE and idle_index == 0)
            else None
        )
        started_at = time.monotonic()
        try:
            content = ask_model(prompt, active)
            if question.kind == KIND_IDLE:
                state["idle_count"] = idle_index + 1
        except Exception as exc:  # noqa: BLE001  模型出错就当作"不答复"
            if logger is not None:
                logger.warning("问 AI 失败（这次不答复）：%s", exc)
            return ""
        if not content:
            return ""
        if logger is not None:
            reason = extract_reason(content)
            if reason:
                logger.info("AI 理由：%s", reason[:180])
        answer = normalise_answer(question, content)

        # 记下这次决定：让"本回合已经做过什么"能被后续问题看到，也供事后复盘
        if answer:
            label = question.card_name or (question.options[0] if question.options else question.kind)
            state["choices"] = list(state["choices"])[-5:] + [f"{question.kind}→{answer}（{label}）"]  # type: ignore[arg-type]
            cache[signature] = answer
        if decision_log is not None:
            try:
                chosen = ""
                if question.options and answer.isdigit() and int(answer) > 0:
                    index = int(answer)
                    if index <= len(question.options):
                        chosen = question.options[index - 1]
                decision_log.add(
                    kind=question.kind,
                    card_id=question.card_id,
                    board=(
                        f"turn={question.turn} my_lp={question.my_lp} opp_lp={question.opp_lp}"
                        f" hand={len(question.zone('hand'))} mine={len(question.zone('mine'))}"
                        f" theirs={len(question.zone('theirs'))} chain={question.chain_depth}"
                        + (f" chose={chosen}" if chosen else "")
                        + (" 选项=" + " / ".join(question.options[:8]) if question.options else "")
                    ),
                    retrieved="｜".join(retrieved)[:600],
                    answer=answer,
                    cost_ms=int((time.monotonic() - started_at) * 1000),
                )
            except Exception as exc:  # noqa: BLE001  落库失败绝不影响对局
                if logger is not None:
                    logger.debug("决策日志写入失败：%s", exc)
        return answer

    return decide


@dataclass
class ZoneCard:
    """场面/手牌里的一张卡（执行器给的：卡号;卡名;攻;守;表示形式）。"""

    card_id: int
    name: str = ""
    attack: int = 0
    defense: int = 0
    position: int = 0

    def describe(self, card_db: Optional[object] = None) -> str:
        """给模型看的一行：卡名 + 表示形式 + 攻守（+ 效果摘要）。

        **守备/里侧要把守备力说清楚**：AI 判断"能不能打过"靠的就是这两个数，
        实测不给数值时它会拿小怪去撞大怪。
        """

        label = self.name or f"卡号{self.card_id}"
        # OCG 的 CardPosition 位：1=表侧攻击 2=里侧攻击 4=表侧守备 8=里侧守备
        if self.position & 0x8:
            stance = "里侧守备"
        elif self.position & 0x4:
            stance = "表侧守备"
        elif self.position & 0x2:
            stance = "里侧攻击"
        else:
            stance = "表侧攻击"
        text = ""
        if card_db is not None:
            try:
                detail = card_db.card_details([self.card_id]).get(self.card_id)
                if detail is not None and detail.effect:
                    text = f"，效果：{detail.effect[:80]}"
            except Exception:  # noqa: BLE001  卡库读不动就少给点上下文
                text = ""
        return f"{label}（{stance}，攻{self.attack}/守{self.defense}{text}）"


def parse_zone(raw: str) -> List[ZoneCard]:
    """解析执行器写的 ``区域=卡号;卡名;攻;守;表示``（一张卡一段，可能多段）。"""

    cards: List[ZoneCard] = []
    for item in raw.split("|"):
        item = item.strip()
        if not item or item.startswith("（"):
            continue
        parts = item.split(";")
        if len(parts) < 1:
            continue

        def as_int(index: int) -> int:
            try:
                return int(parts[index])
            except (IndexError, ValueError):
                return 0

        cards.append(
            ZoneCard(
                card_id=as_int(0),
                name=parts[1].strip() if len(parts) > 1 else "",
                attack=as_int(2),
                defense=as_int(3),
                position=as_int(4),
            )
        )
    return cards


@dataclass
class Question:
    """执行器问的一件事。"""

    question_id: int
    kind: str
    card_id: int = 0
    card_name: str = ""
    choice_ids: List[int] = field(default_factory=list)
    choice_names: List[str] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    """``idle_action`` 的候选菜单（按序号排列，第 1 项对应 ``options[0]``）。"""
    turn: int = 0
    my_lp: int = 0
    opp_lp: int = 0
    raw: Dict[str, str] = field(default_factory=dict)

    def zone(self, key: str) -> List[ZoneCard]:
        """取某一片区域（``hand`` / ``mine`` / ``theirs`` …）。"""

        return parse_zone(self.raw.get(key, ""))

    def seen_cards(self) -> List[ZoneCard]:
        """对手**已经亮出来**的卡（场上、墓地、除外区里露过的；P1.5 起执行器会写）。

        用途有两个：① 推断对手在打什么体系（≥3 张同系列＝他的主轴）；
        ② 判断"他还有没有后续"——已经交过的坑不会再来一次。

        旧版 exe 不写这段，所以缺字段时返回空列表（不能因此报错）。
        """

        return parse_zone(self.raw.get("their_seen", ""))

    def chain_cards(self) -> List[ZoneCard]:
        """连锁上正在处理的卡（``卡号;卡名;控制者``；1.1.0 起执行器会写）。

        **这是"该不该交坑"的关键上下文**：只知道"我手里有灰流丽"没法决定要不要交，
        得知道对手现在发动的是什么。旧版 exe 不写，缺字段时返回空列表。
        """

        return parse_zone(self.raw.get("chain", ""))

    @property
    def banished_mine(self) -> int:
        """我方除外区张数（缺字段时返回 0）。"""

        return _int_or_zero(self.raw.get("banish_mine"))

    @property
    def is_my_turn(self) -> Optional[bool]:
        """现在是不是我的回合（``my_phase`` 由执行器写：``1`` 我的回合、``0`` 对手的回合）。

        **区分"我在展开"和"我在应对"就靠它**：同一个"要不要发动"的问题，
        在我自己的回合问的是本家引擎件（脚本的主场），在对手回合问的才是"该不该交这个坑"。
        旧版 exe 不写这一行，此时返回 ``None``（调用方要按"不知道"处理，别当成对手回合）。
        """

        value = self.raw.get("my_phase")
        if value is None or value == "":
            return None
        return _int_or_zero(value) == 1

    @property
    def banished_theirs(self) -> int:
        """对方除外区张数。"""

        return _int_or_zero(self.raw.get("banish_theirs"))

    @property
    def chain_depth(self) -> int:
        """连锁深度：0＝我在自由时点自己动，≥1＝我正顺着别人的效果应对。"""

        return _int_or_zero(self.raw.get("chain_depth"))


def _int_or_zero(raw: Optional[str]) -> int:
    """把问题里的数字字段转成 int；空/多值/坏值时返回 0。"""

    text = (raw or "").split("|")[0].strip()
    try:
        return int(text)
    except ValueError:
        return 0


def playbook_priority_tags(
    playbook: str, card_db: Optional[object]
) -> List[Tuple[str, str]]:
    """打法数据里的优先名单 → ``[(卡名, 标签)]``（拿不到卡名时返回空）。

    **为什么需要它**：``question.card_id`` 只在"要不要发动 / 打谁"这类问题里有值，
    而 AI 真正掌舵的是「这一步做什么」的菜单（``card_id`` 为 0）——于是
    "该优先召唤/该优先盖放"这条最硬的信号**在菜单里从来没出现过**。
    菜单选项的文本里带卡名（执行器写的 ``召唤 卡名（攻/守）``），所以这里按卡名匹配补上。
    """

    if card_db is None or not (playbook or "").strip():
        return []
    pairs: List[Tuple[str, str]] = []
    for key, label in (
        ("summon_order", "该优先召唤"),
        ("search_order", "该优先检索"),
        ("set_order", "该优先盖放"),
        ("activate_order", "该优先发动"),
        # "别拿它当怪兽用"：手坑/速攻这类**留在手牌才有用**的卡，执行器的菜单却会把它们
        # 列成"召唤/盖放 XXX"。实测模型真会挑：日志里它选过「里侧盖放 幽鬼兔」
        # 甚至「发动 增殖的G（自己回合）」——把等对手动作的手坑当展开件用掉了。
        ("never_summon", "别拿它当怪兽用（手坑留着，等对手动作时再从手牌丢）"),
    ):
        for card_id in parse_playbook_ids(playbook, key):
            try:
                name = card_db.name(int(card_id)) or ""
            except Exception:  # noqa: BLE001  卡名只是锦上添花，读不到就不标注
                continue
            if name:
                pairs.append((name, label))
    # 长的名字优先：「斯奎茲」是「斯奎茲-千金大獎」的子串，短名字会误命中
    pairs.sort(key=lambda item: -len(item[0]))
    return pairs


def tag_option_labels(labels: Sequence[str], tags: Sequence[Tuple[str, str]]) -> List[str]:
    """给菜单选项打上"打法数据点名"的标签（同一个选项只打一个）。"""

    if not tags:
        return list(labels)
    tagged: List[str] = []
    for label in labels:
        mark = next((tag for name, tag in tags if name in label), "")
        tagged.append(f"{label}（打法数据：{mark}）" if mark else label)
    return tagged


def parse_question(text: str) -> Optional[Question]:
    """解析执行器写下的问题；缺 id 或缺 kind 时返回 ``None``（不认识的格式不猜）。

    同一个键可能**出现多次**（区域里每张卡一行），所以用"多值"收集：
    ``raw`` 里存的是用 ``|`` 连起来的值，交给 :func:`parse_zone` 拆。
    """

    fields: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().lower()
        if key in fields:
            fields[key] = fields[key] + "|" + value.strip()
        else:
            fields[key] = value.strip()
    if "id" not in fields or "kind" not in fields:
        return None
    try:
        question_id = int(fields["id"].split("|")[0])
    except ValueError:
        return None

    def as_int(name: str, default: int = 0) -> int:
        try:
            return int(fields.get(name, "").split("|")[0] or default)
        except ValueError:
            return default

    def as_options() -> List[str]:
        """``idle_action`` 的候选：每行 ``编号;说明``，按编号排序后返回说明列表。"""

        pairs: List[Tuple[int, str]] = []
        for chunk in fields.get("option", "").split("|"):
            chunk = chunk.strip()
            if not chunk or ";" not in chunk:
                continue
            head, _, label = chunk.partition(";")
            try:
                pairs.append((int(head.strip()), label.strip()))
            except ValueError:
                continue
        pairs.sort(key=lambda item: item[0])
        return [label for _index, label in pairs]

    def as_list(name: str) -> List[str]:
        raw = fields.get(name, "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    choice_ids = []
    for item in as_list("choice_ids"):
        try:
            choice_ids.append(int(item))
        except ValueError:
            continue
    return Question(
        question_id=question_id,
        kind=fields["kind"].strip().lower(),
        card_id=as_int("card"),
        card_name=fields.get("card_name", "").split("|")[0],
        choice_ids=choice_ids,
        choice_names=as_list("choice_names"),
        options=as_options(),
        turn=as_int("turn"),
        my_lp=as_int("my_lp"),
        opp_lp=as_int("opp_lp"),
        raw=fields,
    )


def describe_card(question: Question, card_db: Optional[object] = None) -> str:
    """这张牌（或这只攻击者）的可读描述：卡名 + 效果文本。

    执行器只给卡号，卡名与效果文本得由这边补上（模型不知道卡号是干什么的）。
    卡库读不动就退回执行器给的卡名——少点上下文也比因为查库失败不答复强。
    """

    if card_db is None or not question.card_id:
        return question.card_name or f"卡号 {question.card_id}"
    try:
        details = card_db.card_details([question.card_id])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001  卡库读不动不该让这次决定没法做
        return question.card_name or f"卡号 {question.card_id}"
    detail = details.get(question.card_id)
    if detail is None:
        return question.card_name or f"卡号 {question.card_id}"
    if detail.effect:
        return f"{detail.name}（效果：{detail.effect}）"
    return str(detail.name)


# 游戏王决策提示词的角色设定：**说清规则语境与判断顺序**，并给出严格的输出格式。
# 三条经验都写进去了（都是实测踩出来的）：
#   1. 不给攻守数值时它会拿小怪撞大怪 → 局面里必须带攻/守；
#   2. 它会把自己展开要用的卡"省下来"不发 → 明确"展开优先、没有干扰就别省"；
#   3. 推理模型会被开放问题带跑 → 输出格式固定成两行（决定 + 一行理由），并要求不解释。
_YGO_ROLE = (
    "你是《游戏王 OCG》对局中的决策选手（大师规则 2020 / 新大师规则）。"
    "你只做当前这一个决定，不要重述规则、不要长篇分析。"
)


def _zone_lines(title: str, cards: Sequence[ZoneCard], card_db: Optional[object]) -> str:
    """把一片区域写成给模型看的几行。"""

    if not cards:
        return f"{title}：（空）"
    return title + "：\n" + "\n".join(f"  - {card.describe(card_db)}" for card in cards)


def parse_playbook_ids(playbook: str, key: str) -> List[int]:
    """从打法数据文本里取某个键的卡号（本模块自己解析，避免 import ``duel.playbook``）。

    本模块既被插件按包导入、也被工具按顶层导入，所以不能去碰 ``duel.*``（见
    :func:`make_model_decider` 的说明）；而这里只需要"按行取一个键"这么点逻辑，自己写更省事。
    """

    for line in (playbook or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip().lower() != key:
            continue
        ids: List[int] = []
        for item in value.replace("，", ",").split(","):
            item = item.strip()
            if item.isdigit():
                ids.append(int(item))
        return ids
    return []


def build_prompt(
    question: Question,
    card_db: Optional[object] = None,
    *,
    playbook: str = "",
    deck_name: str = "",
    combo_guide: str = "",
    retrieved: Sequence[str] = (),
    turn_plan: str = "",
    my_choices: Sequence[str] = (),
) -> str:
    """把一次决策写成"游戏王选手看得懂"的提问，并要求固定格式的两行回答。

    Args:
        question: 执行器写下的问题（局面 + 这次要决定什么）。
        card_db: 卡库（补卡名与效果文本）。
        playbook: 这副牌的打法数据（``duel/playbook.py`` 的原文）；给了就告诉模型
            "哪些是关键件"。
        deck_name: 卡组名（只用于让它知道自己在打什么体系）。
        combo_guide: 这副牌的**攻略要点**（展开流程/关键件/注意事项，见
            :func:`load_combo_guide`）。**这是最要紧的一段**：没有它时模型会凭常识
            "省牌"，把展开链上的卡省下来不发（实测"连基础展开都断了"）。
        retrieved: 知识库**按当前局面检索出来**的几条（见 ``duel/knowledge.py``）。

                它与 ``combo_guide`` 的区别是"静态 vs 临场"：攻略要点是整副牌的一篇说明书，
                检索到的是"这一问用得上的那几条"（这张牌的事实、我这副牌的线路、
                对手轴系的威胁）。两者都喂，前者给全局、后者给当次。
        turn_plan: **本回合的目标**（回合开始时让模型自己定的一句话）。
            每一步都带着它，模型才不会"做着做着忘了要做什么"。
        my_choices: 本回合**已经让它做过**的选择（例如"发动 强欲而贪欲之壶"）。
            用途是不自相矛盾、也不重复尝试同一个动作。
    """

    hand = question.zone("hand")
    mine = question.zone("mine")
    my_spell = question.zone("my_spell")
    theirs = question.zone("theirs")
    their_spell = question.zone("their_spell")
    state = [
        f"第 {question.turn} 回合｜我方 LP {question.my_lp}｜对方 LP {question.opp_lp}",
        _zone_lines("我的手牌", hand, card_db),
        _zone_lines("我的怪兽区", mine, card_db),
        _zone_lines("我的魔陷区", my_spell, card_db),
        _zone_lines("对方怪兽区", theirs, card_db),
        _zone_lines("对方魔陷区", their_spell, card_db),
        f"墓地：我方 {question.raw.get('grave_mine', '?')} 张｜对方 {question.raw.get('grave_theirs', '?')} 张",
    ]
    # 除外区与连锁深度：只有新版执行器会写；缺字段时不提（不能凭空编一个 0 出来）
    if "banish_mine" in question.raw or "banish_theirs" in question.raw:
        state.append(
            f"除外：我方 {question.banished_mine} 张｜对方 {question.banished_theirs} 张"
        )
    if question.chain_depth:
        state.append(f"现在处于连锁中（深度 {question.chain_depth}）——这是在应对别人的效果，不是我自己展开")
    chain = question.chain_cards()
    if chain:
        state.append(
            "连锁上正在处理的卡（判断该不该交坑就看这里）：\n"
            + "\n".join(
                f"  - {card.name or card.card_id}（{'我方' if card.position == 0 else '对方'}）"
                for card in chain[:6]
            )
        )
    seen = question.seen_cards()
    if seen:
        state.append(
            "对方已经亮出来的卡（用它判断他还有什么、还有什么坑）：\n"
            + "、".join(f"{card.name or card.card_id}" for card in seen[:12])
        )
    context = "\n".join(state)
    if deck_name or playbook or combo_guide:
        tips = [f"我这副牌是「{deck_name}」" if deck_name else "我在打自己的卡组"]
        if combo_guide.strip():
            # 单点问题（要不要发动 / 打谁）只喂决策清单，省下的篇幅让它把注意力放在这一问上；
            # "这一步做什么"与"定回合目标"要完整攻略（那里才需要流程与自肃细节）
            guide_text = combo_guide.strip()
            if question.kind in (KIND_ACTIVATE, KIND_ATTACK_TARGET):
                guide_text = condense_combo_guide(guide_text)
            tips.append("【这副牌的攻略要点（按它打，别把展开件省下来）】\n" + guide_text)
        if playbook.strip():
            tips.append("打法数据（优先级从高到低）：\n" + playbook.strip())
        context += "\n\n" + "\n".join(tips)
    if retrieved:
        # 检索到的几条放最后（离问题最近的位置），并明确标注它的性质：
        # 它是本机整理/抽取的资料，**可能过时或抽错**，局面优先
        context += (
            "\n\n【知识库（按当前局面检索；可能过时或抽得有偏差，与局面冲突时以局面为准）】\n"
            + "\n".join(f"  - {item}" for item in retrieved)
        )

    if turn_plan.strip():
        context += f"\n\n【本回合目标（你自己定的，按它做）】{turn_plan.strip()}"
    if my_choices:
        context += "\n【本回合已经让它做过的】" + "；".join(list(my_choices)[-6:])

    # 这张牌是不是"打法数据里点名的关键件"：点名的直接告诉它——这是脚本之外最硬的信号
    key_roles: List[str] = []
    for key, label in (
        ("activate_order", "该优先发动"),
        ("search_order", "该优先检索"),
        ("summon_order", "该优先召唤"),
        # 盖放也是"这一步做什么"的合法选项（陷阱/速攻），打法数据早就写了 set_order，
        # 但提示词一直没用它——模型看到"盖放 XXX"时拿不到"这是你最该盖的那张"这个信号
        # （实测「升辉月」的輝煌就是这种：盖下去等于多一次干扰，不盖则白留在手里）
        ("set_order", "该优先盖放"),
        # "别拿它当怪兽用"：手坑这类留在手里才有用的卡。菜单位那侧（playbook_priority_tags）
        # 也认这个键；这里补上按卡号的问题（"要不要发动/盖放 XXX"）——
        # 实测模型选过「里侧盖放 幽鬼兔」，这种问题必须给它同一个信号
        ("never_summon", "别拿它当怪兽用（手坑留着，等对手动作时再从手牌丢）"),
    ):
        if question.card_id and question.card_id in parse_playbook_ids(playbook, key):
            key_roles.append(label)
    if question.card_id and question.card_id in parse_playbook_ids(playbook, "never_activate"):
        # 打法数据里的"谨慎"名单：**只提醒、不禁止**（实测模型会把手坑列进去，
        # 硬禁等于灰流丽永远不用）
        key_roles.append("打法数据提醒要谨慎（通常是留着的手坑/容易被骗的坑）")
    if key_roles:
        context += f"\n【脚本/打法数据对这张牌的定位】{'、'.join(key_roles)}"

    if question.kind == KIND_ACTIVATE:
        judgement = (
            "判断顺序（**默认跟脚本走**）：\n"
            "① 这张卡如果是展开链上的关键件（看上面攻略要点/定位）→ **发动**；\n"
            "② 现在是能打断对方的关键时点 → **发动**；\n"
            "③ 只有你能说出「这次发动会明显亏」（例如它是关键件却被白白浪费、"
            "或留着下回合能直接赢）才「不发动」。\n"
            "三条**时点/自杀**的特例（实测错过这三种最常见）：\n"
            "· 手坑看时点：G／朔夜时雨／灰流丽这类只在**对手回合、对手正在做对应动作**时才发动；"
            "自己回合为了「抽一张」发动它们＝白送一张阻抗；\n"
            "· 会炸到自己场面的效果（无差别清场、连自己的怪/魔陷一起破坏）：除非能当回合打死对面，"
            "否则不发动；\n"
            "· 会让自己这回合**不能再用主要手段**的效果（自肃）在展开没做完前别发动。\n"
            "反面提醒：**只顾着省牌会把基础展开打断**，那是比多打一张更严重的错。\n"
            "输出格式（两行，第一行必须是决定，第二行一句话理由，不要写别的）：\n"
            "决定：发动\n"
            "理由：……\n"
            "（不发动就把第一行写成「决定：不发动」）"
        )
    elif question.kind == KIND_ATTACK_TARGET:
        # 候选用执行器给的 choice_ids/choice_names + 场上对应卡的攻守（按卡号找回数值）
        defenders = {card.card_id: card for card in theirs}
        option_lines = ["0＝不攻击"]
        for index, card_id in enumerate(question.choice_ids, start=1):
            found = defenders.get(card_id)
            if found is not None:
                option_lines.append(f"{index}＝{found.describe(card_db)}")
            else:
                name = question.choice_names[index - 1] if index - 1 < len(question.choice_names) else card_id
                option_lines.append(f"{index}＝{name}")
        attacker = None
        for card in mine:
            if card.card_id == question.card_id:
                attacker = card
                break
        attacker_text = attacker.describe(card_db) if attacker is not None else (question.card_name or "我的怪兽")
        judgement = (
            f"要决定的是：让「{attacker_text}」攻击哪个目标。\n"
            "候选：\n  " + "\n  ".join(option_lines) + "\n"
            "判断顺序：① 打得过（攻击力＞对方的攻击力；对方守备表示则＞其守备力）就优先打，"
            "能直接攻击玩家时优先打玩家（除非要留怪防守）；"
            "② 打不过就别攻击（0）——除非有明确理由（例如必须攻击、或攻击能触发自己的效果）；"
            "③ 对方有盖卡/未知后场时，权衡被坑的风险。\n"
            "输出格式（两行，第一行必须是数字，第二行一句话理由，不要写别的）：\n"
            "决定：1\n"
            "理由：……"
        )
    elif question.kind == KIND_IDLE:
        # "这一步做什么"：候选是执行器列出的全部合法动作。这是**AI 真正出牌**的地方——
        # 脚本不认识的卡组（新系列、投稿卡组）全靠这里动起来，所以要写清"从零开始怎么想"。
        option_lines = [f"{index}＝{label}" for index, label in enumerate(question.options, start=1)]
        # 菜单里也把"打法数据点名的关键件"标出来（card_id 为 0，上面那套按卡号的标注在这里用不上）
        option_lines = tag_option_labels(option_lines, playbook_priority_tags(playbook, card_db))
        judgement = (
            "现在是你的主要阶段，要你选**这一步做什么**。候选：\n  "
            + "\n  ".join(option_lines)
            + "\n 0＝不插手，继续按脚本自己的判断走。\n"
            "判断顺序：\n"
            "① **先把展开做出来**：能召唤/特殊召唤的本家怪兽、能把卡组里的关键件找出来的效果，优先做；\n"
            "② 有能直接扩大场面或解掉对手威胁的效果就发动（攻守数值见候选里的括注）；\n"
            "③ 展开做完了、场上够打了，再进战斗阶段；没有后续可做就结束主要阶段；\n"
            "④ **手坑不要当怪兽用**：候选里带「别拿它当怪兽用」或名字是手坑（增殖的G／灰流丽／"
            "朔夜时雨／幽鬼兔／效果遮蒙者／锁鸟／欢聚友伴…）的「召唤／盖放／里侧盖放」项，**别选**——"
            "它们待在手里才有用（等对手动作时从手牌丢弃），摆到场上等于废掉一张阻抗；\n"
            "⑤ **手坑也别在自己回合「发动」**：增殖的 G、朔夜时雨这类只在**对手回合、对手正在做"
            "对应动作**时才有价值（G＝对手要特召、灰流丽＝对手要检索/堆墓/抽卡）。自己回合为了"
            "「抽一张」把它们发动掉是明显亏的；\n"
            "⑥ **会炸到自己场面的效果**（无差别清场、把自己怪/魔陷一起破坏）除非能当回合打死对面"
            "或换到更大优势，否则不要发动——先看自己场上有什么再决定；\n"
            "⑦ **0 只在一种情况下选**：你确定脚本自己会做得更好（例如它手里有明确的 combo，"
            "而你选不出比它更优的一步）。**脚本通常只是「随手打」**——这副牌若是新系列它根本不会展开，"
            "那时候选 0 等于这一回合空过。\n"
            "输出格式（两行，第一行必须是序号，第二行一句话理由，不要写别的）：\n"
            "决定：1\n"
            "理由：……"
        )
    elif question.kind == KIND_TURN_PLAN:
        # 回合目标：只要一句话，不要长篇分析（它会被塞进这一回合接下来的每一次决策里）
        judgement = (
            "先定**这一回合的目标**（一句话，20~40 字），后面每一步都会按它走。例如：\n"
            "「用 100267031 起手，做出鲜花女男爵 + 留一张坑」\n"
            "「场面不占优：只留手坑，攒资源等下一回合」\n"
            "「能直接打死：全部转攻击，优先打脸」\n"
            "输出格式（一行，不要写别的）：\n"
            "目标：……"
        )
    else:
        return ""

    headline = {
        KIND_IDLE: "这一步做什么",
        KIND_TURN_PLAN: "这一回合要做什么",
    }.get(question.kind, "")
    if not headline:
        headline = describe_card(question, card_db)
    return (
        f"{_YGO_ROLE}\n\n"
        f"【局面】\n{context}\n\n"
        f"【要你决定的】{headline}\n"
        f"{judgement}\n"
    )


def extract_target_line(text: str) -> str:
    """从模型的回答里取出"这一回合的目标"；认不出来就返回第一行。"""

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    for line in lines:
        if line.startswith("目标"):
            return line.split("：", 1)[-1].split(":", 1)[-1].strip()
    return lines[0][:80]


def extract_decision_line(text: str) -> str:
    """从两行格式里取出"决定"那一行；只有一行时原样返回。"""

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith("决定"):
            return line.split("：", 1)[-1].split(":", 1)[-1].strip()
    return lines[0] if lines else ""


def extract_reason(text: str) -> str:
    """取出"理由"那一行（只用于日志与复盘，不参与决策）。"""

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("理由"):
            return stripped.split("：", 1)[-1].split(":", 1)[-1].strip()
    return ""


def normalise_answer(question: Question, text: str) -> str:
    """把模型的话收敛成执行器认的答复；认不出来时返回空串（＝不答复）。

    现在要求的是**两行**（``决定：…`` + ``理由：…``），所以先只看"决定"那一行
    （``extract_decision_line``）；兼容模型只回一个词的老写法。
    只做**严格**映射：模型说"看情况"这种含糊回答一律当没答——与其猜，不如让脚本按自己的判断走。
    """

    answer = extract_decision_line(text).strip()
    if not answer:
        return ""
    if question.kind == KIND_ACTIVATE:
        lowered = answer.lower()
        # **先判否定**："不发动" 里也有 "发动" 两个字，顺序写反就会把"不要发动"读成"发动"
        # （这种反义误读比不答复糟得多，测试专门钉了它）
        if lowered.startswith("no") or "不发动" in answer or "别发动" in answer or "不要发动" in answer:
            return "no"
        if lowered.startswith("yes") or "发动" in answer:
            return "yes"
        return ""
    if question.kind == KIND_ATTACK_TARGET:
        # "不攻击/不打/pass" 这类否定说法等于选了 0（不打），得在抠数字之前认出来——
        # 否则会被当成"没答"，白白放弃一次能听懂的答复
        if any(word in answer for word in ("不攻击", "不打", "别打", "不要攻击")) or answer.lower() in (
            "pass",
            "no",
        ):
            return "0"
        digits = "".join(char for char in answer if char.isdigit())
        if not digits:
            return ""
        index = int(digits[:2])
        if index > len(question.choice_ids):
            return ""
        return str(index)
    if question.kind == KIND_IDLE:
        # 选动作：只认序号，以及"结束/进战斗"这类能唯一对上菜单项的说法。
        # 认不出来就返回空串＝不插手（比瞎猜一个动作安全：猜错会把展开打歪）
        digits = "".join(char for char in answer if char.isdigit())
        if digits:
            index = int(digits[:2])
            if 0 <= index <= len(question.options):
                return str(index)
            return ""
        for candidate_index, label in enumerate(question.options, start=1):
            for word in ("结束", "战斗", "发动", "召唤", "盖放", "特殊召唤"):
                if word in answer and word in label:
                    return str(candidate_index)
        if "不插手" in answer or "跟脚本" in answer or answer.lower() in ("pass", "no"):
            return "0"
        return ""
    return ""


def decide_with_rule(question: Question, *, answer: str = "no") -> str:
    """固定策略（对照与测试用）：对每类问题都给同一个答复。"""

    if question.kind == KIND_ACTIVATE:
        return answer if answer in ("yes", "no") else "no"
    if question.kind == KIND_ATTACK_TARGET:
        return answer if answer.isdigit() else "0"
    return ""


class BrainServer:
    """轮询问题文件、把答复写回去。

    Args:
        prefix: 问答文件的前缀（执行器用 ``BrainFile=<这个前缀>`` 启动）。
        decide: ``(question) -> 答复``；返回空串表示"这次不答复"（执行器超时后按脚本判断走）。
        logger: 日志器。
    """

    def __init__(
        self,
        prefix: Path,
        decide,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._prefix = Path(prefix)
        self._decide = decide
        self._logger = logger
        self._answered = 0
        self._asked = 0
        self._last_id = -1
        self._answers: Dict[str, int] = {}

    @property
    def question_path(self) -> Path:
        """执行器写问题的路径。"""

        return Path(str(self._prefix) + ".q")

    @property
    def answer_path(self) -> Path:
        """我们写答复的路径。"""

        return Path(str(self._prefix) + ".a")

    @property
    def answered(self) -> int:
        """已经答复过多少次（给报告用）。"""

        return self._answered

    @property
    def answers(self) -> Dict[str, int]:
        """各种答复各出现了几次（``{"yes": 42, "no": 17, "0": 3}``）。

        为什么值得统计：AI 只能**否决**脚本想做的事，所以"否决率"就是它实际施加的影响。
        报告里没有这个数，胜率变化就说不清是 AI 判断得好还是它把该做的事都拦掉了。
        """

        return dict(self._answers)

    async def serve_forever(self) -> None:
        """一直服务到被取消。"""

        while True:
            await self.serve_once()
            await asyncio.sleep(POLL_INTERVAL)

    async def serve_once(self) -> bool:
        """检查并答复一个问题；返回是否答复了。"""

        path = self.question_path
        if not path.is_file():
            return False
        try:
            question = parse_question(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
        if question is None or question.question_id == self._last_id:
            return False
        self._last_id = question.question_id
        self._asked += 1
        started = time.monotonic()
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(self._decide, question), timeout=ANSWER_DEADLINE
            )
        except asyncio.TimeoutError:
            if self._logger is not None:
                self._logger.warning("问 AI 超时（%.0fs），这次不答复", ANSWER_DEADLINE)
            return False
        except Exception as exc:  # noqa: BLE001  问 AI 出错绝不能影响对局
            if self._logger is not None:
                self._logger.warning("问 AI 出错，这次不答复：%s", exc)
            return False
        if not answer:
            return False
        self._write_answer(question.question_id, answer)
        self._answered += 1
        self._answers[answer] = self._answers.get(answer, 0) + 1
        if self._logger is not None:
            self._logger.info(
                "答 AI 一次：%s → %s（%.1fs）", question.kind, answer, time.monotonic() - started
            )
        return True

    def _write_answer(self, question_id: int, answer: str) -> None:
        """把答复写给执行器（带 id，让它认出这是回答哪一次）。"""

        target = self.answer_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".{question_id}.tmp")
        tmp.write_text(f"id={question_id}\nanswer={answer}\n", encoding="utf-8")
        tmp.replace(target)
