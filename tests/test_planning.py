import os
import sys
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

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

    def create(self, requirement='Change the greeting', request_id=None):
        response = self.client.post('/api/v1/planning-tasks', json={
            'request_id': request_id or str(uuid4()), 'workflow_run_id': 'run-123',
            'requirement': requirement, 'planning_profile': 'feature',
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['task_id']

    def action(self, task, operation, **payload):
        return self.client.post(f'/api/v1/tasks/{task}/{operation}', json={'request_id': str(uuid4()), **payload})

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
        from types import SimpleNamespace

        scope = {'assembly': 'Tests', 'fixtures': ['Example.ExistingTests']}
        result = {'failed': 1, 'test_results': 'baseline.xml', 'failed_tests': [{'name': 'Example.ExistingTests.Case'}]}
        workspace = SimpleNamespace(unity=SimpleNamespace(), git=SimpleNamespace(
            checkout_is=lambda *_: None, assert_clean=lambda *_: None))
        ctx = SimpleNamespace(workspace=workspace, task={'base_commit': 'a' * 40})
        with patch.object(workspace.unity, 'run', create=True, side_effect=StageError('tests_failed', 'Assertion', result=result)) as run:
            evidence = record_baseline(ctx, False, {'playmode': [scope]})
        run.assert_called_once_with(ctx, 'playmode', scope=scope)
        self.assertEqual(evidence, [{'mode': 'playmode', 'scope': scope, 'status': 'tests_failed', **result}])
