
"""Install the project-local Unity SOPs through the supported LoopForge API."""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, HTTPRedirectHandler, Request, build_opener

from integrations.loopforge.worker_client import save_json
from worker_config import FEATURE_TOTAL_REWORK_LIMIT
TAG = "unity-worker-formal"
WINDOWS_PLANNING_TAG = "unity-worker-windows-planning"
STARTERS = (
    ("feature", "Unity 功能交付", "generic-backend-delivery", "backend"),
    ("ui", "Unity UI 交付", "generic-frontend-delivery", "frontend"),
    ("product", "Unity 产品方案", "generic-product-delivery", "product"),
    ("issue-fix", "Unity 缺陷修复", "issue-fix-production", "repair"),
)
CLARIFY = {
    "feature": "确认涉及的 Unity 模块、数据边界、兼容性和可复核的验收条件。",
    "ui": "确认入口、交互状态、异常反馈、桌面尺寸和本地化。写明需要人工观察的界面证据。",
    "product": "区分需求目标、范围、用户场景、交互规则和验收标准，记录依赖与未决问题。",
    "issue-fix": "写明版本、复现步骤、预期与实际、已有证据、影响范围和最小回归场景。",
}
CODE_PLAN = {
    "feature": "给出受影响模块和最小实现步序，并覆盖兼容性及相关回归。",
    "ui": "标明 Scene、Prefab、Canvas/RectTransform、域重载及本地化边界。必须提供定向 EditMode 和 PlayMode 夹具。",
    "issue-fix": "写明复现、根因证据或待验证假设、最小修复，以及能捕获回归的 fixture。涉及帧时序时加入 PlayMode。",
}
PRODUCT_ROLE = {
    "clarify": "在运行根目录写 requirement.md，明确产品目标、范围、用户场景和验收标准。",
    "design": "依据上游需求写 design.md，给出用户流程、交互规则、业务边界和验证计划。",
    "review": "独立核对需求和方案，在 review.md 中记录问题位置、影响和修改建议，并明确标记阻断项。",
    "deliver": "依据验收结论与上游文档写 delivery.md，汇总交付物、实际验收结果和后续开发输入。",
}


@dataclass(frozen=True)
class Target:
    project_id: str
    sandbox_profile: str
    worker_url: str

    @property
    def execution_id(self):
        return f"{self.project_id}-worker-execution"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call_factory(token, base):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Origin": base}

    def call(method, path, value=None, *, missing=False):
        body = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        try:
            with opener.open(Request(base + path, body, headers, method=method), timeout=45) as response:
                return json.load(response)
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"{method} {path}: HTTP {error.code}") from None

    return call


def artifact(name, path, media="text/markdown"):
    return {"name": name, "path": path, "mediaType": media, "required": True}


def agent_node(node_id, title, role_id, output):
    return {"id": node_id, "name": title, "type": "agent", "agentId": role_id,
            "artifactOutputs": output, "maxAttempts": 1}


def script_node(node_id, title, body, output=None):
    node = {"id": node_id, "name": title, "type": "script", "scriptLanguage": "shell",
            "script": "exec /usr/bin/python3 -I -c " + shlex.quote(body), "maxAttempts": 1}
    if output:
        node["artifactOutputs"] = [artifact(output, output + ".json", "application/json")]
    return node


def approval(node_id, title, prompt, reject_to=None):
    node = {"id": node_id, "name": title, "type": "approval", "taskPrompt": prompt, "maxAttempts": 1}
    if reject_to:
        node.update(rejectTo=reject_to, maxReworks=3)
    return node


def render(*args):
    return subprocess.check_output([sys.executable, str(Path(__file__).with_name("worker_client.py")), "render", *args], encoding="utf-8")


