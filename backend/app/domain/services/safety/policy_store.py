"""Persistence and bootstrap for administrator-managed safety rules."""

import re
import unicodedata
from datetime import datetime, UTC

from app.application.errors.exceptions import BadRequestError
from app.domain.models.safety import SafetyRule
from app.infrastructure.models.documents import SafetyRuleDocument, SafetyRuleSeedStateDocument


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").strip().casefold().split())


DEFAULT_RULES = [
    SafetyRule(
        name="恶意软件与危险执行",
        description="拦截恶意软件名称与下载、安装、执行等高风险组合。",
        category="malware_or_dangerous_execution",
        risk_level="critical",
        match_type="regex",
        patterns=[r"(?is)(?=.*(?:木马|远控|恶意软件|病毒|勒索软件|后门|trojan|ransomware|keylogger|reverse\s+shell|remote\s+access\s+trojan|\brat\b))(?=.*(?:下载|安装|运行|执行|启动|部署|持久化|免杀|download|install|execute|run|deploy))"],
        reason="请求同时包含恶意软件相关内容和下载、安装、运行或持久化等执行意图。",
        suggestion="请改为合法的软件分析、漏洞修复或防御性安全需求，不要要求执行未知软件。",
        priority=10,
        built_in=True,
    ),
    SafetyRule(
        name="危险命令管道",
        description="拦截下载后直接交给 Shell、PowerShell 编码执行等命令。",
        category="malware_or_dangerous_execution",
        risk_level="critical",
        match_type="regex",
        patterns=[r"(?i)(?:curl|wget)\b[^\n]{0,500}\|\s*(?:ba|z)?sh\b", r"(?i)powershell[^\n]{0,300}(?:-enc|-e\b)", r"(?i)chmod\s+\+x[^\n]{0,300}(?:/tmp|/var/tmp|curl|wget)"],
        reason="请求包含下载内容后直接执行、编码 PowerShell 或授予未知脚本执行权限的命令。",
        suggestion="请去除直接执行未知代码的步骤，改用受控、已授权且可审计的安全测试方式。",
        priority=10,
        built_in=True,
    ),
    SafetyRule(
        name="提示词注入与越狱",
        description="拦截要求忽略或绕过系统安全规则的请求。",
        category="prompt_injection_or_jailbreak",
        risk_level="high",
        match_type="regex",
        patterns=[r"(?is)(?:越狱|jailbreak|\bdan\b|忽略|无视|忘记|绕过)[^\n]{0,80}(?:系统|安全|规则|指令|提示词|限制|之前|ignore|system|policy|instruction)"],
        reason="请求包含绕过系统指令、安全规则或使用限制的意图。",
        suggestion="请直接描述需要完成的业务目标，不要要求忽略、覆盖或绕过系统规则。",
        priority=20,
        built_in=True,
    ),
    SafetyRule(
        name="凭证与密钥获取",
        description="拦截读取、导出或发送真实凭证的请求。",
        category="credential_or_secret_theft",
        risk_level="critical",
        match_type="regex",
        patterns=[r"(?is)(?:读取|窃取|泄露|导出|发送|上传|打印|显示|steal|exfiltrat|dump)[^\n]{0,100}(?:密码|密钥|token|令牌|api[_ -]?key|环境变量|cookie|凭证|secret)"],
        reason="请求包含读取、导出或发送密码、密钥、令牌等敏感凭证的意图。",
        suggestion="请使用脱敏示例，或仅描述合规的凭证管理与轮换流程。",
        priority=20,
        built_in=True,
    ),
    SafetyRule(
        name="网络攻击与滥用",
        description="拦截未授权入侵、漏洞利用和钓鱼等网络攻击行为。",
        category="cyber_abuse",
        risk_level="high",
        match_type="keyword",
        patterns=["提权", "攻击网站", "入侵", "渗透", "漏洞利用", "绕过认证", "盗号", "钓鱼", "ddos", "rce", "exploit", "credential stuffing", "phishing", "privilege escalation"],
        reason="请求包含未授权入侵、漏洞利用、钓鱼或其他网络攻击行为。",
        suggestion="请将需求限定为已授权环境中的防御、检测、修复或合规测试，并说明授权范围。",
        priority=30,
        built_in=True,
    ),
    SafetyRule(
        name="色情与淫秽内容",
        description="拦截色情、淫秽和露骨性内容。",
        category="sexual_or_obscene",
        risk_level="high",
        match_type="keyword",
        patterns=["色情", "淫秽", "裸聊", "露骨性", "性爱", "porn", "explicit sex", "sexual content"],
        reason="请求包含系统策略限制的色情或淫秽内容。",
        suggestion="请移除露骨或淫秽内容，改为健康、教育或合规场景下的描述。",
        priority=30,
        built_in=True,
    ),
    SafetyRule(
        name="政治与敏感内容",
        description="拦截管理员定义的政治、政府和其他敏感关键词。",
        category="political_or_sensitive",
        risk_level="high",
        match_type="keyword",
        patterns=["政治", "政府", "政党", "选举", "领导人", "国家机密", "政治敏感", "government", "election", "political"],
        reason="请求包含政治、政府或其他受限制的敏感内容。",
        suggestion="请移除政治或政府敏感内容，或将需求改写为不涉及敏感对象的中性业务任务。",
        priority=30,
        built_in=True,
    ),
]

