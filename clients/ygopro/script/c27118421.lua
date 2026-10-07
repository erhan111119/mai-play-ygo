--四天之龍 飢餓毒液結合龍 / 四天之龙 凶饿毒融合龙 (official id 27118421)
--手写补档：这份客户端脚本包里没有这张卡。上游那份脚本用的是新版 Fusion.* 模块，
--而本引擎的 utility.lua / procedure.lua 里并没有定义 Fusion（grep "function Fusion.AddProcMixN" 全库无命中），
--一加载就报 "CallCardFunction(c27118421.initial_effect): attempt to call an error function"。
--这里改用**本包自己在用**的两套接口重写：
--  · 素材声明 —— Auxiliary.AddFusionProcFunRep（procedure.lua:1283，老 API 的 aux.AddFusionProc* 系列）
--  · 融合召唤流程 —— FusionSpell.CreateSummonEffect（procedure.lua:2422，超融合 c48130397、c17350692 同款）
--卡文（cards.cdb 15276 版）：
--  场上的暗属性怪兽×2。这张卡在规则上也当作「捕食植物」卡。
--  ① 这张卡特殊召唤的场合，以场上 1 只其他的表侧表示怪兽为对象才能发动。
--     那只怪兽的攻击力变成 0，效果无效化，变成暗属性。
--  ② 对方把效果发动时才能发动。包含这张卡的自己·对方场上的暗属性怪兽作为融合素材，
--     把 1 只暗属性融合怪兽融合召唤。
local s,id,o=GetID()
function s.initial_effect(c)
	c:EnableReviveLimit()
	--融合素材：场上的暗属性怪兽×2（第 4 个参数 true＝允许用「融合素材代用」类效果）
	aux.AddFusionProcFunRep(c,s.matfilter,2,true)

	--① 特殊召唤成功：对象怪兽攻击力变 0、效果无效、变成暗属性
	local e1=Effect.CreateEffect(c)
	e1:SetDescription(aux.Stringid(id,0))
	e1:SetCategory(CATEGORY_ATKCHANGE+CATEGORY_DISABLE)
	e1:SetType(EFFECT_TYPE_SINGLE+EFFECT_TYPE_TRIGGER_O)
	e1:SetProperty(EFFECT_FLAG_DELAY+EFFECT_FLAG_CARD_TARGET)
	e1:SetCode(EVENT_SPSUMMON_SUCCESS)
	e1:SetCountLimit(1,{id,0})
	e1:SetTarget(s.atktg)
	e1:SetOperation(s.atkop)
	c:RegisterEffect(e1)

	--② 对手发动效果时：把**包含这张卡**的双方场上暗属性怪兽作素材，融合召唤 1 只暗属性融合怪兽
	--   pre_select_mat_location / pre_select_mat_opponent_location 把素材限定在双方怪兽区（＝"场上的"），
	--   gc 指定必须包含这张卡本身，fusfilter 限定只能是暗属性融合怪兽。
	local e2=FusionSpell.CreateSummonEffect(c,{
		fusfilter=s.fusfilter,
		matfilter=s.darkfilter,
		pre_select_mat_location=LOCATION_MZONE,
		pre_select_mat_opponent_location=LOCATION_MZONE,
		gc=function(e) return e:GetHandler() end
	})
	e2:SetDescription(aux.Stringid(id,1))
	e2:SetType(EFFECT_TYPE_QUICK_O)
	e2:SetCode(EVENT_CHAINING)
	e2:SetRange(LOCATION_MZONE)
	e2:SetCountLimit(1,{id,1})
	e2:SetCondition(s.fscon)
	c:RegisterEffect(e2)
end
s.listed_series={0x10f3}   -- 规则上也当作「捕食植物」卡

--融合素材过滤：暗属性、在场上（限制"场上的"由 pre_select_mat_location 保证，这里再兜一层）
function s.matfilter(c,fc,sub)
	return c:IsAttribute(ATTRIBUTE_DARK) and c:IsOnField()
end

--融合召唤目标：只能是暗属性融合怪兽
function s.fusfilter(c)
	return c:IsType(TYPE_FUSION) and c:IsAttribute(ATTRIBUTE_DARK)
end

--素材的强限制：只用暗属性怪兽（卡文写死了属性）
function s.darkfilter(c,e,tp)
	return c:IsAttribute(ATTRIBUTE_DARK)
end

--② 的发动条件：对手（或对手的卡）发动效果
function s.fscon(e,tp,eg,ep,ev,re,r,rp)
	return rp==1-tp
end

--① 可选的对象：场上任意表侧表示怪兽（排除自己）
function s.atkfilter(c)
	return c:IsFaceup() and c:IsType(TYPE_MONSTER)
end

function s.atktg(e,tp,eg,ep,ev,re,r,rp,chk,chkc)
	local c=e:GetHandler()
	if chkc then return chkc:IsLocation(LOCATION_MZONE) and s.atkfilter(chkc) and chkc~=c end
	if chk==0 then return Duel.IsExistingTarget(s.atkfilter,tp,LOCATION_MZONE,LOCATION_MZONE,1,c) end
	Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_TARGET)
	local g=Duel.SelectTarget(tp,s.atkfilter,tp,LOCATION_MZONE,LOCATION_MZONE,1,1,c)
	Duel.SetOperationInfo(0,CATEGORY_DISABLE,g,1,0,0)
end

function s.atkop(e,tp,eg,ep,ev,re,r,rp)
	local c=e:GetHandler()
	local tc=Duel.GetFirstTarget()
	if not tc:IsRelateToEffect(e) or not tc:IsFaceup() then return end
	--攻击力变成 0
	local e1=Effect.CreateEffect(c)
	e1:SetType(EFFECT_TYPE_SINGLE)
	e1:SetProperty(EFFECT_FLAG_CANNOT_DISABLE)
	e1:SetCode(EFFECT_SET_ATTACK_FINAL)
	e1:SetValue(0)
	e1:SetReset(RESET_EVENT+RESETS_STANDARD)
	tc:RegisterEffect(e1)
	--变成暗属性
	local e2=Effect.CreateEffect(c)
	e2:SetType(EFFECT_TYPE_SINGLE)
	e2:SetProperty(EFFECT_FLAG_CANNOT_DISABLE)
	e2:SetCode(EFFECT_CHANGE_ATTRIBUTE)
	e2:SetValue(ATTRIBUTE_DARK)
	e2:SetReset(RESET_EVENT+RESETS_STANDARD)
	tc:RegisterEffect(e2)
	--效果无效化（老 API 写法：EFFECT_DISABLE + EFFECT_DISABLE_EFFECT，本引擎没有 Card.NegateEffects）
	local e3=Effect.CreateEffect(c)
	e3:SetType(EFFECT_TYPE_SINGLE)
	e3:SetProperty(EFFECT_FLAG_CANNOT_DISABLE)
	e3:SetCode(EFFECT_DISABLE)
	e3:SetReset(RESET_EVENT+RESETS_STANDARD)
	tc:RegisterEffect(e3)
	local e4=Effect.CreateEffect(c)
	e4:SetType(EFFECT_TYPE_SINGLE)
	e4:SetProperty(EFFECT_FLAG_CANNOT_DISABLE)
	e4:SetCode(EFFECT_DISABLE_EFFECT)
	e4:SetReset(RESET_EVENT+RESETS_STANDARD)
	tc:RegisterEffect(e4)
end