def pack_script(assemblies, ui):
    return f"ASSEMBLIES = {assemblies!r}\nREQUIRE_PLAYMODE = {ui!r}\n" + r'''
import json
import os
import re
from pathlib import Path
from uuid import uuid4

workspace = Path.cwd().resolve()
matches = list(Path(".agentflow/inputs").glob("**/unity-plan-draft.json"))
if len(matches) != 1:
    raise SystemExit("需要唯一的上游 unity-plan-draft.json 产物")
source = matches[0]
if not source.is_file() or source.stat().st_size > 96000:
    raise SystemExit("方案产物过大")
draft = json.loads(source.read_text(encoding="utf-8-sig"))
required = {"requirement", "plan", "acceptance_criteria", "test_selection"}
if not isinstance(draft, dict) or set(draft) != required:
    raise SystemExit("计划 JSON 字段必须为 requirement、plan、acceptance_criteria、test_selection")
for key in ("requirement", "plan"):
    value = draft[key]
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 20000 or "\0" in value:
        raise SystemExit("需求和实施计划不能为空，长度上限是 20000")
    draft[key] = value.strip()
criteria = draft["acceptance_criteria"]
if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 50
        or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 2000 for item in criteria)):
    raise SystemExit("提供 1 到 50 条可核对的验收标准")
selection = draft["test_selection"]
if not isinstance(selection, dict) or not selection.get("editmode") or (REQUIRE_PLAYMODE and not selection.get("playmode")):
    raise SystemExit("缺少此 SOP 所需的定向测试范围")
for mode, scopes in selection.items():
    if mode not in ASSEMBLIES or not isinstance(scopes, list) or not 1 <= len(scopes) <= 8:
        raise SystemExit("测试模式或范围数量无效")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"assembly", "fixtures"}:
            raise SystemExit("测试范围必须包含 assembly 和 fixtures")
        assembly, names = scope["assembly"], scope["fixtures"]
        if not isinstance(assembly, str) or assembly not in ASSEMBLIES[mode] or assembly in seen:
            raise SystemExit("程序集不在许可清单中或重复")
        seen.add(assembly)
        if not isinstance(names, list) or not 1 <= len(names) <= 20 or any(
            not isinstance(name, str) or len(name) > 220 or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:[.+][A-Za-z_][A-Za-z0-9_]*)*", name)
            or name.rsplit(".", 1)[-1].lower() == "shippedassettests" for name in names
        ) or len(set(names)) != len(names):
            raise SystemExit("请提供不重复的完整测试类名，禁止正则与资产扫描")
draft["project_rules"] = "遵循 Git 根目录与 client/ 中的 AGENTS.md 及子目录规则。工作目录是 Git 根，Unity 工程位于 client/。保留 .meta/GUID，禁止全量资产扫描和修改 Git 设置。"
run_id = os.environ.get("AGENTFLOW_RUN_ID", "").strip()
draft["request_id"] = str(uuid4())
if run_id:
    draft["workflow_run_id"] = run_id
destination = workspace / "unity-plan.json"
temp = workspace / ".unity-plan.tmp"
if destination.is_symlink() or temp.is_symlink():
    raise SystemExit("拒绝链接方案产物")
temp.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temp.replace(destination)
print("已验证方案、验收标准和定向测试范围，等待人工审批。")
'''


HANDOFF = r'''
from pathlib import Path

matches = list(Path(".agentflow/inputs").glob("**/unity-plan.json"))
if len(matches) != 1 or not matches[0].is_file() or matches[0].stat().st_size > 96000:
    raise SystemExit("缺少唯一有效的已审批方案产物")
print("已挂载审批通过的冻结方案产物")
'''


def definition(target, identifier, title, lane, nodes, credentials=()):
    for index, node in enumerate(nodes):
        node["position"] = {"x": 80 + index * 240, "y": 160}
    return {"id": identifier, "projectId": target.project_id, "name": title, "sopLane": lane, "status": "draft",
            "sandboxProfileId": target.sandbox_profile, "tags": [TAG], "projectCredentialIds": list(credentials),
            "nodes": nodes, "edges": [
                {"id": f"e{index}", "source": a["id"], "target": b["id"], "when": "success"}
                for index, (a, b) in enumerate(zip(nodes, nodes[1:]))
            ]}


def save_workflow(target, api, value, *, publish=False):
    route = "/api/workflows/" + value["id"]
    live = api("GET", route, missing=True)
    if live:
        if live.get("projectId") != target.project_id or TAG not in live.get("tags", []):
            raise RuntimeError(f"Workflow id is already in use: {value['id']}")
        if live["status"] == "published" and not publish:
            return live
        tags = [tag for tag in live.get("tags", []) if tag != WINDOWS_PLANNING_TAG]
        tags = list(dict.fromkeys([*tags, *value["tags"]]))
        value = {**live, **value, "revision": live["revision"], "status": live["status"], "tags": tags}
    else:
        value["revision"] = 0
    saved = api("PUT", "/api/workflows", value)
    return api("POST", route + "/publish", {}) if publish else saved