SEED_VERSION = 2


async def ensure_safety_rule_seeds() -> None:
    state = await SafetyRuleSeedStateDocument.find_one(SafetyRuleSeedStateDocument.state_id == "safety_rule_seed_state")
    if state and state.version >= SEED_VERSION:
        return
    for rule in DEFAULT_RULES:
        existing = await SafetyRuleDocument.find_one(SafetyRuleDocument.name_key == normalize_name(rule.name))
        if existing:
            # Version 2 corrects the escaping of the initial bundled regexes.
            # This migration only affects the short-lived v1 seed data.
            if not state or state.version < 2:
                existing.patterns = rule.patterns
                existing.updated_at = datetime.now(UTC)
                await existing.save()
            continue
        doc = SafetyRuleDocument.from_domain(rule)
        doc.name_key = normalize_name(rule.name)
        await doc.insert()
    if state:
        state.version = SEED_VERSION
        await state.save()
    else:
        await SafetyRuleSeedStateDocument(state_id="safety_rule_seed_state", version=SEED_VERSION).insert()


class SafetyPolicyStore:
    async def list_enabled(self) -> list[SafetyRule]:
        docs = await SafetyRuleDocument.find(SafetyRuleDocument.enabled == True).sort([("priority", 1), ("name", 1)]).to_list()
        return [doc.to_domain() for doc in docs]

    async def list(self, query: str | None, include_disabled: bool, limit: int, offset: int) -> tuple[list[SafetyRule], int]:
        docs = await SafetyRuleDocument.find().sort([("priority", 1), ("name", 1)]).to_list()
        if not include_disabled:
            docs = [doc for doc in docs if doc.enabled]
        if query:
            needle = query.casefold().strip()
            docs = [doc for doc in docs if needle in doc.name.casefold() or needle in doc.description.casefold() or needle in doc.category.casefold()]
        total = len(docs)
        return [doc.to_domain() for doc in docs[offset : offset + min(limit, 200)]], total

    async def create(self, rule: SafetyRule, actor_user_id: str) -> SafetyRule:
        rule.created_by = actor_user_id
        rule.created_at = datetime.now(UTC)
        rule.updated_at = rule.created_at
        self._validate(rule)
        if await SafetyRuleDocument.find_one(SafetyRuleDocument.name_key == normalize_name(rule.name)):
            raise BadRequestError("安全规则名称已存在")
        doc = SafetyRuleDocument.from_domain(rule)
        doc.name_key = normalize_name(rule.name)
        await doc.insert()
        return doc.to_domain()

    async def update(self, rule_id: str, rule: SafetyRule) -> SafetyRule:
        doc = await SafetyRuleDocument.find_one(SafetyRuleDocument.rule_id == rule_id)
        if not doc:
            raise BadRequestError("安全规则不存在")
        self._validate(rule)
        duplicate = await SafetyRuleDocument.find_one(SafetyRuleDocument.name_key == normalize_name(rule.name))
        if duplicate and duplicate.rule_id != rule_id:
            raise BadRequestError("安全规则名称已存在")
        rule.id = rule_id
        rule.created_by = doc.created_by
        rule.created_at = doc.created_at
        rule.updated_at = datetime.now(UTC)
        doc.name = rule.name
        doc.name_key = normalize_name(rule.name)
        doc.description = rule.description
        doc.category = rule.category
        doc.risk_level = rule.risk_level
        doc.match_type = rule.match_type
        doc.patterns = rule.patterns
        doc.enabled = rule.enabled
        doc.reason = rule.reason
        doc.suggestion = rule.suggestion
        doc.priority = rule.priority
        doc.updated_at = rule.updated_at
        await doc.save()
        return doc.to_domain()

    async def delete(self, rule_id: str) -> None:
        doc = await SafetyRuleDocument.find_one(SafetyRuleDocument.rule_id == rule_id)
        if not doc:
            raise BadRequestError("安全规则不存在")
        await doc.delete()

    @staticmethod
    def _validate(rule: SafetyRule) -> None:
        if not rule.name.strip():
            raise BadRequestError("安全规则名称不能为空")
        if not rule.patterns or any(not item.strip() for item in rule.patterns):
            raise BadRequestError("至少填写一个非空匹配内容")
        if rule.match_type == "regex":
            for pattern in rule.patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise BadRequestError(f"正则表达式无效：{exc}") from exc
        if len(rule.patterns) > 100:
            raise BadRequestError("单条规则最多支持 100 个匹配内容")


_store = SafetyPolicyStore()


def get_safety_policy_store() -> SafetyPolicyStore:
    return _store
