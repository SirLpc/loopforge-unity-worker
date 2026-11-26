import json
import os
import sys
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from codex_executor import CodexExecutor, toml_string
from planning import record_baseline
from process_runner import StageError
from support import test_directory
from test_mvp import TOKEN
from test_workspace import git, repository
from test_workflow import FixtureRunner
from worker import create_app
from worker_config import Settings


class PlanningAgent:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.implementation_briefs = []

    def clarify(self, ctx, briefing):
        text = (self.project / 'hello.txt').read_text().strip()
        return {'goal': briefing['requirement'], 'audience': ['Players'],
                'scope': [f'Replace the current greeting {text!r}'],
                'acceptance_criteria': ['The greeting updates'], 'open_questions': []}

    def plan(self, ctx, briefing):
        self.calls.append((ctx.task['slot_id'], briefing))
        text = (self.project / 'hello.txt').read_text().strip()
        return {
            'requirement_summary': 'Change the current greeting', 'plan': 'Modify the greeting only.',
            'acceptance_criteria': ['The greeting updates'],
            'source_evidence': [{'path': 'hello.txt', 'line': 1, 'excerpt': text}],
            'baseline_compile': False, 'baseline_tests': {'editmode': [], 'playmode': []},
            'test_selection': {'editmode': [{'assembly': 'Tests', 'fixtures': ['Game.UnitTests']}], 'playmode': []},
        }

    def execute(self, ctx, prompt, *, review=False):
        if review:
            return {'verdict': 'pass', 'summary': 'Verified greeting', 'issues': [], 'session_id': 'review-test'}
        self.implementation_briefs.append(json.loads(prompt.split('\n\n', 1)[1]))
        (self.project / 'hello.txt').write_text('new greeting\n')
        return {'success': True, 'summary': 'Changed greeting', 'changed_files': ['hello.txt'],
                'implementation_notes': [], 'known_issues': [], 'session_id': 'implementation-test'}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(test_directory())
        _, self.project, logs = repository(self.root)
        self.agent = PlanningAgent(self.project)
        self.runner = FixtureRunner(self.root)
        env = {'UNITY_EXE': sys.executable,
               'UNITY_PROJECT': str(self.project), 'UNITY_WORKER_LOGS': str(logs), 'UNITY_WORKER_TOKEN': TOKEN}
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch('unity_executor.unity_product_version', return_value='2022.3.62f3'))
        config = replace(Settings.load(), test_assemblies={'editmode': ('Tests',), 'playmode': ('PlayTests',)})
        self.client = self.enterContext(TestClient(create_app(config, runner=self.runner, agent=self.agent),
                                                   headers={'Authorization': f'Bearer {TOKEN}'}))

    def create_response(self, requirement='Change the greeting', request_id=None, **extra):
        response = self.client.post('/api/v1/planning-tasks', json={
            'request_id': request_id or str(uuid4()), 'workflow_run_id': 'run-123',
            'requirement': requirement, 'planning_profile': 'feature', **extra,
        })
        return response

    def create(self, **extra):
        response = self.create_response(**extra)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['task_id']

    def action(self, task, operation, request_id=None, **payload):
        return self.client.post(f'/api/v1/tasks/{task}/{operation}',
                                json={'request_id': request_id or str(uuid4()), **payload})

    def wait(self, action):
        self.assertEqual(action.status_code, 202, action.text)
        job = action.json()['job_id']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.client.get(f'/api/v1/jobs/{job}').json()
            if response['status'] not in {'queued', 'running'}:
                return response
            time.sleep(0.04)
        self.fail(f'Job {job} stayed active')

    def task(self, task):
        return self.client.get(f'/api/v1/tasks/{task}').json()

    def test_staged_planning_task_requires_a_valid_project_id(self):
        for project_id in (None, '', '-game', 'a/b', 'a b', 'a' * 65):
            with self.subTest(project_id=project_id):
                response = self.create_response(workflow_mode='staged-feature', workflow_project_id=project_id)
                self.assertEqual(response.status_code, 422, response.text)
        response = self.create_response(workflow_mode='staged-feature')
        self.assertEqual(response.status_code, 422, response.text)
        task_id = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        self.assertEqual(self.task(task_id)['workflow_project_id'], 'Game_Project-1')
        self.assertEqual(self.task(task_id)['workflow_mode'], 'staged-feature')

    def test_staged_clarify_and_plan_share_the_checkout(self):
        task_id = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        self.assertEqual(self.action(task_id, 'plan').status_code, 409)
        request_id = str(uuid4())
        action = self.action(task_id, 'clarify', request_id=request_id, queue_wait_seconds=10)
        clarification_job = self.wait(action)
        self.assertEqual(clarification_job['status'], 'succeeded', clarification_job)
        clarified = self.task(task_id)
        self.assertTrue(clarified['lease'])
        self.assertTrue(clarified['pool_id'])
        self.assertEqual(clarified['workflow_run_id'], 'run-123')
        self.assertEqual(clarified['current_stage'], 'clarified')
        self.assertEqual(clarified['base_commit'], git(self.project, 'rev-parse', 'HEAD'))
        self.assertEqual(clarified['task_branch'], git(self.project, 'branch', '--show-current'))
        self.assertEqual(clarified['clarification']['scope'], ["Replace the current greeting 'old'"])
        repeated = self.action(task_id, 'clarify', request_id=request_id, queue_wait_seconds=10)
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()['job_id'], clarification_job['job_id'])

        plan_job = self.wait(self.action(task_id, 'plan'))
        self.assertEqual(plan_job['status'], 'succeeded', plan_job)
        planned = self.task(task_id)
        self.assertEqual((planned['slot_id'], planned['base_commit']), (clarified['slot_id'], clarified['base_commit']))
        self.assertEqual(self.agent.calls[-1][1]['clarification'], clarified['clarification'])
        self.assertEqual(planned['planning']['source_evidence'][0], {'path': 'hello.txt', 'line': 1, 'excerpt': 'old'})
        self.assertEqual(self.action(task_id, 'implement').status_code, 409)

    def test_staged_plan_freeze_rejects_stale_workspace_and_plan(self):
        task_id = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        self.assertEqual(self.wait(self.action(task_id, 'clarify'))['status'], 'succeeded')
        self.assertEqual(self.wait(self.action(task_id, 'plan'))['status'], 'succeeded')
        first = self.task(task_id)
        binding = {'slot_id': first['slot_id'], 'base_commit': first['base_commit'],
                   'plan_digest': first['planning']['plan_digest']}
        for wrong in ({'slot_id': 'other-slot'}, {'base_commit': 'a' * 40}):
            self.assertEqual(self.action(task_id, 'accept-plan', **{**binding, **wrong}).status_code, 409)
        self.assertEqual(self.wait(self.action(task_id, 'plan', feedback='Keep the text short'))['status'], 'succeeded')
        self.assertEqual(self.action(task_id, 'accept-plan', **binding).status_code, 409)
        latest = self.task(task_id)['planning']['plan_digest']
        accepted = self.wait(self.action(task_id, 'accept-plan', **{**binding, 'plan_digest': latest}))
        self.assertEqual(accepted['status'], 'succeeded', accepted)

    def test_staged_plan_freeze_revalidates_the_live_checkout(self):
        task_id = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        self.assertEqual(self.wait(self.action(task_id, 'clarify'))['status'], 'succeeded')
        self.assertEqual(self.wait(self.action(task_id, 'plan'))['status'], 'succeeded')
        planned = self.task(task_id)
        binding = {'slot_id': planned['slot_id'], 'base_commit': planned['base_commit'],
                   'plan_digest': planned['planning']['plan_digest']}
        greeting = self.project / 'hello.txt'

        for mismatch in ('branch', 'HEAD', 'dirty'):
            with self.subTest(mismatch=mismatch):
                if mismatch == 'branch':
                    git(self.project, 'switch', 'main')
                elif mismatch == 'HEAD':
                    git(self.project, 'commit', '--allow-empty', '-m', 'Change HEAD without changing the tree')
                else:
                    greeting.write_text('outside change\n')
                try:
                    job = self.wait(self.action(task_id, 'accept-plan', **binding))
                    self.assertEqual(job['status'], 'failed', job)
                    self.assertEqual(job['failure_kind'], 'dirty_workspace' if mismatch == 'dirty' else 'workspace_changed')
                    current = self.task(task_id)
                    self.assertTrue(current['lease'])
                    self.assertEqual(current['planning']['plan_digest'], binding['plan_digest'])
                    self.assertFalse(current.get('approved_plan_digest'))
                    self.assertEqual(self.action(task_id, 'implement').status_code, 409)
                finally:
                    if mismatch == 'branch':
                        git(self.project, 'switch', planned['task_branch'])
                    elif mismatch == 'HEAD':
                        git(self.project, 'reset', '--soft', planned['base_commit'])
                    else:
                        greeting.write_text('old\n')

        request_id = str(uuid4())
        accepted = self.wait(self.action(task_id, 'accept-plan', request_id=request_id, **binding))
        self.assertEqual(accepted['status'], 'succeeded', accepted)
        self.assertEqual(self.task(task_id)['current_stage'], 'plan_accepted')
        self.assertEqual(self.task(task_id)['approved_plan_digest'], binding['plan_digest'])
        repeated = self.action(task_id, 'accept-plan', request_id=request_id, **binding)
        self.assertEqual(repeated.json()['job_id'], accepted['job_id'])

    def test_staged_plan_keeps_clarified_criteria_and_scope_for_implementation(self):
        task_id = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        settled = ['The greeting updates', 'The greeting remains local']
        extra = 'The editor starts with the new greeting'

        def clarify(ctx, briefing):
            return {**PlanningAgent.clarify(self.agent, ctx, briefing), 'acceptance_criteria': settled}

        def plan(ctx, briefing):
            return {**PlanningAgent.plan(self.agent, ctx, briefing), 'acceptance_criteria': [settled[0], extra]}

        with patch.object(self.agent, 'clarify', side_effect=clarify), patch.object(self.agent, 'plan', side_effect=plan):
            self.assertEqual(self.wait(self.action(task_id, 'clarify'))['status'], 'succeeded')
            self.assertEqual(self.wait(self.action(task_id, 'plan'))['status'], 'succeeded')
        planned = self.task(task_id)
        scope = ["Replace the current greeting 'old'"]
        self.assertEqual(planned['planning']['acceptance_criteria'], [*settled, extra])
        self.assertEqual(planned['planning']['scope'], scope)
        binding = {'slot_id': planned['slot_id'], 'base_commit': planned['base_commit'],
                   'plan_digest': planned['planning']['plan_digest']}
        accepted = self.wait(self.action(task_id, 'accept-plan', **binding))
        self.assertEqual(accepted['status'], 'succeeded', accepted)
        implemented = self.wait(self.action(task_id, 'implement'))
        self.assertEqual(implemented['status'], 'succeeded', implemented)
        self.assertEqual(self.agent.implementation_briefs[-1]['acceptance_criteria'], [*settled, extra])
        self.assertEqual(self.agent.implementation_briefs[-1]['scope'], scope)

    def test_clarify_waits_for_a_slot_and_can_reconnect_to_the_queued_job(self):
        first = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        second = self.create(workflow_mode='staged-feature', workflow_project_id='Game_Project-1')
        self.assertEqual(self.wait(self.action(first, 'clarify'))['status'], 'succeeded')
        request_id = str(uuid4())
        queued = self.action(second, 'clarify', request_id=request_id, queue_wait_seconds=10)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()['status'], 'queued')
        self.assertFalse(self.task(second)['lease'])
        repeated = self.action(second, 'clarify', request_id=request_id, queue_wait_seconds=10)
        self.assertEqual(repeated.json()['job_id'], queued.json()['job_id'])
        self.assertEqual(self.action(first, 'cancel').status_code, 202)
        self.assertEqual(self.wait(self.action(first, 'release'))['status'], 'succeeded')
        self.assertEqual(self.wait(queued)['status'], 'succeeded')
        self.assertTrue(self.task(second)['lease'])

    def test_codex_clarification_reads_the_assigned_checkout_with_planning_permissions(self):
        from pathlib import Path

        sibling = self.root / 'unleased-checkout'
        cfg = SimpleNamespace(project=self.project, checkout=self.project, codex_exe=str(self.root / 'codex.exe'),
                              codex_home=self.root / 'codex-home', codex_sandbox='mxc', codex_timeout=10,
                              sibling_checkouts=(sibling,))
        agent = CodexExecutor(cfg)
        self.assertTrue(hasattr(agent, 'clarify'), 'CodexExecutor must have a native clarification stage')

        def run(argv, label, *, cwd, data, **options):
            permissions = ' '.join(argv)
            self.assertIn(f'{toml_string(self.project)}="read"', permissions)
            self.assertIn(f'{toml_string(self.project / ".git")}="deny"', permissions)
            self.assertIn(f'{toml_string(sibling)}="deny"', permissions)
            self.assertIn('permissions.unity_worker.network.enabled=false', permissions)
            self.assertEqual(cwd, self.project)
            self.assertIn('Change the greeting', data.decode())
            response = {'goal': 'Change the greeting', 'audience': ['Players'], 'open_questions': [],
                        'scope': [(cwd / 'hello.txt').read_text().strip()], 'acceptance_criteria': ['Greeting changes']}
            Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(response), encoding='utf-8')
            return SimpleNamespace(exit_code=0)

        ctx = SimpleNamespace(prefix=lambda name: self.root / name, run=run)
        with patch.object(agent, 'capabilities', return_value='--skip-git-repo-check --output-schema'), \
                patch.object(agent, 'verify_sandbox'):
            result = agent.clarify(ctx, {'requirement': 'Change the greeting'})
        self.assertEqual(result['scope'], ['old'])

    def test_plan_prepares_readable_checkout_and_blocks_stale_or_unapproved_development(self):
        task_id = self.create()
        self.assertEqual(self.action(task_id, 'run').status_code, 409)
        plan_job = self.wait(self.action(task_id, 'plan', queue_wait_seconds=10))
        self.assertEqual(plan_job['status'], 'succeeded', plan_job)
        first = self.task(task_id)
        self.assertEqual(first['status'], 'awaiting_plan_approval')
        self.assertTrue(first['lease'])
        self.assertEqual(first['base_commit'], git(self.project, 'rev-parse', 'HEAD').strip())
        self.assertEqual(first['planning']['source_evidence'][0]['path'], 'hello.txt')
        binding = {'slot_id': first['slot_id'], 'base_commit': first['base_commit'],
                   'plan_digest': first['planning']['plan_digest']}
        self.assertEqual(self.action(task_id, 'run').status_code, 409)
        self.assertEqual(self.action(task_id, 'accept-plan', **{**binding, 'plan_digest': 'a' * 64}).status_code, 409)
        self.assertEqual(self.action(task_id, 'accept-plan', **binding).status_code, 202)
        second = self.task(task_id)
        self.assertEqual(second['slot_id'], first['slot_id'])
        self.assertEqual(second['base_commit'], first['base_commit'])
        self.assertEqual(second['current_stage'], 'plan_accepted')
        self.assertEqual(second['approved_plan_digest'], binding['plan_digest'])
        self.assertEqual(len(self.agent.calls), 1)
        developed = self.wait(self.action(task_id, 'run'))
        self.assertEqual(developed['status'], 'succeeded', developed)
        final = self.task(task_id)
        self.assertEqual(final['slot_id'], first['slot_id'])
        self.assertEqual(final['base_commit'], first['base_commit'])
        self.assertEqual(final['status'], 'awaiting_approval')
        self.assertFalse(final['pushed'])

    def test_existing_source_citation_must_match_git_baseline(self):
        task_id = self.create()
        def invalid(ctx, briefing):
            return {**PlanningAgent.plan(self.agent, ctx, briefing), 'source_evidence': [
                {'path': 'hello.txt', 'line': 1, 'excerpt': 'unverified claim'}]}
        with patch.object(self.agent, 'plan', side_effect=invalid):
            job = self.wait(self.action(task_id, 'plan'))
        self.assertEqual(job['failure_kind'], 'plan_evidence_invalid', job)
        self.assertTrue(self.task(task_id)['lease'])

    def test_replan_invalidates_previous_plan_and_preserves_slot(self):
        task_id = self.create()
        self.assertEqual(self.wait(self.action(task_id, 'plan'))['status'], 'succeeded')
        before = self.task(task_id)
        self.assertEqual(self.wait(self.action(task_id, 'plan', feedback='Check existing conventions'))['status'], 'succeeded')
        after = self.task(task_id)
        self.assertEqual(before['slot_id'], after['slot_id'])
        self.assertEqual(before['base_commit'], after['base_commit'])
        self.assertNotEqual(before['planning']['plan_digest'], after['planning']['plan_digest'])
        self.assertEqual(self.action(task_id, 'accept-plan', slot_id=before['slot_id'], base_commit=before['base_commit'],
                                     plan_digest=before['planning']['plan_digest']).status_code, 409)

    def test_native_compile_runs_during_planning_and_is_included_in_the_final_brief(self):
        task_id = self.create()
        def request_compile(ctx, briefing):
            normal = PlanningAgent.plan(self.agent, ctx, briefing)
            return {**normal, 'baseline_compile': 'baseline_results' not in briefing}
        with patch.object(self.agent, 'plan', side_effect=request_compile):
            result = self.wait(self.action(task_id, 'plan'))
        self.assertEqual(result['status'], 'succeeded', result)
        records = self.task(task_id)['planning']['baseline']
        self.assertEqual([(row['mode'], row['status']) for row in records], [('compile', 'passed')])
        self.assertEqual(len(self.agent.calls), 2)
        self.assertEqual(self.agent.calls[-1][1]['baseline_results'], records)

    def test_baseline_keeps_scoped_assertion_failure_as_evidence(self):
        scope = {'assembly': 'Tests', 'fixtures': ['Example.ExistingTests']}
        result = {'failed': 1, 'test_results': 'baseline.xml', 'failed_tests': [{'name': 'Example.ExistingTests.Case'}]}
        workspace = SimpleNamespace(unity=SimpleNamespace(), git=SimpleNamespace(
            checkout_is=lambda *_: None, assert_clean=lambda *_: None))
        ctx = SimpleNamespace(workspace=workspace, task={'base_commit': 'a' * 40})
        with patch.object(workspace.unity, 'run', create=True, side_effect=StageError('tests_failed', 'Assertion', result=result)) as run:
            evidence = record_baseline(ctx, False, {'playmode': [scope]})
        run.assert_called_once_with(ctx, 'playmode', scope=scope)
        self.assertEqual(evidence, [{'mode': 'playmode', 'scope': scope, 'status': 'tests_failed', **result}])