def planning_role(target, api, starter, workflow_title, step, title, prompt, output):
    identifier = f"{target.project_id}-{starter}-{step}"
    old = api("GET", "/api/agents/" + identifier, missing=True)
    if old and old.get("projectId") != target.project_id:
        raise RuntimeError(f"Agent id is in use: {identifier}")
    if old is None:
        if starter == "product":
            role_prompt = (
                f"{PRODUCT_ROLE[step]}\n\n任务需求：{{{{input}}}}\n\n"
                "这条 SOP 根据需求、上游文档和验收记录交付产品文档。"
                "把需要核对代码、运行 Unity 或修改项目的事项列为后续开发输入。"
                "把假设、证据缺口和评审发现写入本节点产物，供正式人工验收或后续开发查阅。"
                "交付合格的节点产物后提交 pass，节点终态表示此阶段文档处理完成。\n" + prompt
            )
        else:
            objective = {
                "clarify": "在运行根目录写 requirement.md，明确范围、兼容性和可复核的验收标准。",
                "design": "根据上游需求，在运行根目录写 design.md，说明最小实施步骤、风险和验证计划。",
            }[step]
            role_prompt = (
                f"{objective}\n\n任务需求：{{{{input}}}}\n\n"
                "本节点在 WSL 规划工作区依据需求与上游文档交付方案。Windows Worker 使用分配的 Unity 工作副本，"
                "负责读取源码、核对现有接口、编辑项目、运行 Unity 测试和 Git 交付。"
                "规划工作区可以为空。把源码位置、现有接口、权限实现和测试程序集等未核实的细节列为 Worker 的待核对项。"
                "仅因规划工作区缺少仓库或源码时，按现有需求完成产物并提交节点终态 pass，继续方案校验和正式审批。"
                "仅在需求目标、业务行为或验收规则必须由用户决定时请求确认。"
                "区分已知事实、方案假设与待验证内容。\n" + prompt
            )
        value = {
            "id": identifier, "name": title, "projectId": target.project_id, "description": f"{TAG} · {workflow_title}",
            "prompt": role_prompt,
            "rules": [], "skills": [], "mcpServers": [], "artifactOutputs": output, "verification": [],
        }
        api("PUT", "/api/agents", value)
    return identifier


def starter_workflow(target, api, row, assemblies):
    key, title, _, lane = row
    clarify = [artifact("clarify", "requirement.md")]
    design = [artifact("design", "design.md")]
    planner_prompt = ""
    if key != "product":
        design.append(artifact("unity-plan-draft", "unity-plan-draft.json", "application/json"))
        planner_prompt = (
            f"{CODE_PLAN[key]}\n同时在运行根目录写严格 JSON 文件 unity-plan-draft.json。"
            '只含 requirement（字符串）、plan（字符串）、acceptance_criteria（字符串数组）、test_selection。'
            'test_selection 的结构为 {"editmode":[{"assembly":"Tests.EditMode","fixtures":["命名空间.测试类"]}]}。'
            "使用真实、完整的 fixture 类名。为本任务拟新增的测试也写出类名。需要 PlayMode 时另加 playmode，"
            "禁止任意命令、路径或全量扫描。部署许可清单为 " + json.dumps(assemblies, ensure_ascii=False)
            + "。需求缺失的关键行为请在设计文档中标注，交给正式的方案审批节点。"
            "以目标 Unity 工程的测试目录和程序集定义为准。"
        )
    else:
        planner_prompt = "产出产品目标、范围、用户流程、验收清单和可移交的后续开发需求，供独立评审。"
    clarify_id = planning_role(target, api, key, title, "clarify", f"{title} · 需求定型", CLARIFY[key], clarify)
    design_id = planning_role(target, api, key, title, "design", f"{title} · 方案设计", planner_prompt, design)
    nodes = [agent_node("clarify", "需求定型", clarify_id, clarify), agent_node("design", "方案设计", design_id, design)]
    if key == "product":
        review = [artifact("review", "review.md")]
        deliver = [artifact("deliver", "delivery.md")]
        review_id = planning_role(target, api, key, title, "review", f"{title} · 独立评审",
                                  "核对方案与需求、验收标准是否一致，并列出未决事项和修改建议。", review)
        deliver_id = planning_role(target, api, key, title, "deliver", f"{title} · 交付汇总",
                                   "基于已审批的方案和审查产物输出产品方案交付摘要与下一步开发输入。", deliver)
        nodes += [
            agent_node("review", "独立评审", review_id, review),
            approval("human-accept", "人工验收", "核对产品方案、独立评审和验收标准。", "design"),
            agent_node("deliver", "交付汇总", deliver_id, deliver),
        ]
    else:
        nodes += [
            script_node("validate-plan", "校验 Unity 实施计划", pack_script(assemblies, key == "ui"), "unity-plan"),
            approval("plan-approval", "审批实施方案", "核对需求范围、验收标准和实施步骤，确认定向测试程序集与 fixture。批准后由正式 Windows Worker 执行。", "design"),
            script_node("handoff", "交接已审批方案", HANDOFF),
            {"id": "worker", "name": "Unity Worker 开发验证与交付", "type": "sop", "sopWorkflowId": target.execution_id,
             "maxAttempts": 1},
        ]
    return definition(target, f"{target.project_id}-{key}", title, lane, nodes)


def worker_credential(target, api, worker_token, *, publish=False):
    return upsert_credential(target, api, "Unity Worker 正式执行",
                             {"UNITY_WORKER_URL": target.worker_url, "UNITY_WORKER_TOKEN": worker_token},
                             publish=publish)


def upsert_credential(target, api, name, variables, *, publish=False):
    path = f"/api/projects/{target.project_id}/credentials"
    result = api("GET", path)
    if not result.get("canManage"):
        raise RuntimeError("项目凭据需要目标项目 Owner 或平台管理员")
    current = next((entry for entry in result["credentials"] if entry["name"] == name), None)
    if current is not None and current["kind"] != "custom":
        raise RuntimeError("项目内凭据名称冲突")
    if current and not publish:
        return current["id"]
    value = {"name": name, "kind": "custom",
             "fields": [{"name": name, "value": content} for name, content in variables.items()]}
    if current:
        return api("PUT", path + "/" + current["id"], value)["id"]
    return api("POST", path, value)["id"]


def execution_workflow(target, credential, pool_id=None, *, approved_plan=".agentflow/inputs/**/unity-plan.json"):
    queue = ("--pool", pool_id, "--queue-wait-seconds", "18000") if pool_id else ()
    nodes = [
        script_node("develop", "Codex 实现、Unity 定向测试与独立审查",
                    render("develop", "--input", approved_plan,
                           "--output", "unity-task.json", "--wait-seconds", "18000", *queue),
                    "unity-task"),
        approval("delivery-approval", "审批交付版本",
                 "核对 unity-task.json 的任务 ID、revision、变更清单、编译与测试 XML、独立审查。UI 相关任务还需核对实际界面效果。"
                 "使用 Worker 仓库的 scripts/approve-delivery.ps1 -TaskID <task_id> -Revision <revision> -Approver <姓名> 签批同一版本，再批准本节点。"
                 "需要改动时保留现场，归档并释放后重新提交方案。"),
        script_node("deliver", "提交并推送任务分支",
                    render("deliver", "--from-artifact", ".agentflow/inputs/**/unity-task.json",
                           "--approved-plan", approved_plan,
                           "--output", "unity-delivery.json"), "unity-delivery"),
        script_node("release", "释放 Unity Worker",
                    render("release", "--from-artifact", ".agentflow/inputs/**/unity-delivery.json",
                           "--approved-plan", approved_plan,
                           "--output", "unity-release.json"), "unity-release"),
    ]
    return definition(target, target.execution_id, "Unity Worker 正式执行 · 子 SOP", "other", nodes, (credential,))


def windows_workflow(target, row, worker_credentials, context_credential, pool_id):
    key, title, _, lane = row
    planned = ".agentflow/inputs/*worker-plan/unity-planning.json"
    accepted = ".agentflow/inputs/*handoff/unity-plan.json"
    nodes = [
        script_node("worker-plan", "Windows Worker 需求定型、源码调查与方案",
                    render("plan", "--planning-profile", key, "--pool", pool_id, "--queue-wait-seconds", "18000",
                           "--wait-seconds", "18000", "--output", "unity-planning.json"), "unity-planning"),
        approval("plan-approval", "审批实施方案",
                 "核对 unity-planning.json 中的实际代码位置、Git 基础提交、Unity 基线日志与测试、待验证项及验收标准。"
                 "批准绑定到该任务、槽位和这版方案。若需修改，用打回功能保留同一工作区并修订方案。", "worker-plan"),
        script_node("handoff", "核对审批与任务绑定",
                    render("accept-planning", "--input", planned, "--output", "unity-plan.json"), "unity-plan"),
        *execution_workflow(target, worker_credentials, pool_id, approved_plan=accepted)["nodes"],
    ]
    workflow = definition(target, f"{target.project_id}-{key}", title, lane, nodes, (worker_credentials, context_credential))
    workflow["tags"].append(WINDOWS_PLANNING_TAG)
    return workflow


def staged_feature_workflow(target, worker_credential, context_credential, pool_id):
    if not pool_id:
        raise ValueError("Staged feature delivery requires a named pool")
    key, title, _, lane = STARTERS[0]
    stages = (
        ("clarify", "需求定型", "feature-clarify", "requirement", "unity-scope", "clarification"),
        ("design", "方案设计", "feature-design", "design", "unity-planning", "clarification"),
        ("implement", "开发实现", "feature-implement", "implementation", "unity-implementation", "development"),
        ("review", "独立评审", "feature-review", "review", "unity-review", "development"),
        ("test", "测试验证", "feature-test", "test-report", "unity-verification", "testing"),
        ("mr", "发起 MR", "feature-mr", "mr-body", "unity-delivery", "none"),
        ("deliver", "交付汇总", "feature-delivery", "delivery", None, "none"),
    )
    nodes = []
    for node_id, label, action, report, evidence, metric_stage in stages:
        args = ("--queue-wait-seconds", "5400") if node_id == "clarify" else ()
        node = script_node(node_id, label, render(action, "--pool", pool_id, "--follow", *args))
        node.update(artifactOutputs=[artifact(report, report + ".md")], metricStage=metric_stage,
                    maxAttempts=2, maxReworks=0)
        if node_id in {"review", "test"}:
            node.update(onFailure="fail", maxReworks=FEATURE_TOTAL_REWORK_LIMIT)
        if evidence:
            node["artifactOutputs"].append(artifact(evidence, evidence + ".json", "application/json"))
        nodes.append(node)
    gate = approval("accept", "人工验收",
                    "打开待审交付物中的测试报告，使用报告开头的任务分支和提交 SHA 验收。"
                    "核对需求、独立评审、Unity 测试和未覆盖项。"
                    "批准后发起 MR；需要修改时填写完整意见并打回开发实现。", "implement")
    gate["maxReworks"] = FEATURE_TOTAL_REWORK_LIMIT
    nodes.insert(5, gate)
    return definition(target, f"{target.project_id}-{key}", title, lane, nodes, (worker_credential, context_credential))


def staged_credential_ids(api, project_path, workflow):
    refs = workflow.get("projectCredentialIds", [])
    items = {entry["id"]: entry for entry in api("GET", project_path + "/credentials")["credentials"]}
    bound = {}
    for ref in refs:
        entry = items.get(ref, {})
        if entry.get("kind") != "custom":
            raise RuntimeError("The feature workflow must bind two existing custom project credentials")
        bound[frozenset(entry.get("variables", []))] = ref
    worker = frozenset(("UNITY_WORKER_URL", "UNITY_WORKER_TOKEN"))
    context = frozenset(("UNITY_PLANNING_CONTEXT_TOKEN",))
    if len(refs) != 2 or bound.keys() != {worker, context}:
        raise RuntimeError("The feature workflow must bind its existing Worker and planning context credentials")
    return bound[worker], bound[context]


def workflow_content(workflow, *, publishing=False):
    ignored = {"revision", "publishedRevision", "updatedAt"}
    if publishing:
        ignored.update(("status", "version"))
    content = {key: value for key, value in workflow.items() if key not in ignored}
    # Some platform releases omit the zero value for script nodes.
    content["nodes"] = [{"maxReworks": 0, **node} for node in content["nodes"]]
    return content


def install_staged_feature(target, api, pool_id, output_dir, *, publish=False, expected_revision=None):
    identifier = target.project_id + "-feature"
    project_path = "/api/projects/" + target.project_id
    project = api("GET", project_path)
    if project.get("id") != target.project_id:
        raise RuntimeError("LoopForge project does not match --project-id")
    route = "/api/workflows/" + identifier
    live = api("GET", route)
    revision = live.get("revision")
    if (live.get("id") != identifier or live.get("projectId") != target.project_id
            or live.get("generic") or TAG not in live.get("tags", [])
            or live.get("status") not in ("draft", "published") or not isinstance(revision, int) or revision < 1
            or live.get("autoSelectSandbox") or not live.get("sandboxProfileId")):
        raise RuntimeError(f"Expected an existing draft or published project feature workflow with a fixed sandbox: {identifier}")
    if expected_revision is not None and expected_revision != revision:
        raise RuntimeError(f"Feature workflow revision changed: expected {expected_revision}, found {revision}")
    worker, context = staged_credential_ids(api, project_path, live)
    live_target = Target(target.project_id, live["sandboxProfileId"], target.worker_url)
    graph = staged_feature_workflow(live_target, worker, context, pool_id)
    value = {**live, "nodes": graph["nodes"], "edges": graph["edges"]}
    # Keep the exact input revision for recovery, even on repeated deployments.
    backup = output_dir / f"{identifier}-before.json"
    if backup.exists():
        if json.loads(backup.read_text(encoding="utf-8")) != live:
            backup = output_dir / f"{identifier}-before-r{revision}.json"
    if not backup.exists():
        save_json(backup, live)
    report = {"id": identifier, "base_revision": revision, "pool": pool_id, "backup": str(backup)}
    if not publish:
        preview = output_dir / f"{identifier}-staged-preview.json"
        save_json(preview, value)
        return {**report, "mode": "preview", "preview": str(preview)}

    # LoopForge checks the revision in the PUT body and reports a stale write as HTTP 409.
    saved = api("PUT", "/api/workflows", value)
    if (saved.get("revision") not in (revision, revision + 1)
            or workflow_content(saved) != workflow_content(value)):
        raise RuntimeError(f"The saved feature workflow does not match the requested graph: {identifier}")
    if api("GET", route) != saved:
        raise RuntimeError(f"Feature workflow changed before publishing: {identifier}")
    # Publish has no revision precondition. Detect an intervening edit through its response.
    published = api("POST", route + "/publish", {})
    if (published.get("status") != "published"
            or published.get("revision") not in (saved["revision"], saved["revision"] + 1)
            or published.get("publishedRevision", published.get("revision")) != published.get("revision")
            or workflow_content(published, publishing=True) != workflow_content(saved, publishing=True)):
        raise RuntimeError(f"The published feature workflow does not match the saved graph: {identifier}")
    return {**report, "mode": "published", "revision": published["revision"]}


def preflight_executors(target, api, *, windows_planning=False):
    for key, _, template_id, _ in STARTERS:
        if windows_planning and key != "product":
            continue
        result = api("GET", f"/api/workflows/{template_id}/executor-requirements?projectId={target.project_id}")
        requirements = result if isinstance(result, list) else result.get("requirements", [])
        if not requirements or any(not item.get("configured") for item in requirements):
            raise RuntimeError("当前 LoopForge 账号缺少可用的方案执行器，请在个人配置中完成授权")


def set_default(target, api, project):
    feature, ui, product = STARTERS[:3]
    choices = [{"id": key, "name": title, "default": key == "feature",
                "bindings": [{"workflowId": f"{target.project_id}-{key}", "order": 1}]}
               for key, title, _, _ in (feature, ui, product)]
    existing = {book["id"]: book for book in project.get("sopPlaybooks", [])}
    if existing and set(existing) - {item["id"] for item in choices}:
        raise RuntimeError("项目新增了其他预设，切换前需要保留已有绑定")
    project["sopPlaybooks"] = choices
    project["workflowId"] = target.project_id + "-feature"
    updated = api("PUT", "/api/projects", project)
    assert updated["workflowId"] == target.project_id + "-feature"
    assert next(book for book in updated["sopPlaybooks"] if book.get("default"))["id"] == "feature"


def read_pool(settings, pool_id):
    if "pools" not in settings and pool_id is None:
        return None
    pool = settings.get("pools", {}).get(pool_id)
    if not pool:
        raise ValueError(f"Unknown worker pool: {pool_id!r}")
    return pool


def preflight_worker(api, settings, pool_id, *, staged_feature=False):
    pool = read_pool(settings, pool_id)
    worker_id = os.environ.get("UNITY_WORKER_ID", settings.get("worker_id", "worker-01"))
    workers = api("GET", "/api/v1/workers")["workers"]
    if len(workers) != 1 or workers[0]["worker_id"] != worker_id:
        raise RuntimeError(f"Worker endpoint does not match configured worker {worker_id}")
    worker = workers[0]
    if staged_feature and "staged-feature-checkpoints" not in worker.get("capabilities", []):
        raise RuntimeError("Worker does not advertise the staged-feature-checkpoints capability")
    if pool is None:
        if worker["status"] != "idle":
            raise RuntimeError(f"Worker {worker_id} is not ready: {worker['status']}")
        return {"worker": worker, "pool": None}

    registered = api("GET", "/api/v1/pools")["pools"]
    ready = next((item for item in registered if item["id"] == pool_id), None)
    if ready is None:
        raise RuntimeError(f"Worker has not registered pool {pool_id}")
    slots = {slot["id"]: slot["status"] for slot in ready["slots"]}
    expected = {slot["id"]: slot.get("enabled", True) for slot in pool["slots"]}
    if slots.keys() != expected.keys():
        raise RuntimeError(f"Registered slots do not match the config for pool {pool_id}")
    for slot_id, enabled in expected.items():
        if enabled and slots[slot_id] not in ("idle", "leased"):
            raise RuntimeError(f"Worker slot {slot_id} is not ready: {slots[slot_id]}")
        if not enabled and slots[slot_id] == "idle":
            raise RuntimeError(f"Worker slot {slot_id} is enabled on the server")
    if staged_feature:
        if not any(expected.values()):
            raise RuntimeError(f"Worker pool {pool_id} has no enabled slot")
    elif "idle" not in slots.values():
        raise RuntimeError(f"Worker pool {pool_id} has no idle enabled slot")
    return {"worker": worker, "pool": ready}


def endpoint(value):
    value = value.rstrip("/")
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
            or url.query or url.fragment):
        raise ValueError("Use a complete HTTP(S) endpoint without credentials, query or fragment")
    return value


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set {name} in the current process environment")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true",
                      help="Publish the selected SOPs (sets project defaults in legacy mode)")
    mode.add_argument("--preflight", action="store_true", help="Check Worker readiness without changing LoopForge")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--output-dir", type=Path, default=Path("state/loopforge"))
    parser.add_argument("--project-id", default=os.environ.get("LOOPFORGE_PROJECT_ID"))
    parser.add_argument("--api-url", default=os.environ.get("LOOPFORGE_API_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--sandbox-profile", default="generic-docker")
    parser.add_argument("--worker-url", default=os.environ.get("UNITY_WORKER_URL"),
                        help="Worker URL reachable by LoopForge script nodes")
    parser.add_argument("--probe-url", help="Worker URL reachable by the machine running this installer")
    parser.add_argument("--pool", help="Use a named pool instead of the default pool")
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument("--staged-feature", action="store_true",
                        help="Preview or publish only the existing project feature SOP")
    layout.add_argument("--windows-planning", action="store_true",
                        help="Use the Windows Worker for code investigation and planning before approval")
    parser.add_argument("--expected-revision", type=int,
                        help="With --staged-feature, stop if the existing feature revision differs")
    args = parser.parse_args(argv)
    if args.expected_revision is not None and (not args.staged_feature or args.preflight
                                                or args.expected_revision < 1):
        parser.error("--expected-revision requires staged preview or publish and a positive revision")

    settings = tomllib.loads(args.config.read_text(encoding="utf-8-sig"))
    pool_id = args.pool or settings.get("default_pool")
    pool = read_pool(settings, pool_id)
    if pool is None:
        if args.windows_planning:
            raise ValueError("Windows planning requires a named pool")
        if args.staged_feature:
            raise ValueError("Staged feature delivery requires a named pool")
    if not args.worker_url:
        parser.error("set --worker-url or UNITY_WORKER_URL")
    worker_url = endpoint(args.worker_url)
    worker_token = require_env("UNITY_WORKER_TOKEN")
    probe_url = endpoint(args.probe_url) if args.probe_url else worker_url
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.preflight:
        result = preflight_worker(call_factory(worker_token, probe_url), settings, pool_id,
                                  staged_feature=args.staged_feature)
        save_json(args.output_dir / "worker-preflight.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return

    if not args.project_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.project_id):
        parser.error("set --project-id to a LoopForge project ID")
    target = Target(args.project_id, args.sandbox_profile, worker_url)
    if args.staged_feature:
        worker_health = preflight_worker(call_factory(worker_token, probe_url), settings, pool_id,
                                        staged_feature=True)
        save_json(args.output_dir / "worker-preflight.json", worker_health)
        api = call_factory(require_env("UNITY_FORMAL_STUDIO_TOKEN"), endpoint(args.api_url))
        result = install_staged_feature(target, api, pool_id, args.output_dir, publish=args.publish,
                                        expected_revision=args.expected_revision)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return

    assemblies = (pool or {}).get("test_assemblies", settings.get("test_assemblies"))
    if not assemblies and not args.windows_planning:
        raise ValueError("Configure test_assemblies for the LoopForge plan handoff")
    token = require_env("UNITY_FORMAL_STUDIO_TOKEN")
    context_token = require_env("UNITY_PLANNING_CONTEXT_TOKEN") if args.windows_planning else None
    if context_token is not None and (len(context_token) < 32 or context_token in (token, worker_token)):
        raise ValueError("Configure an independent Unity planning context token of at least 32 characters")

    worker_health = None
    if args.publish:
        worker_health = preflight_worker(call_factory(worker_token, probe_url), settings, pool_id)
        save_json(args.output_dir / "worker-preflight.json", worker_health)
    api = call_factory(token, endpoint(args.api_url))
    project_path = f"/api/projects/{target.project_id}"
    project = api("GET", project_path)
    if project.get("id") != target.project_id:
        raise RuntimeError("LoopForge project does not match --project-id")
    if args.publish:
        preflight_executors(target, api, windows_planning=args.windows_planning)
    backup = args.output_dir / f"{target.project_id}-before.json"
    if not backup.exists():
        save_json(backup, project)

    credential = worker_credential(target, api, worker_token, publish=args.publish)
    context_credential = None
    if context_token is not None:
        context_credential = upsert_credential(target, api, "Unity Worker 规划运行上下文",
                                               {"UNITY_PLANNING_CONTEXT_TOKEN": context_token}, publish=args.publish)
    execution = execution_workflow(target, credential, pool_id)
    child = save_workflow(target, api, execution, publish=args.publish)
    workflows = []
    for row in STARTERS:
        if args.windows_planning and row[0] != "product":
            descriptor = windows_workflow(target, row, credential, context_credential, pool_id)
            result = save_workflow(target, api, descriptor, publish=args.publish)
            if args.publish or result["status"] != "published":
                assert result.get("projectCredentialIds") == [credential, context_credential]
        else:
            descriptor = starter_workflow(target, api, row, assemblies)
            result = save_workflow(target, api, descriptor, publish=args.publish)
            if args.windows_planning:
                assert not result.get("projectCredentialIds")
        assert result["projectId"] == target.project_id
        workflows.append(result)
    assert child.get("projectCredentialIds") == [credential]
    if args.publish:
        assert all(workflow["status"] == "published" for workflow in [child, *workflows])
        set_default(target, api, api("GET", project_path))
    report = {"project_id": target.project_id, "preflight": worker_health,
              "windows_planning": args.windows_planning,
              "execution": {"id": child["id"], "status": child["status"]},
              "workflows": [{key: workflow[key] for key in ("id", "name", "status")} for workflow in workflows],
              "default": api("GET", project_path).get("workflowId")}
    save_json(args.output_dir / f"{target.project_id}-install.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
